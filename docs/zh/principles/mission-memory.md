# 任务绑定文件记忆（mission_memory）原理

## 1. 定位：stage 间的上下文接力棒

一个任务（initiative）启动后 harness 锁定（`models.py` 的
`harness_locked`，启动时在 `orchestrator.py` 的 `start_pipeline` 中置位），
native session 的 `resume` 能力只在**单个 stage 内**有效；进入下一个 stage
就是一个全新 session。`mission_memory.py` 的核心职责是：**把上一个 stage
的上下文带进下一个 stage 的全新 session**，并覆盖进程重启后的恢复。

它不是 harness 原生会话记忆的替代品，而是横跨其上的桥接层：

- 会话内连续性：由 harness 原生承担（Claude Code SDK `resume`、
  Cursor `--resume`、Codex `exec resume`）。
- 跨 stage / 跨重启连续性：由 mission_memory 承担。
- 罕见例外：HITL `switch_harness` 人工授权中途换 harness（`orchestrator.py`
  的 `switch_harness_via_hitl`，`graph.py` 三处 HITL 分支），换完后从当前
  stage 重跑，此时上下文同样靠 mission_memory 传递。

## 2. 数据模型

每个任务目录（`TDT_DATA_DIR/missions/<initiative_id>/memory/`）：

| 文件 | 内容 | 产生方式 |
|------|------|----------|
| `memory.md` | 滚动总结 | 每个 stage 后自动追加一条；超 token 预算时 LLM 压缩重写 |
| `sessions/<sid>.jsonl` | 每 stage 会话转录 | 每次 harness 执行后追加一行 JSON |
| `context/<name>.md` | 需求绑定文件 | 任务启动与编排节点写入 |

全部是明文文件——重启安全、harness 无关、所有 provider 共享。

## 3. 数据流

### 写入路径（3 个时机）

1. **任务启动**：`start_pipeline` 锁定 harness 的同时调
   `_bind_mission_context_files`，写 `brief.md`（任务简报）、
   `plan.md`（阶段流水线）、`harness.md`（锁定声明）。失败静默，
   不阻塞启动。
2. **每个 stage 执行完成后**（`executor.py` 的 `_run_harness`）：
   - `record_turn` → `sessions/<sid>.jsonl` 追加一条（时间、stage、role、
     prompt/reply 各截 2000 字符）；
   - `append_memory` → `memory.md` 追加一条带时间戳的流水记录
     （`Stage xxx via yyy: <reply 前 600 字符>`）；
   - `maybe_summarize` → `memory.md` 超过 `TDT_MISSION_CONTEXT_TOKEN_LIMIT`
     时调 LLM 压缩并写回（LLM 失败降级为"保头 + 保最近 4 条"截断）。
3. **bootstrap 超预算时**：新 stage 组装上下文包若超预算，
   `bootstrap_context` 再触发一次 `summarize`。

### 读取路径（唯一消费方）

只有 `HarnessStageExecutor` 读。每个 stage 开始时
`_build_context_pack` → `bootstrap_context` 组装
`ContextPack`，再由 `_build_prompt` 拼进 stage prompt：

- `memory.md` 全文（"Rolling mission memory"段）；
- 每个 context 文件（单个截 6000 字符，"Bound file"段）；
- artifact 索引（最近 40 个产物路径）+ RAG brief。

注入方式是**纯 prompt 文本拼接**：`provider.send` 虽然接收
`context_pack` 参数，但各 provider 实现实际只用 `prompt`，
记忆全部通过 prompt 工程进入 harness。

## 4. 与三层记忆体系的关系

`mission_memory.py` 只是最内层，外面还有两层（见
`docs/zh/modules/memory.md`）：

| 层 | 实现 | 粒度 | 注入方式 |
|----|------|------|----------|
| 文件记忆 | `mission_memory.py` | stage 转录 / 滚动总结 / 绑定文件 | prompt 拼接 |
| 条目记忆 | `memory.py`（4 作用域：workspace/user/mission/agent） | 单条事实 | `context_block` 短摘要 |
| 共享后端 | mem0（可选，`TDT_MEMORY_BACKEND=mem0`） | 向量检索 | 检索命中并入 context_block |

## 5. 与主流 agent 产品的对齐与差距

对齐的部分：文件化存储、provider 无关、预算控制、LLM 压缩——
与 Claude Code 的 "transcripts + CLAUDE.md + compact" 三件套思路一致
（`context/` ≈ CLAUDE.md，`sessions/` ≈ transcripts，`memory.md` ≈ compact 结果）。

已知差距（改进方向，按性价比排序）：

1. `record_turn` 只存 prompt/reply 头 2000 字符，无工具调用/结果记录；
   转录应存完整 turn + 工具事件（JSONL 追加成本很低）。
2. `append_memory` 每轮无条件追加回复头部，属流水账，压缩负担重；
   应改为条件写入（仅决策/结论/风险入库）。
3. `memory.md` 无固定分区结构（目标/决策/未完成/风险），压缩时被
   流水账冲掉；应按分区合并而非全量重写。
4. 无检索：context 全部线性注入，超预算截断；sessions/context 应接入
   检索（可复用 knowledge_gateway），bootstrap 只注入命中部分。

## 6. 依赖

- `harness_providers/executor.py`（唯一读写入口）
- `orchestrator.py`（启动时写 context 文件）
- `settings.py`：`TDT_MISSION_CONTEXT_TOKEN_LIMIT`（token 预算，约 4 字符/token 估算）
