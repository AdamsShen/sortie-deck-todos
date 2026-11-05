# 自测阶段脚本化：固定 e2e runner，LLM 只修失败项

- 日期：2026-11-05
- 状态：待确认（未改代码）
- 关联：[2026-11-05-pipeline-complexity-tiers.md](./2026-11-05-pipeline-complexity-tiers.md)
- 数据来源：任务 `ini_1f447c262543`（贪吃蛇小游戏）

## 1. 问题

自测阶段（`eng_web_selftest` / `eng_backend_selftest`）实测工具调用分布：

| 阶段 | 总步数 | 起服务 | pkill/kill | sleep | ps/lsof | 跑测试 |
|---|---|---|---|---|---|---|
| eng_web_selftest | 116 | 2 | 0 | 1 | 4 | 20 |
| eng_backend_selftest | 125 | 25 | 22 | 22 | 28 | 2 |
| 合计 | **241** | **27** | **22** | **22** | **28** | 22 |

两个阶段合计 241 步，其中 **99 步（41%）是环境管理**（起服务、杀进程、等待、查进程），真正跑测试只有 22 步。按每步 6-8 秒估算，单任务在这上面白耗约 12-13 分钟。

根因：

1. 每条 Bash 是独立子进程，后台服务不跨命令保活，每次验证都要重新拉起；
2. 起服务是异步的，必须 `sleep` + `ps/lsof` 确认端口就绪；
3. 没有固定脚本，路径/端口/环境变量每次现拼，写错就多一轮排查；
4. 残留进程导致端口占用，触发额外的 pkill 重试。

## 2. 业内做法

| 来源 | 做法 | 启示 |
|---|---|---|
| [Playwright webServer](https://playwright.dev/docs/test-webserver) | 测试运行器按 `webServer.command` 启动服务、等待 URL 就绪、跑完自动关闭；`reuseExistingServer` 控制复用 | 服务生命周期交给 runner，用例只关心断言 |
| [SWE-bench 评测机制](https://www.bestaiweb.ai/glossary/swe-bench) | agent 只产出 patch，测试由评测 harness 统一执行并按用例输出结果 | agent 不负责环境管理，只对失败项负责 |
| [E2E CLI 实践](https://www.worldprogramming.org/posts/how-to-perform-end-to-end-testing-from-the-command-line-v1kgah) | 让测试运行器拥有应用生命周期，避免手工 start/stop | 一次启动、跑全部用例、`trap` 清理 |

共性：**环境生命周期脚本化，agent 只做「跑脚本 → 读结构化结果 → 修失败项」**。

## 3. 现状（已核实）

- 自测阶段已有 checklist 约束：逐条重跑验证命令，结果写 `selftest_log.json`，汇总写 `selftest.md`（`harness_providers/executor.py:480-487`）。
- 但每条验证命令自己管环境，没有「一次启动、跑完全部」的入口。
- 阶段契约为 `selftest.md`（`contracts.py`），没有结构化的测试结果契约。
- 项目模板目录 `packages/templates/` 只有 pipeline yaml 与 personas，没有测试脚本模板。
- 阶段级工具策略已存在（`StageContext.disallowed_tools`，见 2026-11-05 技术方案治理），可用于后续收紧，但本方案先不动工具权限。

## 4. 设计

### 4.1 固定 runner 模板

新增 `packages/templates/selftest/runner.sh`，编排层在自测阶段开始时落进 workspace（若项目已有等价脚本则跳过）：

```bash
#!/usr/bin/env bash
# 由 Sortie Deck 注入：一次启动服务 → 跑全部用例 → 输出结构化结果 → 清理
set -uo pipefail
CONFIG="${1:-selftest.config.json}"
START_CMD=$(jq -r '.start_command // empty' "$CONFIG")
HEALTH_URL=$(jq -r '.health_url // empty' "$CONFIG")
TEST_CMD=$(jq -r '.test_command // empty' "$CONFIG")
TIMEOUT="${SELFTEST_TIMEOUT:-120}"

SERVER_PID=""
cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT

if [ -n "$START_CMD" ]; then
  bash -c "$START_CMD" > selftest_server.log 2>&1 &
  SERVER_PID=$!
  # 轮询健康检查，替代 sleep + ps/lsof
  for _ in $(seq 1 "$TIMEOUT"); do
    curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && break
    kill -0 "$SERVER_PID" 2>/dev/null || { echo '{"status":"start_failed"}'; exit 2; }
    sleep 1
  done
fi

bash -c "$TEST_CMD" > selftest_run.log 2>&1
CODE=$?
python3 - "$CODE" <<'PY'
import json, sys
code = int(sys.argv[1])
json.dump({"status": "passed" if code == 0 else "failed", "exit_code": code,
           "log": "selftest_run.log"}, open("selftest_result.json", "w"))
PY
exit "$CODE"
```

配套 `selftest.config.json`（由 LLM 在首次运行时填写，后续复用）：

```json
{
  "start_command": "python3 -m snake --port 8000",
  "health_url": "http://127.0.0.1:8000/api/health",
  "test_command": "node e2e/selftest.mjs --json selftest_result.json"
}
```

### 4.2 Prompt 改造

自测阶段的 prompt 增加一段硬约束（替换 `_SELFTEST_CHECKLIST_RULES` 的「逐条重跑」）：

```text
Environment rules (mandatory):
- Run `bash scripts/selftest_runner.sh` ONCE to start the service and execute every case.
- Read `selftest_result.json`; only fix the failing cases listed there.
- After fixing, re-run the same script. Do NOT start/stop services by hand, do NOT use
  pkill/kill/sleep/ps/lsof — the runner owns the service lifecycle.
- If `selftest.config.json` is missing, fill it from the project's existing e2e entry
  (package.json scripts / Makefile / scripts/*.sh) instead of inventing new commands.
```

### 4.3 执行器注入

`harness_providers/executor.py` 的自测分支：

1. 若 `workspace/scripts/selftest_runner.sh` 不存在，从模板复制并 `chmod +x`（失败只告警）；
2. 若 `workspace/selftest.config.json` 不存在，仅提示 LLM 填写，不代填（避免猜错端口/命令）；
3. prompt 注入 4.2 的约束段。

### 4.4 结构化结果契约

- `selftest_result.json` 作为**可选产物**写入 `contracts.OPTIONAL`，不进硬校验，避免项目自带测试框架时被卡；
- `selftest.md` 仍是硬契约，内容改为「runner 结果摘要 + 修复记录」。

### 4.5 与现有 checklist 的关系

- `checklist.md` 继续作为编码阶段的工作清单；
- 自测阶段不再逐条执行 checklist 命令，改为「一次跑 runner」；checklist 只用于说明覆盖了哪些项；
- 这样避免同一批用例被逐条重跑 20 次。

## 5. 文件清单

| 文件 | 改动 |
|---|---|
| `packages/templates/selftest/runner.sh` | 新增 runner 模板 |
| `packages/templates/selftest/config.example.json` | 新增配置示例 |
| `src/sortie_deck/harness_providers/executor.py` | 自测 prompt 约束 + 模板注入 |
| `src/sortie_deck/contracts.py` | `selftest_result.json` 加入可选项 |
| `tests/test_selftest_runner.py` | runner 行为测试（起停、超时、失败码透传） |
| `CHANGELOG.md` | 行为变更记录 |

## 6. 备选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 只改 prompt（要求少起服务） | 改动最小 | LLM 仍会即兴发挥，无法保证 | 否 |
| B. 禁 Bash、强制只跑脚本 | 最彻底 | 项目没有脚本时直接卡死 | 否 |
| **C. 模板注入 + prompt 约束 + 结构化结果** | 可控、可解释、兼容已有测试框架 | 需要维护模板 | **推荐** |
| D. 编排层直接执行测试（不经 LLM） | 最快 | 失去「失败后修复」能力 | 作为未来 P3 |

## 7. 验收

- 单测：runner 能在 mock 服务上完成「启动 → 健康检查 → 跑用例 → 清理」，超时/启动失败返回非 0；
- 端到端：同一 mock 项目跑一次自测，工具步数从 241 降到 20 以内；
- 回归：没有 e2e 入口的项目仍能走原有 checklist 路径（模板注入失败只告警）。

## 8. 风险

1. 项目技术栈多样（Python/Node/Java），模板只能覆盖「有 start 命令 + 有健康检查」的常见形态；不满足时回退原流程。
2. 端口冲突仍可能发生，runner 需要支持 `PORT` 环境变量注入（配置项 `port_env`）。
3. 模板注入会改动 workspace，需要遵守现有产物边界（只写 `scripts/` 与 `selftest.config.json`）。

## 9. 状态

- [x] 方案确认（2026-11-05）
- [x] P1 实现（2026-11-05）：`packages/templates/selftest/runner.sh` + `config.example.json`、`executor.py` 自测 prompt 切换与模板注入、`selftest_result.json` 加入可选项；8 个 runner 用例（含真实起服务/端口清理）全部通过
- [ ] P2 实现（结果契约消费 + 前端展示；当前结果已落文件，前端尚未读取）
