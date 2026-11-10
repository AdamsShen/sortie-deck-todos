# DSH 沙箱自动探测/回退（方案 B）设计与统一 launcher（方案 C）影响评估

- 日期：2026-11-10
- 状态：**已实施并上线（M1–M5 完成，2026-11-10）**
- 关联：`docs/plans/2026-11-09-dsh-harness-provider.md`、`docs/plans/2026-11-10-三家沙箱隔离对齐.md`、`docs/plans/2026-11-09-权限阻断与环境阻塞熔断.md`

## 1. 需求背景

云端任务 `ini_d8ff9eb21cb3`（harness=`dsh`，标题「实现一个可运行的贪吃蛇小游戏，并支持用户登录和注册」）出现「回退到开发中」：

| 时间（北京） | 事件 |
|---|---|
| 17:00:54 → 17:08:59 | eng 阶段因沙箱不可用升级真人（`needs_hitl`），请求授权 `danger-full-access` |
| 17:14:29 → 17:21:46 | 批准后重入 eng 完成 |
| 17:21:46 → 17:27:15 | qa_verify 完成，`status=ok` 但 `qa_passed=false` |
| 17:27:20 | 门禁 `recommended_action=reroute → eng_implement`，点 approve 后按 `on_fail` **打回开发中** |
| 17:27:20 → 17:34:44 | 第二轮 eng 无效重跑 |
| 17:34:44 → 现在 | 第二轮 qa_verify，报告仍 `passed=false / status=blocked / rework_required=false` |

QA 报告原文：「打回 build 无法修复『没有执行环境』，只会重复产出 `not_run`；建议升级裁决而非自动回环」。

用户设想：「if harness 发现沙箱不可用时，自己把 PERMISSION_MODE 改成完全访问权限继续执行；可用就走沙箱，沙箱里命令不用审批」。我给出 A（静态配置止血）/ B（自动探测回退）/ C（统一 launcher 根治）三档，**用户最终选择 B**，并要求把 C 的影响评估落盘到文档。

### 1.1 本轮明确的边界（含对未答问题的合理假设）

1. **只做 B**：范围限定 DSH provider 的自动探测/回退，含开关、审计、缓存、单测；目标「今天能落地止血」。不做 C。
2. **生效环境**：假设「公网云端可开」，否则对本次云端阻塞无效。但开关默认 `false`；命中必须 `WARNING` + `reply.meta` 标记；文档必须明写「外层 launcher 未落地时属权限面放大，是临时止血，不是安全默认」。
3. **QA 归因修复不纳入本期**（`status=blocked → environment_blocked`），但要在「已知风险」标注：回退失败或环境修不好时仍可能返工，需另开任务。
4. **云端部署**：含「改云端 `.env` 开关 + 按既有流程发布 + 停空转任务 + 一次验收」作为实施里程碑；停任务/重启 worker 是写操作，执行前需用户确认。

## 2. 业内调研（来源）

| 来源 | 结论 |
|---|---|
| DSH 官方 README（随 `deepseek-harness-runtime-bin` 安装，`~/.dsh/profiles/node_modules/@deepseek-ai/dsh-*`） | `ApprovalPolicy` 只有 `ask`/`never`，`never` 是「拒绝前不再询问」；`danger-full-access` deliberately bypasses `ctx.sandbox`；`sandbox-policy` 只有 `mode` + 单 `workspaceRoot`，不支持额外可写根 |
| Claude Agent SDK 官方权限模型 / Codex CLI `sandbox_mode`（见 `docs/plans/2026-11-10-三家沙箱隔离对齐.md` §4.1/§4.2） | 三家机制不统一；进程级沙箱只是第一层，真正的多租户隔离落在容器/VM |
| SWE-bench PR #594 <https://github.com/SWE-bench/SWE-bench/pull/594>、OpenHands benchmarks <https://github.com/OpenHands/benchmarks/blob/main/benchmarks/swebench/README.md> | 基础设施失败要单独分类；**预检只做告警不做成败判定**，避免误伤 |
| 本项目 `docs/plans/2026-11-09-权限阻断与环境阻塞熔断.md` §2.5 | 公网多用户环境扩大权限面必须配 deny 规则 + 开关，不能默认全局放开；不可恢复错误应暂停交人，不自动回环 |
| 实测（2026-11-10） | 本地 macOS `danger-full-access` 下 bash 可写 workspace 外且无需审批；云端内核 `6.8.0-136-generic`、Landlock ABI v4 可用、`seccomp/apparmor=unconfined`，但 linux 运行时 wheel 缺 landlock runner → `SANDBOX_UNAVAILABLE / no usable backend` |

### 2.1 两个必须先澄清的语义

- `DSH_PERMISSION_MODE` 在**进程启动**时由 profile 的 `!!js process.env...` 解析（`sandbox-policy.mode`、`approval.policy`：`danger-full-access → never`，其余 `ask`）。SDK 未暴露 `setSandboxMode` RPC，因此「发现不可用再改」只能是**轻量探测 → 关当前运行时 → 用 `danger-full-access` 重启该轮**，不是热切换；冷启动实测约 0.73s。
- `never` **不是自动放行**；`workspace-write` 下 workspace 内的 bash 本来就无需审批（本地 DSH 任务 62 次工具调用实证），只有越界/escalation 才需审批且无应答者时 fail closed。所以用户设想里「沙箱环境中命令都可以使用不用审批」这一半不成立：**B 只做「沙箱后端不可用时回退到无沙箱模式」，不做「沙箱内免审批」**。

## 3. 方案 C（统一 launcher）影响程度评估 —— 本轮要求落盘的内容

### 3.1 命名澄清

我表格里的「C」= `docs/plans/2026-11-10-三家沙箱隔离对齐.md` 的 **§5.1 方案 A 统一 launcher**；该文档内部另有「方案 B 每任务一容器」「方案 C 最小加固」，是另一套编号，勿混淆。该 launcher 目前**尚未落地**（`src/sortie_deck/sandbox_launcher.py` 不存在）。

### 3.2 工作量：约 2–3 周（单人）

按文档 §8 的 P0–P4，结合现有代码基础重估：

| 阶段 | 内容 | 主要改动 | 估算 |
|---|---|---|---|
| P0 | env 白名单 + per-run home + `runs/<id>` 700 | `permissions.py`、三个 provider 的 home/env、compose | **1–2 天**，可独立先上、**不碰 Claude 沙箱** |
| P1 | `resolve_sandbox_policy()` + pack 扩字段 + capability 表/warn | `permissions.py`、`executor.py` 的 context_pack、三个 provider、`types.ts` | 2–3 天 |
| P2 | launcher（mount ns + env 过滤）+ 三家接线 | **新增 `sandbox_launcher.py`**、`codex.py`、`dsh.py`、`claude_code.py`、compose | **3–5 天**，风险最高 |
| P3 | egress 代理 + 域名白名单 | 新增代理 + 健康检查 + 显式降级开关 | 3–5 天 |
| P4 | `test_sandbox_parity.py` 九条用例 × 三家 | 新测试矩阵 | 2–3 天 |

已具备的基础能省 P0/P1：`Dockerfile:84-87` 已装 `bubblewrap`/`socat`；云端 `/api/health` 实测 `bwrap:true, socat:true, config_present:true, permission_mode:acceptEdits, env:production`；`permissions.py` 已有 `resolve_permission_mode` / `resolve_sandbox_mode` / 两个 preflight；`context_pack["sandbox"]` 已透传并被 dsh/codex 消费；已有 550 行相关测试（`test_sandbox_settings.py` 233 / `test_permissions.py` 135 / `test_env_block_pause.py` 182）。

### 3.3 未决技术风险（文档 §7/§9）

1. **bwrap 嵌套退化**：Claude 现用 `enableWeakerNestedSandbox: true`，外面再套一层要实测，可能退化成「launcher 只做目录权限、bwrap 仍归 Claude」，达不到三家机制一致。
2. **双层规则冲突**：外层放行、内层拒绝，难排查。
3. **代理成为单点**：网络全走代理，代理挂 = 所有任务断网，必须健康检查 + 显式降级开关（降级要打 warn）。
4. `seccomp=unconfined` 是仅为 Claude bwrap 放的（`docker-compose.yml` worker 段注释），三家都进 bwrap 后需重评。
5. §9 四条未决：选 A 还是只写文档差异 / Codex 是否改 `danger-full-access` / 域名白名单维护责任 / P0 是否立即做。

### 3.4 对现有 Claude Code 沙箱的影响：**会，且是当前最敏感的部分**

`docker/claude-sandbox.json` 经 `claude_code.py:230-237` 的 `settings=` 注入，是三家唯一生效的 OS 级边界（云端 `config_present:true`）。

| 现有能力 | C 的动作 | 风险 |
|---|---|---|
| 静态 `claude-sandbox.json`（denyWrite / allowWrite / allowedDomains / credentials.envVars） | 降级为默认模板，改按 run 生成 `runs/<id>/.sandbox.json`（§5.1 决策 1） | 迁移漏一项即改变 Claude 行为 |
| Claude 自己的 bwrap 一层 | 外面再套 launcher bwrap | 嵌套退化 / sandbox unavailable |
| 网络由 Claude `network.allowedDomains` 管 | 上移到 launcher 层代理（§5.1 决策 4） | 代理单点，挂了影响所有任务 |
| compose `seccomp/apparmor=unconfined` | 需重评是否收紧 | 收紧过头会让 Claude 沙箱起不来 |
| `permission_mode=acceptEdits`（云端实测） | 任务级策略统一后可能变化 | 需全量回归 |

**结论**：不是「加一层不影响」。它要动 Claude 目前唯一生效的沙箱配置来源、启动链和网络出口；收益是三家最终等价、DSH 终于有边界，代价是这条已跑通的路径要重新验证（9 条 parity 用例 + 现有 550 行测试 + 云端回归）。

### 3.5 B vs C 影响面对比（选择 B 的依据）

| 维度 | B（本期） | C |
|---|---|---|
| 改动面 | 仅 `dsh.py` + `settings.py` + 测试 | 三家 provider 启动方式 + compose + 网络出口 + 凭据注入 |
| Claude/Codex 现有路径 | **零改动** | 全部要接线、要回归 |
| 失败影响范围 | 只影响 DSH 阶段 | 所有 harness 的任务都可能起不来/断网 |
| 灰度 | 开关默认关，天然双轨 | 需额外维护两套启动逻辑 |
| 今天云端 DSH 阻塞 | 直接解决 | 不能，2–3 周后才见效 |

**建议**：先 B 止血；C 的 P0 单独先上（1–2 天、不动 Claude 沙箱），P2/P3 单独排期做双轨灰度。

## 4. 方案 B 技术设计

### 4.1 目标 / 非目标

- 目标：DSH 在 `workspace-write` 后端不可用时，**经显式开关授权后**自动回退到 `danger-full-access` 完成该轮；沙箱可用时完全走原路径，零行为变化。
- 非目标：不做「沙箱内免审批」；不改 `approval.policy` 语义；不改 QA 归因与 `on_fail` 路由；不引入统一 launcher；不修 SDK linux wheel 缺 runner 的上游问题。

### 4.2 配置（`src/sortie_deck/settings.py`，挂在 DSH 配置块 `dsh_permission_mode` 附近）

| 字段 | 默认 | 说明 |
|---|---|---|
| `dsh_sandbox_auto_fallback` | `False` | 总开关；`TDT_DSH_SANDBOX_AUTO_FALLBACK` |
| `dsh_sandbox_probe_ttl_seconds` | `600.0` | 探测结果进程级缓存 TTL；0 = 每轮探测 |
| `dsh_sandbox_probe_timeout_seconds` | `30.0` | 单轮探测超时（复用 `_request_timeout()` 的下限保护） |
| `dsh_sandbox_probe_prompt` | `用 bash 执行 \`echo __dsh_probe__\`，只回答 ok。` | 固定轻量 prompt，避免注入任务内容 |
| `dsh_sandbox_unavailable_hints` | 内置元组 | `SANDBOX_UNAVAILABLE` / `no usable backend` / `no approval channel` / `requires approval` / `Operation not permitted` |

探测结果缓存键：`(platform.node(), sdk 版本, 初始 mode, cwd 是否存在)`，进程级 dict + 时间戳 + `threading.Lock`（并发阶段只探一次）；借鉴 `permissions.py:159-176` 的 `_preflight_state` 范式，但保留 TTL 便于环境修好后自动恢复。

### 4.3 探测与回退流程

```text
send(session, prompt, ...)
  ├─ 解析 sandbox_mode（已有 _sandbox_mode_from_pack；显式 full_access → 直接跳过探测）
  ├─ 开关关闭 → 走现有 _run_once（零变化）
  └─ 开关开启且初始 mode 不是 danger-full-access
       ├─ 查缓存：命中「可用」→ 现有 _run_once
       ├─ 命中「不可用」→ 直接以 danger-full-access 跑该轮
       └─ 未命中 → 探测轮（workspace-write + 固定 prompt，独立 dsh_home/_probe，不复用任务会话）
            ├─ 识别到 unavailable 特征 → 缓存「不可用」→ WARNING → 关进程 → 用 danger-full-access 重跑该轮
            ├─ 未识别（命令成功）→ 缓存「可用」→ 现有 _run_once
            └─ 探测超时/异常 → 缓存「可用」+ warn（保守：不因探测失败而提权）
```

关键约束：

- 探测轮使用**独立 session id 与独立 `dsh_home/_probe`**，绝不污染任务会话与 `native_id`；探测轮不写产物、不挂 MCP、不传 `on_activity`。
- 回退只影响**当前这一轮**的 `env["DSH_PERMISSION_MODE"]`，不改 `settings`、不改任务级 override、不写 `.env`。
- 回退后 `HarnessReply.meta` 增：`permission_fallback=True`、`probe_reason`（截断 200 字）、`effective_mode="danger-full-access"`、`probe_cached=bool`。
- 同一进程内回退成功后缓存「不可用」，后续轮直接走回退，不重复探测；TTL 到期重新探测一次（环境修好后自动回到沙箱）。
- 复用现有 `send()` / `_run_once()` 参数链与 patch 目录清理，避免重复建目录；`threading.Timer` 看门狗对探测轮同样生效。

### 4.4 审计与日志

- 回退命中：`logger.warning` 带 `initiative_id / stage / session / reason / elapsed / effective_mode`（禁止打印密钥）。
- 探测失败、缓存过期重探、开关关闭但检测到不可用：分别 `debug/warn`，符合「降级与兜底分支必须记日志」。
- 探测轮自身的异常按 `warn` 收敛，不抛给编排层；任何失败都退回原模式。

### 4.5 安全护栏

1. 开关默认 `false`；文档与 CHANGELOG 明写「公网多任务共享 `/data` 且外层 launcher 未落地时开启 = 放大权限面，属临时止血」。
2. 与已有 `TDT_PERMISSION_DENY_RULES` 并列说明：DSH 侧暂无等价 deny 能力，回退后 DSH 的写边界完全依赖容器隔离。
3. 探测结果**只缓存可用性事实**，不缓存凭据、不落盘。
4. 回滚：关开关即恢复 `workspace-write`；无需改代码、无需清数据。
5. 云端建议在单租户/内网窗口开启，并在验收后评估是否关闭。

### 4.6 文件清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/harness_providers/dsh.py` | 新增 `_probe_result()` / `_run_probe()` / `_fallback_decision()` / `_probe_cache`；`send()` 增加探测分支；`_run_once()` 增 `effective_mode` 参数并透传 `meta`；复用 `_open_session()`、看门狗、patch 清理 |
| `src/sortie_deck/settings.py` | 新增 5 个 `dsh_sandbox_*` 配置 |
| `tests/test_harness_dsh_sandbox.py` | 新增：开关关闭零探测、可用零回退、命中回退、缓存复用/TTL 失效、探测超时保守、显式 full_access 跳过、meta 字段、审计日志、独立会话不污染 |
| `docs/zh/modules/harness.md`、`docs/en/modules/harness.md` | 增加「沙箱自动回退」小节：开关、语义、风险、`never` 不是自动放行 |
| `docs/plans/2026-11-10-dsh-sandbox-auto-fallback.md` | 本文档（含 C 影响评估、验收记录） |
| `CHANGELOG.md` | Unreleased/Added 一条 |
| `.env.example`、`.env.docker.example` | 补 `TDT_DSH_SANDBOX_AUTO_FALLBACK=false` 等说明 |

关键接线点（已核实）：`dsh.py:96` `_permission_mode(override)`、`dsh.py:110` `_sandbox_mode_from_pack()`、`dsh.py:410` `send()` 取 `sandbox_mode`、`dsh.py:480-501` `_run_once()` 设 `env["DSH_PERMISSION_MODE"]`；`settings.py:164` `dsh_permission_mode`；`permissions.py:114/148/159/162` 预检与缓存范式；`executor.py:1998` `context_pack["sandbox"]["mode"]`。

### 4.7 测试与验收

单测（fake `deepseek_harness`，复用 `tests/test_harness_dsh.py` 的 `_FakeHarness` 范式）：

1. 开关关闭：`_run_probe` 零调用、`env` 保持 `workspace-write`；
2. 沙箱可用：探测轮成功、无回退、`meta` 无 `permission_fallback`；
3. 命中不可用：出现 `WARNING`、`effective_mode=danger-full-access`、业务轮成功、`native_id` 正确；
4. 缓存：第二轮不重复探测；TTL 过期重探；
5. 探测异常/超时：保守走原模式 + warn；
6. 任务级显式 `full_access`：跳过探测、直接生效；
7. 探测轮不污染任务会话、不写产物、不改 `settings`；
8. 全量回归：`pytest -q`、`ruff check`、`ruff format --check`。

真机/云端验收（实施阶段，需用户确认后执行）：

- 本地 macOS：开关开启，人为构造 `no usable backend`（或用 stub）验证回退；关闭开关验证沙箱路径不变；
- 云端：改 `.env` 开关 → 按既有发布流程发镜像 → 停掉 `ini_d8ff9eb21cb3` 空转任务 → 新起同题 dsh 任务，确认 QA 能跑 `npm test`/浏览器用例、`test_report.json` 不再是 `blocked`、日志有回退 WARNING 且 `reply.meta.permission_fallback=true`。

## 5. 备选方案

| 方案 | 不采用原因 / 适用 |
|---|---|
| A 静态 `TDT_DSH_PERMISSION_MODE=danger-full-access` | 零代码、立即生效，但环境差异复发时需人工再改；作为本次的**临时兜底**保留在文档中 |
| C 统一 launcher | 2–3 周、要动 Claude 现有沙箱链，不在本期（影响评估见 §3） |
| QA 归因修复 `blocked → environment_blocked` | 能根治返工循环，但超出「只做 DSH」范围，另开任务 |
| 上游修 SDK linux wheel 缺 landlock runner | 根因修复，但依赖上游发布，不可控 |

## 6. 已知风险与后续

1. 回退失败或环境修不好时，QA 仍会按 `qa_passed=false` 打回开发（`graph.py:2809-2812`、`graph.py:987-990`）；需另开任务修归因。
2. 回退后 DSH 写边界依赖容器隔离；外层 launcher 未落地时不得视为安全默认。
3. 探测轮多一次模型调用与约 0.7s 冷启动，靠进程级缓存摊薄。
4. `never` 语义不变，不实现「沙箱内免审批」。
5. 云端 `ini_d8ff9eb21cb3` 仍在 `qa_verify` 空转，实施前需用户确认是否停止。

## 7. 状态与实施记录（2026-11-10）

| 里程碑 | 结果 |
|---|---|
| M1 配置 + provider | ✅ 新增 5 个 `dsh_sandbox_*` 配置；`dsh.py` 增探测/缓存/回退/审计；`runtime_meta` 让异常兜底分支也保留审计字段 |
| M2 单测 | ✅ `tests/test_harness_dsh_sandbox.py` 16 条；全量 `pytest -q` **1209 passed**；改动文件 `ruff check` / `format --check` 全过 |
| M3 文档 | ✅ 中英文 `harness.md` §2.3、`.env.example`、`.env.docker.example`、`CHANGELOG`、本文档 |
| M4 本地真机 | ✅ 真实 SDK 三条路径：开关关闭 `mode=None / 零探测`；开关开且沙箱可用 `workspace-write / 不回退`；模拟不可用 `danger-full-access / WARNING / permission_fallback=true` |
| M5 云端发布 | ✅ 见 §7.1；公网 `/api/health` 200、`dsh=ok`，容器内真实一轮验证回退与 bash 可执行 |

### 7.1 M5 云端发布记录（2026-11-10 19:50 UTC+8 前后）

发布前置：队列 52 `done` / 2 `failed`，无 running job，`initiative_locks` 为空；门禁任务
`ini_d8ff9eb21cb3` 此时已因「连续 3 轮 sandbox blocked」自行升级为 `waiting_hitl`，
**没有活跃 job**，因此无需停任务，该任务状态全程保留未动。

| 步骤 | 结果 |
|---|---|
| 备份 `.env` | `.env.bak-20261110-195028-dsh-fallback` |
| 开开关 | `TDT_DSH_SANDBOX_AUTO_FALLBACK=true`（`.env:52`） |
| 快进代码 | `git merge --ff-only origin/dev` → `0097da5`（9 files，+962/-19） |
| 重建镜像 | `DOCKER_BUILDKIT=1 docker compose build api`，exit 0 |
| 重建容器 | `docker compose up -d --no-deps --force-recreate api worker` + `docker restart sortie-caddy` |
| 公网验证 | `/api/health` = ok / version 0.5.0 / production；`/api/harnesses?refresh=true` 中 `dsh=ok` |
| 容器内配置 | `dsh_sandbox_auto_fallback=True`、TTL 600s、探测超时 30s、初始 `workspace-write` |
| 真机验收 | 容器内跑一轮 `DshProvider.send`：`STATUS ok / MODE danger-full-access / FALLBACK True / REASON sandbox backend / PROOF FALLBACK_OK`，即 bash 确实执行成功，WARNING 审计正常 |
| 清理 | 验收临时目录 `/tmp/dsh-accept-*` 已删除，worker 近 5 分钟无 ERROR |

**发布副作用（需运维跟进）**：`codex` 由 `ok` 变为 `auth_required`
（`/home/sortie/.codex/auth.json` 位于容器可写层，`--force-recreate` 后丢失，探针无
`OPENAI_API_KEY` / `CODEX_API_KEY` 兜底）。与 B 方案代码无关；恢复方式：重新
`codex login` 并持久化该目录，或显式配置 API key。`claude_code` / `dsh` / `mock` 不受影响。

**遗留**：`ini_d8ff9eb21cb3` 仍停在 `qa_verify` 的 `waiting_hitl`（保留未动）。
它此前 3 轮 QA 产物都是环境阻塞，需要在页面上选择重跑/放行，或手动停止；由用户决定。

### 实施中发现并修正的一处误判

首轮真机验证里「开关开·沙箱可用」被误判成不可用并回退，命中的是 `sandbox: file access denied`。
排查：探测轮的 `system/message` / `request/context` 事件本来就会注入沙箱策略说明，
扫全量事件时这些静态文案被当成错误。修正：

- `_probe_unavailable_reason()` 只扫 `tool/result` / `turn/end` / 回复正文；
- `tool/result` 里命令确实跑出结果（`isError` 非真且 `content` 非空）时直接短路判「可用」；
- 补回归单测 `test_probe_success_short_circuits_policy_text` 锁住该行为。

### 与设计的两处工程偏离

1. 探测轮 `dsh_home` 用 `home/_probe/<uuid>` 且跑完 `shutil.rmtree` 删除，避免 probe 会话在任务 home 下累积（设计只要求「独立」）。
2. `send()` 增加 `runtime_meta` 出参，把 worker 线程里生成的审计字段回传给异常兜底分支；否则业务轮抛异常时 `reply.meta` 会丢掉 `permission_fallback`，线上无法判断是否提过权。
