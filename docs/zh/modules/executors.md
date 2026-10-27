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
