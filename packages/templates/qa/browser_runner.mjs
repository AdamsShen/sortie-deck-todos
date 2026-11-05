#!/usr/bin/env node
// Sortie Deck QA 浏览器复验 runner（QA 阶段注入 workspace/scripts/qa_browser_runner.mjs）。
//
// 职责：起服务 → 轮询健康检查 → 用无头 Chrome 跑 qa.config.json 里的场景 → 写
// qa_result.json + 截图 → 清理服务与浏览器进程。
// 只依赖 Node 内置能力（Node >= 21 自带 WebSocket），不安装任何 npm 包；找不到浏览器时
// 返回 browser_unavailable，让模型退回 HTTP 级检查，而不是伪造浏览器结论。
//
// 用法：node scripts/qa_browser_runner.mjs [config.json]
// 退出码：0 passed / 1 failed / 2 missing_config / 3 browser_unavailable / 4 start_failed

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ARGV = process.argv.slice(2);
// 允许 `--cases TC-X-01` 时省略 config 路径
const CONFIG_PATH = ARGV[0] && !ARGV[0].startsWith("--") ? ARGV[0] : "qa.config.json";
const RESULT_PATH = "qa_result.json";
const SHOT_DIR = "qa_screenshots";
const STATE_PATH = ".qa_runner_state.json";
const DEFAULT_TIMEOUT_SEC = 60;
const CASE_ID_RE = /TC-[A-Za-z0-9]+-\d+/g;

class RunnerError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// 用例白名单：只认 test_cases.md 里出现过的 TC-xxx-n 编号。
// 没有用例文件时返回空集合（不限制），保持手工调试可用。
function loadAllowedCases(cfg) {
  const candidates = [
    cfg.cases_file,
    ".upstream/test_cases.md",
    "artifacts/qa_verify/test_cases.md",
    "test_cases.md",
  ];
  for (const file of candidates) {
    if (!file || !existsSync(file)) continue;
    const ids = [...new Set(readFileSync(file, "utf8").match(CASE_ID_RE) || [])];
    if (ids.length) return { ids: new Set(ids), source: file };
  }
  return { ids: new Set(), source: "" };
}

// `--cases TC-X-01,TC-X-02`：返工时只回归 QA 判失败的用例。
function parseCaseFilter(argv) {
  const collected = [];
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--cases" && argv[i + 1]) collected.push(argv[i + 1]);
    else if (argv[i].startsWith("--cases=")) collected.push(argv[i].slice("--cases=".length));
  }
  return new Set(
    collected
      .join(",")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

// 验证预算：跨进程累加，超出后拒绝再跑，逼 agent 按现状收尾而不是无限加用例。
function bumpRunBudget(cfg) {
  const max = Number(cfg.max_runs ?? 5);
  let state = {};
  try {
    state = JSON.parse(readFileSync(STATE_PATH, "utf8"));
  } catch {
    state = {};
  }
  state.runs = Number(state.runs || 0) + 1;
  state.updated_at = new Date().toISOString();
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2) + "\n", "utf8");
  if (max > 0 && state.runs > max) {
    throw new RunnerError(
      "budget_exhausted",
      `验证预算已用尽（上限 ${max} 次，本次第 ${state.runs} 次）：按现有 qa_result.json 收尾，不要再加用例`,
      { runs: state.runs, max_runs: max },
    );
  }
  return { runs: state.runs, max_runs: max };
}

function scopeSkipReason(caseId, scope) {
  if (scope.only.size && !scope.only.has(caseId)) {
    return `case_id 不在 --cases（${[...scope.only].join(",")}）`;
  }
  if (scope.allowed.size && !scope.allowed.has(caseId)) {
    return `case_id 不在 ${scope.source}（用例边界外，不计入判定）`;
  }
  return "";
}

function readConfig() {
  if (!existsSync(CONFIG_PATH)) {
    throw new RunnerError(
      "missing_config",
      `${CONFIG_PATH} 不存在：请按项目已有入口填写 start_command / base_url / health_url / scenarios`,
    );
  }
  let cfg;
  try {
    cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  } catch (err) {
    throw new RunnerError("missing_config", `${CONFIG_PATH} 不是合法 JSON: ${err.message}`);
  }
  if (!cfg || typeof cfg !== "object") {
    throw new RunnerError("missing_config", `${CONFIG_PATH} 内容为空`);
  }
  if (!cfg.base_url) {
    throw new RunnerError("missing_config", `${CONFIG_PATH} 缺少 base_url`);
  }
  return cfg;
}

function findChrome(cfg) {
  const candidates = [
    cfg.chrome,
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  ];
  return candidates.find((p) => p && existsSync(p)) || "";
}

function killTree(child) {
  if (!child || child.exitCode !== null) return;
  try {
    // detached 启动 => 子进程自成一个进程组，整组杀，避免孙子进程占着端口
    process.kill(-child.pid, "SIGTERM");
  } catch {
    try {
      child.kill("SIGTERM");
    } catch {
      /* 已经退出 */
    }
  }
}

function startServer(cmd) {
  const child = spawn(cmd, { shell: true, detached: true, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  const append = (buf) => {
    log += buf.toString();
    if (log.length > 8000) log = log.slice(-8000);
  };
  child.stdout?.on("data", append);
  child.stderr?.on("data", append);
  child.serverLog = () => log;
  return child;
}

async function waitHealth(url, seconds, server, serverLog) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (res.status < 500) return;
    } catch {
      /* 服务还没起来 */
    }
    if (server && server.exitCode !== null) break;
    await sleep(1000);
  }
  throw new RunnerError("start_failed", `服务未在 ${seconds}s 内就绪`, {
    server_log: serverLog ? serverLog().slice(-2000) : "",
  });
}

async function waitDevToolsPort(profileDir, timeoutMs) {
  const file = join(profileDir, "DevToolsActivePort");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (existsSync(file)) {
      const [port] = readFileSync(file, "utf8").split("\n");
      if (port && Number(port) > 0) return Number(port);
    }
    await sleep(200);
  }
  throw new RunnerError("browser_unavailable", "Chrome 启动后未写出 DevToolsActivePort");
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(typeof event.data === "string" ? event.data : "{}");
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message || "CDP error"));
        else resolve(msg.result || {});
        return;
      }
      for (const fn of this.listeners.get(msg.method) || []) fn(msg.params || {});
    });
  }

  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error("CDP 连接失败")), { once: true });
    });
    return new CDP(ws);
  }

  send(method, params = {}) {
    const id = ++this.seq;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
    return () => {
      const arr = this.listeners.get(method) || [];
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    };
  }

  waitFor(method, timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        off();
        reject(new Error(`等待 ${method} 超时`));
      }, timeoutMs);
      const off = this.on(method, (params) => {
        clearTimeout(timer);
        off();
        resolve(params);
      });
    });
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* ignore */
    }
  }
}

async function evaluate(cdp, expression) {
  const res = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (res.exceptionDetails) {
    throw new Error(res.exceptionDetails.text || "页面脚本执行失败");
  }
  return res.result?.value;
}

const VISIBILITY_JS = (selector) => `(() => {
  const el = document.querySelector(${JSON.stringify(selector)});
  if (!el) return "missing";
  const style = getComputedStyle(el);
  const box = el.getBoundingClientRect();
  const visible = style.display !== "none" && style.visibility !== "hidden"
    && Number(style.opacity) > 0.01 && box.width > 0 && box.height > 0;
  return visible ? "visible" : "hidden";
})()`;

const TEXT_JS = (selector) => `(() => {
  const el = document.querySelector(${JSON.stringify(selector)});
  return el ? (el.innerText || el.textContent || "") : "";
})()`;

const CLICK_JS = (selector) => `(() => {
  const el = document.querySelector(${JSON.stringify(selector)});
  if (!el) return "missing";
  el.click();
  return "ok";
})()`;

// 用原生 setter 写值，React 受控组件也能收到 input 事件
const TYPE_JS = (selector, text) => `(() => {
  const el = document.querySelector(${JSON.stringify(selector)});
  if (!el) return "missing";
  el.focus();
  const proto = el instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (setter) setter.call(el, ${JSON.stringify(text)});
  else el.value = ${JSON.stringify(text)};
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return "ok";
})()`;

const KEY_CODES = {
  ArrowUp: 38,
  ArrowDown: 40,
  ArrowLeft: 37,
  ArrowRight: 39,
  Enter: 13,
  Escape: 27,
  Space: 32,
};

async function pressKey(cdp, key) {
  const code = KEY_CODES[key] ?? 0;
  const base = {
    key,
    code: key === " " ? "Space" : key,
    windowsVirtualKeyCode: code,
    nativeVirtualKeyCode: code,
  };
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", ...base });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

function isNoise(url) {
  return /favicon\.ico$/i.test(url || "");
}

async function runScenario(cdp, scenario, baseUrl, cfg, scope) {
  const steps = Array.isArray(scenario.steps) ? scenario.steps : [];
  const caseId = String(scenario.case_id || "").trim();
  const skipReason = scopeSkipReason(caseId, scope);
  if (skipReason) {
    // 用例边界外的场景不执行、不计入判定，只在结果里留痕
    return {
      name: scenario.name || scenario.path || "scenario",
      case_id: caseId || null,
      path: scenario.path || "/",
      url: "",
      out_of_scope: true,
      skipped: true,
      passed: false,
      reason: skipReason,
      doc_status: null,
      steps: [],
      errors: [],
    };
  }
  const errors = [];
  const records = [];
  let docStatus = null;

  const offs = [
    cdp.on("Runtime.exceptionThrown", (p) => {
      const desc = p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || "";
      errors.push(`pageerror: ${String(desc).split("\n")[0].slice(0, 300)}`);
    }),
    cdp.on("Runtime.consoleAPICalled", (p) => {
      if (p.type !== "error") return;
      const text = (p.args || [])
        .map((a) => a.value ?? a.description ?? "")
        .join(" ")
        .slice(0, 300);
      errors.push(`console: ${text}`);
    }),
    cdp.on("Log.entryAdded", (p) => {
      const entry = p.entry || {};
      if (entry.level !== "error") return;
      // favicon 404 是浏览器默认请求，不代表页面有问题
      if (isNoise(entry.url)) return;
      errors.push(`log: ${String(entry.text).slice(0, 300)}`);
    }),
    cdp.on("Network.responseReceived", (p) => {
      const res = p.response || {};
      if (res.type === "Document" && docStatus === null) docStatus = res.status;
      if (res.status >= 400 && !isNoise(res.url)) {
        errors.push(`http ${res.status}: ${String(res.url).slice(0, 200)}`);
      }
    }),
  ];

  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Log.enable");

  const url = new URL(scenario.path || "/", baseUrl).toString();
  const loaded = cdp.waitFor("Page.loadEventFired", 30000).catch(() => null);
  await cdp.send("Page.navigate", { url });
  await loaded;
  await sleep(Number(scenario.wait_ms || 300));

  const add = (step, passed, detail) => records.push({ step, passed, detail });

  for (const step of steps) {
    const label = Object.keys(step)[0] || "unknown";
    try {
      if (step.wait_ms !== undefined) {
        await sleep(Number(step.wait_ms));
        add(label, true, `${step.wait_ms}ms`);
      } else if (step.click !== undefined) {
        const out = await evaluate(cdp, CLICK_JS(step.click));
        add(label, out === "ok", `${step.click} -> ${out}`);
      } else if (step.type !== undefined) {
        const out = await evaluate(cdp, TYPE_JS(step.type.selector, step.type.text ?? ""));
        add(label, out === "ok", `${step.type.selector} -> ${out}`);
      } else if (step.key !== undefined) {
        await pressKey(cdp, step.key);
        add(label, true, step.key);
      } else if (step.expect_visible !== undefined) {
        const out = await evaluate(cdp, VISIBILITY_JS(step.expect_visible));
        add(label, out === "visible", `${step.expect_visible} -> ${out}`);
      } else if (step.expect_hidden !== undefined) {
        const out = await evaluate(cdp, VISIBILITY_JS(step.expect_hidden));
        add(label, out === "hidden", `${step.expect_hidden} -> ${out}`);
      } else if (step.expect_text !== undefined) {
        const text = await evaluate(cdp, TEXT_JS(step.expect_text.selector || "body"));
        const ok = String(text).includes(step.expect_text.contains ?? "");
        add(label, ok, `${step.expect_text.selector || "body"} -> ${String(text).slice(0, 80)}`);
      } else if (step.screenshot !== undefined) {
        const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
        mkdirSync(SHOT_DIR, { recursive: true });
        const file = join(SHOT_DIR, step.screenshot);
        writeFileSync(file, Buffer.from(shot.data, "base64"));
        add(label, true, file);
      } else {
        add(label, false, "未知步骤，支持 click/type/key/expect_visible/expect_hidden/expect_text/screenshot/wait_ms");
      }
    } catch (err) {
      add(label, false, String(err.message || err));
    }
  }

  for (const off of offs) off();

  const stepsOk = records.every((r) => r.passed);
  const docOk = docStatus === null || (docStatus >= 200 && docStatus < 400);
  return {
    name: scenario.name || scenario.path || "scenario",
    case_id: caseId || null,
    path: scenario.path || "/",
    url,
    out_of_scope: false,
    passed: stepsOk && docOk && errors.length === 0,
    doc_status: docStatus,
    steps: records,
    errors,
  };
}

async function withBrowser(cfg, fn) {
  const bin = findChrome(cfg);
  if (!bin) {
    throw new RunnerError(
      "browser_unavailable",
      "未找到 Chrome/Chromium：用 CHROME_PATH 或 qa.config.json 的 chrome 指定路径",
    );
  }
  if (typeof WebSocket !== "function") {
    throw new RunnerError("browser_unavailable", "当前 Node 不支持内置 WebSocket（需要 Node >= 21）");
  }
  const viewport = cfg.viewport || {};
  const profile = join(tmpdir(), `sortie-qa-${process.pid}-${Date.now()}`);
  mkdirSync(profile, { recursive: true });
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--hide-scrollbars",
    "--mute-audio",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    `--window-size=${viewport.width || 1280},${viewport.height || 800}`,
    "about:blank",
  ];
  // 容器里通常以 root 跑，Chrome 不加 --no-sandbox 会直接拒绝启动
  if (typeof process.getuid === "function" && process.getuid() === 0) {
    args.unshift("--no-sandbox", "--disable-dev-shm-usage");
  }
  const child = spawn(bin, args, { detached: true, stdio: "ignore" });
  const port = await waitDevToolsPort(profile, 20000);
  let target;
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
    target = await res.json();
  } catch {
    const res = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`);
    target = await res.json();
  }
  const cdp = await CDP.connect(target.webSocketDebuggerUrl);
  try {
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: viewport.width || 1280,
      height: viewport.height || 800,
      deviceScaleFactor: 1,
      mobile: Boolean(viewport.mobile),
    });
    return await fn(cdp);
  } finally {
    cdp.close();
    killTree(child);
    // Chrome 退出时还在写 profile，立刻删会 ENOTEMPTY；清理失败不影响结果
    await sleep(300);
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch {
      /* 临时目录残留无碍 */
    }
  }
}

function writeResult(payload) {
  writeFileSync(RESULT_PATH, JSON.stringify(payload, null, 2) + "\n", "utf8");
}

async function main() {
  const startedAt = Date.now();
  const cfg = readConfig();
  const timeout = Number(cfg.timeout_sec || DEFAULT_TIMEOUT_SEC);
  const health = cfg.health_url || cfg.base_url;
  const allowed = loadAllowedCases(cfg);
  const only = parseCaseFilter(ARGV);
  const budget = bumpRunBudget(cfg);
  const scope = { allowed: allowed.ids, source: allowed.source, only };
  let server = null;
  try {
    if (cfg.start_command) {
      server = startServer(cfg.start_command);
      await waitHealth(health, timeout, server, server.serverLog);
    } else {
      await waitHealth(health, Math.min(timeout, 10), null, null);
    }

    const scenarios = Array.isArray(cfg.scenarios) && cfg.scenarios.length
      ? cfg.scenarios
      : [{ name: "首页可加载", path: "/", steps: [{ expect_visible: "body" }] }];

    const results = await withBrowser(cfg, async (cdp) => {
      const out = [];
      for (const scenario of scenarios) {
        out.push(await runScenario(cdp, scenario, cfg.base_url, cfg, scope));
      }
      return out;
    });

    const executed = results.filter((r) => !r.out_of_scope);
    const failed = executed.filter((r) => !r.passed).length;
    const passed = executed.length - failed;
    const skipped = results.filter((r) => r.out_of_scope);
    const payload = {
      status: executed.length > 0 && failed === 0 ? "passed" : "failed",
      ok: executed.length > 0 && failed === 0,
      base_url: cfg.base_url,
      summary: {
        scenarios: executed.length,
        passed,
        failed,
        out_of_scope: skipped.length,
        browser_errors: results.reduce((n, r) => n + r.errors.length, 0),
      },
      scope: {
        cases_file: allowed.source || null,
        allowed_cases: allowed.ids.size,
        only_cases: [...only],
        skipped_cases: skipped.map((r) => r.case_id || r.name),
      },
      budget,
      scenarios: results,
      screenshots: results.flatMap((r) =>
        r.steps.filter((s) => s.step === "screenshot" && s.passed).map((s) => s.detail),
      ),
      duration_ms: Date.now() - startedAt,
      next:
        executed.length === 0
          ? "no scenario executed: every case_id is outside test_cases.md or --cases; fix qa.config.json"
          : failed === 0
            ? ""
            : "read qa_result.json, fix only the failing cases, then re-run this script",
    };
    writeResult(payload);
    console.log(
      `QA browser runner: ${payload.status} (${passed}/${executed.length} cases, ${skipped.length} out of scope)`,
    );
    return payload.ok ? 0 : 1;
  } finally {
    killTree(server);
  }
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    const status = err instanceof RunnerError ? err.status : "failed";
    if (status === "budget_exhausted") {
      // 不覆盖上一轮结果：预算耗尽后模型应按现有 qa_result.json 收尾
      writeFileSync(
        "qa_budget.json",
        JSON.stringify(
          {
            status,
            ok: false,
            error: String(err.message || err),
            ...(err instanceof RunnerError ? err.extra : {}),
            next: "stop testing: report the existing qa_result.json as-is, do not add cases or scripts",
          },
          null,
          2,
        ) + "\n",
        "utf8",
      );
    } else {
      writeResult({
        status,
        ok: false,
        error: String(err.message || err),
        ...(err instanceof RunnerError ? err.extra : {}),
        next:
          status === "browser_unavailable"
            ? "fall back to HTTP-level checks (curl) and say so in the report; do not fabricate browser results"
            : "fix qa.config.json / the start command, then re-run this script",
      });
    }
    console.error(`QA browser runner: ${status} — ${err.message || err}`);
    const codes = { missing_config: 2, browser_unavailable: 3, start_failed: 4, budget_exhausted: 5 };
    process.exit(codes[status] || 1);
  });
