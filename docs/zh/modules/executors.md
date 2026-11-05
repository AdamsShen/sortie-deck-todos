# 执行器与插件

## 1. 概述

将 `StageContext` 变成 `StageResult` 产物的阶段运行器；编码/发布提供者可替换。

| 项 | 值 |
|----|-----|
| 注册表 | `registry.py`、`plugins/factory.py` |
| 队列 | `ENG_QUEUE` |
| 实现 | `mock.py`、`coding.py`、`deploy.py` |

## 2. 协议

```python
async def run(self, ctx: StageContext) -> StageResult: ...
```

上下文含 brief、persona（含工具 brief）、上游产物文本、workspace、permissions。

## 3. 内置

| 名称 | 用途 | 说明 |
|------|------|------|
| mock_* | 演示 | 确定性产物 |
| mock_qa_cases / selftest / integrate / handoff | 标准轨 | |
| claude_code / cursor_cli | 工程实现 | worktree |
| deploy_shell | 发布 | `TDT_DEPLOY_CMD` |
| llm_product | 可选 PRD | 需 LLM Key |

解析：模板 executor 字段，否则 `graph._executor_name` 按 coding/deploy 配置。

## 4. 技术方案（目标）

- `probe()` 健康检查
- 日志流式进频道
- shell 沙箱档案
- `sortie_deck.executors` 社区入口

## 5. 开发计划

### P0（2–3 天）

| 任务 | 验收 |
|------|------|
| README：Claude/Cursor 环境 | 可照做 |
| 注册列表测试 | pytest |
| CLI 缺失时软失败 | 明确 message |

### P1（1–2 周）

| 任务 | 验收 |
|------|------|
| 日志流挂钩 | 小队可见 |
| `/api/executors` 带 probe | status 字段 |
| 编码执行器调 MCP | 开关 |

### P2（2 周）

| 任务 | 验收 |
|------|------|
| entry points 发现插件 | 可外载（`sortie_deck.executors`；示例 `packages/examples/demo_executor`） |
| `/api/executors` probe | 已返回 `probes` |
| 时限/资源限制 | 待续 |

## 6. 测试计划

- mock 满足契约；队列并发；缺二进制不崩

## 7. 依赖

- Graph、Toolkit brief、Artifacts、Settings

## 8. 上下文成本控制

单个阶段的长跑成本近似等于「模型请求数 × 单次上下文」。2026-10-27 实测某阶段 196 轮、
单次 input 中位 28.7 万 token，因此执行器侧做了三件事（互相正交，可独立回退）：

### 8.1 上游产物只给摘要 + 路径

`artifact_summary.py` 为每份上游产物生成 `ArtifactBrief`（文件名 / 绝对路径 / 字符数 / 摘要），
prompt 里只列 brief，不再注入全文；agent 需要细节时用 Read 打开路径。

- 短产物（`TDT_PROMPT_INLINE_ARTIFACT_CHARS`，默认 1200 字符）直接内联，不为小文件多付一次摘要调用
- 其余并发调 LLM 摘要（`TDT_PROMPT_ARTIFACT_SUMMARY_TIMEOUT`，默认 12s），失败/超时/未配置 LLM 时回落规则摘要
- `diff.patch` 一律规则摘要（文件清单 + 增删行统计），不送 LLM
- 摘要按「路径 + 内容 sha1」缓存在 `data/missions/<id>/memory/artifact_summaries.json`，同一产物跨阶段零额外调用
- 回退开关 `TDT_PROMPT_ARTIFACT_BRIEFS=false`：恢复旧的 PRD 全文注入

`StageContext` 因此新增 `upstream_paths`（与 `upstream_artifacts` 同键的绝对路径映射），
`upstream_artifacts` 的全文语义保持不变（consult / mock / diff 仍依赖它）。

### 8.2 MCP 按需挂载

`harness_providers/mcp_binding.py` 把「本阶段绑定 + 阶段主动召回 + `toolkit_pick` 上浮」解析出的
MCP 条目转成 SDK `mcp_servers`，并置 `strict_mcp_config=True`，不再从本机 `~/.claude.json`
全量加载全局 mcpServers。

- 未绑定即「本阶段无 MCP」，工具 schema 不进上下文
- harness 会话启动前确定集合；阶段中途 pick 的 MCP 从下一阶段生效（协议说明里已注明）
- coding CLI（claude）落盘 `.tdt/mcp-config.json` 并传 `--mcp-config` + `--strict-mcp-config`；
  cursor CLI 无该参数，日志里标注未挂载
- 回退开关 `TDT_HARNESS_MCP_STRICT=false`：恢复 CLI 默认的全量加载

### 8.3 会话内自动压缩

`claude_code` provider 通过 `ClaudeAgentOptions.env` 注入
`CLAUDE_CODE_AUTO_COMPACT_WINDOW=<TDT_HARNESS_AUTO_COMPACT_WINDOW>`（默认 200000，
官方有效区间 100K–1M）；coding CLI 子进程同样带该环境变量。

- `compact_boundary` 事件写入 `StageResult.meta["context"]`（`compacted` / `at_token` / `post_token` / `trigger`）
  并打一条 info 日志，工作台据此判断该阶段是否压缩过
- 同时回填 `num_turns` / `usage` / `cost_usd` / `stop_reason`
- `TDT_HARNESS_MAX_TURNS`（默认 0 不限制）可兜住「反复打磨不收敛」的阶段

### 8.4 单条消息缓冲上限

`claude-agent-sdk` 默认只接受 1 MiB 的单条 CLI 消息（NDJSON），超限抛
`SDKJSONDecodeError` 并让整个阶段失败。编码 agent 用 `Read` 打开截图时，图片会 base64
进消息（`tool_use_result` 里还会再带一份），379 KB 的 Playwright 截图即可触发
（2026-11-05 实测：`eng_implement` 因此整段作废，返工约 17 分钟）。

- `ClaudeCodeProvider` 通过 `ClaudeAgentOptions.max_buffer_size` 传入
  `TDT_HARNESS_MAX_BUFFER_BYTES`（默认 10485760 = 10 MB，官方 cookbook 对图片场景的建议值；
  `<=0` 回退 SDK 默认 1 MiB）
- 溢出时 `HarnessReply.meta` 带 `buffer_overflow=true` / `max_buffer_size`，日志记录
  initiative / stage / session 与异常栈，`reply.md` 附带调参提示

### 8.5 QA 浏览器复验 runner

QA 阶段会注入 `scripts/qa_browser_runner.mjs`（项目已有同名文件则不覆盖）。它把
「起服务 → 健康检查 → 无头 Chrome 跑场景 → 截图 → 清理」一次做完，只依赖 Node 内置
WebSocket（Node ≥ 21），不装 npm 包；找不到浏览器时返回 `browser_unavailable`，模型退回 HTTP 级检查。

- 场景写在 `qa.config.json`：`start_command` / `base_url` / `health_url` / `scenarios`；
  单步支持 `click` / `type` / `key` / `expect_visible` / `expect_hidden` / `expect_text` /
  `screenshot` / `wait_ms`（示例见 `packages/templates/qa/config.example.json`）
- 结果写 `qa_result.json`（每条场景的步骤结果、文档状态码、console / pageerror / 4xx 列表），
  截图落 `qa_screenshots/`；`favicon.ico` 404 视为噪音
- prompt 硬约束：只跑同一个脚本、只修失败项，禁止手工起停服务，也禁止再手写 CDP/Playwright 脚本
- **用例边界**：每个场景必须带 `case_id`（如 `TC-X-01`），且该编号必须出现在 `test_cases.md`
  （默认依次找 `.upstream/test_cases.md` / `artifacts/qa_verify/test_cases.md` / `test_cases.md`）。
  边界外的场景不执行、不计入判定，只在 `scope.skipped_cases` 留痕；没有用例文件时不限制（保手工调试）
- **验证预算**：`max_runs`（默认 5）跨进程累加在 `.qa_runner_state.json`；超出后退出码 5、写
  `qa_budget.json`，**保留上一轮 `qa_result.json`**，提示按现状收尾
- **只回归失败项**：返工阶段用 `--cases TC-X-01,TC-X-02` 只跑 QA 判失败的用例，不重跑全量
- **阶段超时**：复验类阶段（`qa_verify` / `*_verify`）单独取 `TDT_QA_STAGE_TIMEOUT_SECONDS`
  （默认 900s），不再沿用全局 1800s；编码阶段仍是 3600s

## 9. 多镜头自审与实现清单

方法论借自 autopilot-harness（MIT），但**不安装其 CLI、不写宿主 hooks**：一轮 lens 就是同一
provider 会话上的下一次 `send()`，由编排层驱动，因此 claude_code / codex / cursor / opencode
行为一致；mock 与 CLI 单发模式走降级（详见 `docs/plans/2026-11-02-多镜头自审方案与改动说明.md`）。

### 9.1 计划 → 清单

`*_tech_design` 阶段除 `tech_design.md` 外要求产出 `checklist.md`：

```markdown
- [ ] C1 <一项具体改动> — files: `src/x.py` — verify: `pytest tests/test_x.py -q` (expect exit=0)
```

编码阶段把它注入 prompt：一次做一项、完成后回写 `[x]`、**真跑** verify 命令并把
`item / command / exit_code / tail` 追加进 `selftest_log.json`；自测阶段只允许重跑，禁止新增用例。
`checklist.md` / `selftest_log.json` 是可选产物（`contracts.OPTIONAL`），缺失不影响契约校验。

没有上游清单时（express 轨没有技术方案阶段），编码阶段会先要求写一份 3-8 项的 `checklist.md`
再逐项执行，保证「逐项 + 可执行验证」对所有航线生效。

### 9.2 多镜头自审

编码/自测阶段收尾时在同一会话续跑 N 轮，每轮只换视角：

| 轮次 | 视角 | 允许改代码 |
|---|---|---|
| 1 | 正确性与不变量（对照 acceptance.json） | 是 |
| 2 | 空值、边界与错误路径 | 是 |
| 3（默认最后一轮） | 测试缺口（只记录） | **否** |
| 4 / 5（`rounds>=5`） | 并发与部分失败 / 安全与信任边界 | 是 |

模型必须在回复结尾给出一个 ```review 围栏块：`status` / `reviewed` / `findings` / `summary`。
编排层只认结构化结论；缺块记 `unparsed`（降级，不判失败）。

### 9.3 自审日志（审查了什么 / 结果 / 状态）

| 文件 | 内容 |
|---|---|
| `missions/<id>/logs/<stage>/review.jsonl` | 逐轮追加，崩溃也保留已完成轮次 |
| `missions/<id>/logs/<stage>/review.json` | 汇总：scope（改动文件/契约/上游/清单）+ 每轮 status/findings/耗时 + verdict |
| `missions/<id>/artifacts/<stage>/review.md` | 报告视图，进阶段产物供门禁与下游阅读（仅真正执行轮次时产出；`skipped` / `prompted` 只留日志） |

`StageResult.meta["review"]` 同时带精简视图（`verdict` / `rounds` / `blocking` /
`degraded` / `statuses` / 日志路径）。

结论有 `blocking` 且 `TDT_REVIEW_BLOCKING_POLICY=hitl`（默认）时阶段转 `needs_hitl`；
`fail` 直接失败，`ignore` 只记录。

### 9.4 开关

| 配置 | 默认 | 说明 |
|---|---|---|
| `TDT_REVIEW_LENSES_ENABLED` | `true` | 关闭即回旧行为 |
| `TDT_REVIEW_LENS_ROUNDS` | `3` | 3 轻量 / >=5 全量（4 按轻量） |
| `TDT_REVIEW_LENS_STAGES` | `build,selftest` | 逗号分隔，可加 `integrate` |
| `TDT_REVIEW_LENS_TIMEOUT_SECONDS` | `300` | 单轮超时，只降级该轮 |
| `TDT_REVIEW_BLOCKING_POLICY` | `hitl` | `hitl` / `fail` / `ignore` |
