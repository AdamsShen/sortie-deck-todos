# 实施计划：toolkit_search 元工具（P0）+ MCP 会话池（P1）

- 日期：2026-10-20
- 依据：`docs/plans/2026-10-20-tool-search-meta-tool.md`（方案已定稿并经确认）
- 状态：**M1 完成、M2 完成**（2026-10-20 实施，383 通过 / 1 存量环境失败与本次无关）

## 里程碑总览

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1（P0） | in-loop 元工具链路：search/pick 意图 → 召回 → 上浮 → 预算门 + 单测 | ✅ 完成 |
| M2（P1） | MCP 会话池（stdio 每任务独立实例）+ 记录落盘 + 截断 | ✅ 完成 |

P2（向量索引、UI 装载记录面板）不在本次范围。

## M1 · P0：in-loop 元工具链路（已实施）

### T1 settings 开关与预算常量 — ✅
- `src/sortie_deck/settings.py`：新增 `toolkit_search: bool = True`（`TDT_TOOLKIT_SEARCH`），
  与 `loadout_auto` 分离；同时预留池参数（见 T7）。

### T2 loadout.py：自由文本召回 + 摘要 + 上浮 — ✅
- `recall()` 扩展 `query`（自由文本）与 `allowed_ids`（白名单硬过滤）参数，
  capability/query 词元合并打分；停用条目过滤保留（`src/sortie_deck/loadout.py:95`）。
- 新增 `summarize_candidates()`（摘要截断 ≤200 字）、`unfreeze_item()`
  （tool→端点 / mcp→连接信息（env 不展开明文）/ skill→正文 ≤5000 字符截断）。
- 预算常量：`SEARCH_TOP_K=5`、`PICK_BUDGET_PER_STAGE=8`、`SUMMARY_FLOAT_LIMIT=200`、
  `SKILL_BODY_FLOAT_LIMIT=5000`。
- 新增 `pick_candidate()`：校验链 存在 → 启用 → 白名单 → 探活，失败原因回传，永不抛异常。

### T3 graph.py：意图处理（核心）— ✅
- 模块级 `run_toolkit_meta()`（`src/sortie_deck/graph.py`）：处理
  `StageResult.meta["toolkit_search"] / ["toolkit_pick"]`，返回 `ToolkitMetaOutcome`
  （brief_append / events / records）。pick 校验链含预算（每阶段 8 条）与本阶段去重。
- `run_stage` 接线：阶段执行后调用，结果并入 `toolkit_briefs[stage:<stage>]`、
  `toolkit_meta.json` 产物、事件流；`PipelineState` 新增
  `toolkit_pick_budget` / `toolkit_floats` 两个状态字段。
- 新增 `_truncate_output()`：单次工具输出 >40000 字符硬截断并标注。

### T4 harness 协议注入 — ✅
- `src/sortie_deck/harness_providers/executor.py`：`_TOOLKIT_META_PROTOCOL` 说明段 +
  `_toolkit_search_enabled()` 开关判定，开启时附加到阶段提示词尾部。

### T5 org_policy 白名单 — ✅（零改动）
- `run_toolkit_meta` 的 `allowed_ids` 由调用方从 `orchestrator().org_policy` 取得后传入，
  复用既有 `filter_toolkit_ids` 语义（白名单集合语义一致）。

### T6 测试 — ✅
- `tests/test_loadout_search.py`（10 用例）：query 召回/停用过滤/白名单过滤/空输入、
  摘要截断、三类上浮格式（含 env 明文不泄露断言）、skill 截断、pick 校验链。
- `tests/test_toolkit_meta.py`（6 用例）：search 命中/无命中、pick 成功/不存在/白名单外/
  预算用尽/去重、开关关闭 no-op、meta 为 None no-op。

## M2 · P1：MCP 会话池（已实施）

### T7 McpSessionPool — ✅
- `src/sortie_deck/mcp_runtime.py` 新增 `McpSessionPool`：
  - 实例键 = `(item_id, config_hash, task_id)`，`config_fingerprint()` 取
    command/args/env/transport/url 的 sha1；
  - 任务内复用（同键存活实例直接借出）；借出前存活检查，死实例先逐出再重建；
  - 回收判据 = 无在途请求 AND（空闲超 TTL OR 任务已结束）；惰性扫描（acquire 时顺带），
    不引入后台线程；
  - 容量：`max_instances`（默认 50）+ LRU 驱逐无在途实例；busy 时 raise 池耗尽；
  - `stateful: bool`（ToolkitItem 新字段，默认 False）：不参与空闲回收与 LRU，
    仅随任务结束回收。
- settings：`mcp_pool_enabled=True`、`mcp_pool_ttl_seconds=300.0`、
  `mcp_pool_max_instances=50`（TDT_MCP_POOL_* 环境变量）。
- 进程级单例 `get_mcp_pool()` / 测试辅助 `reset_mcp_pool()`。

### T8 graph 接线 — ✅
- `_run_mcp_calls` 调 `invoker.open(items, task_id=initiative_id)`；池开启时借出/复用，
  `close()` 仅归还（递减在途计数）；池关闭或无 task_id 时保持旧的即用即关行为（零回归）。
- `done_node` 调 `pool.release_task(initiative_id)`：任务结束立即回收全部实例（含 stateful）。

### T9 记录落盘 + 截断 — ✅
- in-loop search/pick 记录落阶段产物 `toolkit_meta.json`（供审计回放；events 同步留痕）。
- mcp 工具输出 25k token 量级硬截断（40000 字符 + 截断标注）。

### T10 测试 — ✅
- `tests/test_mcp_pool.py`（6 用例，fake 帧化 JSON-RPC server 覆盖真实子进程路径）：
  同任务复用、跨任务隔离、任务结束回收、在途保护、空闲 TTL 逐出、LRU 上限与 busy 保护、
  stateful 豁免空闲回收。

## 实施顺序（实际执行）

T1 → T2 → toolkit.stateful → T7（池） → T8 → T3/T9（graph 接线） → T4 → T6/T10 测试 → 全量回归。

## 风险与回退（兑现情况）

| 风险 | 兑现 |
|------|------|
| 意图字段被旧 executor 忽略 | ✅ 字段全部可选，缺失时行为与现状一致（383 通过含全部存量用例） |
| 会话池进程泄漏 | ✅ LRU 硬上限 + 任务结束兜底 + 惰性扫描三重保险 |
| skill body 上浮过大 | ✅ 5000 字符截断 + 单阶段 8 条预算 |
| http/sse 池化工作量 | 按预案降级：本期仅池化 stdio，http/sse 维持 skip（`McpSession.start` 非 stdio 直接跳过，日志注明） |

## 遗留与后续

1. ~~harness 执行器从回复文本解析意图 → 写入 `StageResult.meta` 的粘合层~~
   ✅ 2026-10-21 完成：`executor._parse_toolkit_intents`（```toolkit_intent 围栏协议）+
   pick 上浮的 mcp 回写 `stage_toolkits`（`graph.merge_picked_toolkits`），端到端链路见
   `tests/test_toolkit_chain.py`。
2. ~~http/sse transport 的连接复用池化~~ ✅ 2026-10-20 完成（见
   `docs/plans/2026-10-20-mcp-http-transport-and-skill-scripts.md`）。
3. P2：`ToolkitItem.embedding` 向量索引、UI 装载记录面板。

## 状态

- [x] M1（P0）完成
- [x] M2（P1）完成
