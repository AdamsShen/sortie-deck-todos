# 航线与图编排

## 意图

把「先做什么、谁做、能否并行」收成 **可确认的阶段图**，而不是一次性写死的脚本。

## 模型

1. **提案**：`pipeline_planner`（启发式或可选 LLM）产出 `pipeline_specs`（节点、角色、`branch` / `parallel_group` / `depends_on`）。
2. **确认**：真人确认或手改后，`materialize_template` → `build_graph_from_template` → 按任务编译 `custom:{initiative_id}`。
3. **运行**：LangGraph 跑单节点或并行 wave；HITL 用 `interrupt()`。

```mermaid
flowchart LR
  brief[Brief]
  planner[PipelinePlanner]
  specs[PipelineSpecs]
  human[HumanConfirm]
  compiled[CompiledGraph]
  brief --> planner --> specs --> human --> compiled
```

## 并行分支

多端场景（如 Web ∥ 后端）按 **分支链** 建模，而不是共用一个自测节点：

```text
用例
 ├─ Web 开发 → Web 自测 ──┐
 └─ 后端开发 → 后端自测 ──┴→ 联调
```

- 同一 `parallel_group` + 不同 `branch`：wave 内 `asyncio.gather` 跑各分支序列。
- UI 轨道按列 / 泳道画分叉与汇合；边可表达进度（炫彩流动为展示层）。

实现要点：`parallel.py`（collapse waves / branches）、`graph.py`（wave node）、`PipelineRail.tsx`。

## 产物契约

- 执行器返回文件名 → 内容；写入 `ArtifactStore`。
- `contracts.REQUIRED` 校验必选文件。
- 并行成员文件名冲突时按路径保留 stage 作用域（`stage::kind` 同步策略）。
- 单阶段执行上限由 `TDT_STAGE_TIMEOUT_SECONDS`（默认 1800s）控制。超时或执行器失败时，编排层会从该阶段 workspace 回收 `REQUIRED` 中已写出的文件（跳过 `repos/` 等目录与空文件）；回收成功则阶段继续推进，并在时间线留下一条 `⚠️` 事件说明"未正常返回但产物已回收"。
- 产物仍缺失时抛 `StageContractError`：异常携带真实阶段（并行波次里 `current_stage` 可能已指向别的成员），并把执行器失败原因（如 `timed out after 1800s`）与 `missing artifacts` 一并抛出，避免"超时"被误报成"没写产物"。

## 与 UI 的契约

| 字段 | 用途 |
|------|------|
| `pipeline_rail` / `pipeline_specs` | 轨道渲染 |
| `active_parallel` | 当前波成员高亮 |
| `depends_on` | 图语义 / 检查面板 |

详见模块：[航线规划](../modules/pipeline.md)、[图与门禁](../modules/graph.md)。
