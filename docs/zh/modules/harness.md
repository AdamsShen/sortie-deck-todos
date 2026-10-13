# Agent Harness

## 1. 概述

两层相关但不同的能力：

1. **进程内公用 Agent 循环** —— Sortie Deck 内部流程使用的循环
   （工具、轮次限制、确认门禁、瀑布流轨迹），参考 AWS **Strands / AgentCore
   harness** 思想，不绑定厂商 SDK。
2. **外部 Harness Provider**（新增）—— 系统级全局能力：接入外部 agent
   harness（Claude Code / OpenCode / Codex / Cursor），让一个任务的**全部阶段
   对话**都在其中运行。

| 项 | 值 |
|----|-----|
| 进程内循环 | `harness/__init__.py`（首个消费者：`mission_briefing.py`，`/api/briefing/sessions`） |
| Provider | `harness_providers/`（`base.py` 注册表 + 各厂商一个模块） |
| 阶段执行器 | `harness_providers/executor.py::HarnessStageExecutor`（`harness_stage`） |
| 任务记忆 | `mission_memory.py` → `data/missions/<id>/memory/` |
| 共享记忆 | `memory.py`（`MemoryStore` + 可选 mem0） |
| API | `GET /api/harnesses`、`PATCH /api/initiatives/{id}/harness`、HITL `switch_harness` |

## 2. Provider

```text
HarnessProvider(Protocol)
  probe() → ProbeStatus             # 可用性 / 凭据探测，TTL 缓存
  start_session() → HarnessSession  # 多会话
  send(session, prompt, cwd, context_pack) → HarnessReply
  close(session)
```

| 名称 | 接入方式 | 说明 |
|------|----------|------|
| `claude_code` | `claude-agent-sdk`（`query()` + `resume`） | 权限模式取 `TDT_CODING_PERMISSION_MODE`；需 `ANTHROPIC_API_KEY` |
| `opencode` | HTTP 调云端部署的 `opencode serve`（`TDT_OPENCODE_BASE_URL`） | 服务端接入，不起本地 CLI |
| `codex` | 云端容器内 `codex exec --json` | 会话续用 `codex exec resume` |
| `cursor` | `cursor-agent -p --output-format json`（云端容器） | `CURSOR_API_KEY` / `cursor-agent login` |
| `mock` | 内置回声 | 永远可用的回退 |

选择与锁定：

- 创建任务时选择（`CreateInitiativeRequest.harness`），默认
  `TDT_DEFAULT_HARNESS=claude_code`；注册表校验失败抛 `UnknownHarnessError`。
- `/start` 时锁定（`Initiative.harness_locked=True`）；草稿期可经
  `PATCH /api/initiatives/{id}/harness` 更换。
- 运行中更换必须经 HITL 授权（`switch_harness` 动作）—— 图把新 harness 写入
  流水线状态并重跑/重入该阶段。

## 3. 阶段路由

任务状态携带非空 `harness` 时，`graph._executor_name()` 对**所有阶段**返回
`harness_stage`。`HarnessStageExecutor` 随后：

1. 解析并探测 provider（TTL 缓存）；
2. bootstrap 任务上下文包（滚动记忆 + 绑定文件 + RAG 简报）；
3. 在按阶段新建的会话上发送该轮对话（工作目录 = 任务 worktree）；
4. 会话记录写入任务记忆，超 token 预算自动总结；
5. provider 不可用且 `TDT_HARNESS_FALLBACK=true` 时降级到既有阶段执行器
   （mock / 编码 CLI）；
6. 始终产出各阶段契约要求的产物（`contracts.REQUIRED`）。

## 4. 需求绑定记忆

`data/missions/<initiative_id>/memory/` 目录约定：

```text
memory.md                 滚动总结（自动压缩，LLM 回写）
sessions/<session>.jsonl  每个 harness 会话一份转录
context/*.md              需求绑定文件（brief.md / plan.md / harness.md 等）
```

新会话经 `bootstrap_context()` 自动加载全部内容；上下文包受
`TDT_MISSION_CONTEXT_TOKEN_LIMIT` 预算约束，超限时滚动记忆经
`LlmClient.chat_text` 总结后回写。

## 5. 共享数据层

`TDT_MEMORY_BACKEND=mem0` 时本地存储被包装为 `CompositeMemory`：mission 范围的
写入同步镜像到 mem0（`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY`），检索命中并入
上下文块 —— 以 `user_id = "<prefix>:<initiative_id>"` 做任务级隔离。RAG 检索
（`knowledge_gateway.retrieve`）结果同样注入 harness 提示。所有远端调用失败均
静默降级到本地。
