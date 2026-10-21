# 工具箱 tool / skill / mcp 执行流程调研

> 来源：2026-10-21 代码调研会话（dev 分支）。
> 涉及核心文件：`src/sortie_deck/toolkit.py`、`loadout.py`、`skill_runner.py`、`mcp_runtime.py`、`mcp_http.py`、`toolkit_index.py`、`graph.py`、`orchestrator.py`、`harness_providers/executor.py`。

---

## 一、总体设计原则

- **模型只"选"，从不"发明"**：所有候选能力来自工具箱库，模型从候选列表中选择，不允许凭空编造工具。
- **声明与执行分离**：executor（harness 层）只在回复里"声明意图"，检索 / 校验 / 上浮 / 执行全部在编排层（`graph.py`）完成——授权与预算集中一处。
- **fail-soft**：三类意图（search / pick / skill_run）任一失败都只把原因回传给模型（可换条目或换词重搜），永不抛异常、不中断阶段。
- **写操作确认门**：所有 write 名样的操作（MCP 工具、skill 脚本）必须显式 `confirmed: true` 才放行。

---

## 二、数据层：三类条目统一建模

tool / mcp / skill 统一为 `ToolkitItem`（`kind: "tool" | "mcp" | "skill"`），落盘为本地 JSON 存储（`src/sortie_deck/toolkit.py:30-69`）。

关键字段：

| 字段 | 含义 |
|---|---|
| `load` | always=常驻注入 / defer=按需检索上浮（**注意：目前只有 API 和 skill_install 写入，编排层尚未消费**） |
| `capabilities` | JIT 召回用的能力标签（打分权重最高） |
| `executable` | skill 脚本可否执行（False 时只当提示词文本） |
| `read_only` | False 时阶段 manifest 会标注"写操作需确认" |
| `stateful` | 有状态 MCP（浏览器登录态等）会话不跨任务共享 |

导入能力：

- skill：markdown（frontmatter 解析）/ zip 包（SKILL.md + scripts/）/ json（`parse_skill_markdown` / `parse_skill_zip_bytes`，toolkit.py:309-390）
- mcp：JSON 配置（stdio/sse/http 自动识别）/ zip 连接器（解压到 `data/mcp_packages/`，toolkit.py:258-306, 393-423）

---

## 三、任务全流程中能力如何被使用

### 3.1 任务创建时：静态装配 + 可选 JIT 装配

1. **按角色预绑定**：`_roles_from_template` 先取任务级 `role_toolkits` 覆盖，否则用 Agent 目录里 agent 默认的 `toolkit_ids`（orchestrator.py:604-616）。
2. **生成 loadout**：`_pipeline_loadout` 把 role/stage 两级绑定压成紧凑简报，存入 `toolkit_briefs["role:{slot}"]` / `toolkit_briefs["stage:{stage}"]`（orchestrator.py:663-703；`_toolkit_brief` 压缩逻辑在 627-651：tool 给端点、mcp 给连接方式、skill 只给摘要+脚本数）。
3. **JIT 动态装配**（默认关，`TDT_LOADOUT_AUTO`）：`_apply_dynamic_loadout` 对每个阶段用"角色+节点名 / 阶段标签"构造 `CapabilityNeed`，走 `build_loadout`（recall → LLM/规则选一个 → 探活 → 生成 manifest），探活通过的条目并入 `stage_toolkits`，计划落 `ini.meta["loadout_plans"]`（orchestrator.py:705-775）。

### 3.2 阶段执行：brief 注入提示词

LangGraph 每个节点 `run_stage`（graph.py:824）：

- toolkit_ids 取值优先级：**stage 绑定 > role 绑定**（graph.py:848）；
- toolkit_brief 优先级：`stage:{stage}` > `role:{role}`（graph.py:849-855），直接拼进 persona；
- Harness executor 再把**元工具协议说明书**（`_TOOLKIT_META_PROTOCOL`）注入 prompt（executor.py:23-41, 318-323），教模型"缺能力时自己申请"。

### 3.3 意图协议（文本围栏，非 function calling）

模型在回复里写 ` ```toolkit_intent ` 围栏块（单对象或数组），executor 用 `_parse_toolkit_intents` 解析成 `StageResult.meta` 的**意图声明**（executor.py:48-66, 218-220）：

```
{"toolkit_search": {"query": "...", "kinds": ["tool", "mcp", "skill"]}}   自由文本召回
{"toolkit_pick": {"item_id": "tk_x"}}                                     点名上浮
{"skill_run": {"item_id": "tk_x", "script": "scripts/x.py",
               "args": [], "confirmed": false}}                           执行脚本（须先 pick）
{"mcp_calls": [{"tool": "...", "arguments": {...}}]}                      MCP 工具调用
```

### 3.4 编排层兑现意图（`run_toolkit_meta`，graph.py:128-249）

executor 返回后，`run_stage` 依次处理（graph.py:1023-1084）：

1. **toolkit_search** → `recall()` 召回候选：
   - 配置了 WeKnora 时走混合检索（向量 + BM25 → RRF 融合 → bge-reranker 重排 → 复合评分）；
   - 异常静默降级本地三层加权打分：能力标签 1.0 / tags 0.6 / 正文 0.25（loadout.py:78-93）；
   - 取 top-5（`SEARCH_TOP_K`）压成候选摘要回 brief。
2. **toolkit_pick** → 校验链 **存在 → 启用 → org 白名单 → 预算（每阶段 8 次）→ 探活**：
   - 任一失败只把原因回传模型，不中断阶段；
   - 通过后 `unfreeze_item` 把条目"解冻"进 brief：tool 给端点、mcp 给连接方式（env 永不展开明文）、skill 直接上浮正文（≤5000 字符截断，loadout.py:186-201）。
3. **skill_run** → 必须先 pick 才能执行（闭环语义，graph.py:312-318），每阶段预算 4 次，write 名样脚本需 `confirmed: true`，沙箱执行（见第四节）。

之后：

- pick 成功的 mcp 条目经 `merge_picked_toolkits` **回写 `stage_toolkits`**（解决"看得见调不了"的缺口闭环，后续 mcp_calls 才能真实建会话调用，graph.py:252-273）；
- 状态推进：pick 预算按"实际 float"累计、上浮条目去重集合、skill_run 预算按"实际执行过"累计（graph.py:1187-1234）；
- 审计产物落 `toolkit_meta.json` / `skill_runs.json`，进事件流与 timeline。

### 3.5 MCP 真实调用（`_run_mcp_calls`，graph.py:717-767）

模型声明 `mcp_calls` 后，编排层统一执行：

- **池化**（`McpSessionPool`，mcp_runtime.py:238-373）：
  - 实例键 = `(item_id, 配置指纹, 隔离域)`；
  - 隔离域：**stdio 恒按 task_id 隔离**（进程一对一物理不可共享）；**http/sse 无状态进共享桶跨任务复用**；`stateful=true` 的 http/sse 仍按任务隔离；
  - 共享前提是配置指纹一致（command/args/env/transport/url/认证头任一变化即起新实例，`config_fingerprint` mcp_runtime.py:191-212）；
  - 回收判据 = 无在途请求 AND（空闲超 TTL OR 任务已结束）；容量满先全量回收再 LRU 驱逐；扫描是惰性的（借出时顺带执行）；
  - 前提是 `mcp_pool_enabled` 开关，池关闭时走旧路径即用即关。
- **两道安全门**（`McpToolInvoker.call`，mcp_runtime.py:489-525）：
  - 白名单 fail-closed：`config.allowed_tools` / `allow_all_tools`，没声明就暴露 0 个工具；
  - 写确认门：工具名以 write/create/update/delete/remove/set/put/post/move 开头且未 `confirmed` 直接 block。
- 结果落 `mcp_calls.json` 产物，并进 `toolkit_usage` 事件。

### 3.6 tool 类型的现状

tool（runtime=cli/http）目前是**纯文本 manifest**：端点写进 brief 供模型参考，探活只查 `shutil.which`（loadout.py:255-265），编排层没有统一的 tool 真实执行通道（与 mcp/skill 不同）。

### 流程总览

```
条目创建/修改/删除
  └─ _mirror_sync → WeKnora manual knowledge（title 锚点映射回 item_id）
                    （backend 未配置 → 无镜像，纯本地打分）

任务创建
  ├─ role/stage 绑定条目 → 压缩 brief → 常驻注入每轮 prompt（不走召回）
  └─ JIT build_loadout: recall→select→probe→manifest → 并入 stage_toolkits
     （needs 含：角色+节点名 / 阶段标签 / 任务 brief 摘要，三路召回）

阶段节点 run_stage 入口（模式 1.5，见第六节）
  └─ 编排层直接 recall（多路聚焦短查询）→ 阈值 → 探活 → 去重 → 注入

阶段执行中（defer 按需路径）
  └─ 模型声明 toolkit_search → recall()（WeKnora 混合检索 ∥ 本地打分）
      → 候选摘要回 brief → toolkit_pick 校验上浮
      → mcp 池化真实调用 / skill 沙箱执行
      → 审计: toolkit_meta.json / skill_runs.json / mcp_calls.json
```

---

## 四、"skill 在沙箱执行"的原因

核心：**skill 的脚本是"外部输入的任意第三方代码"，而调用它的模型是不可信的调度者**——两者都不可信，所以执行必须收在笼子里（`src/sortie_deck/skill_runner.py`）。

### 4.1 脚本来源不可信（最根本）

skill 条目支持从 markdown/zip 导入（toolkit.py:375-390），`scripts[].content` 本质是任何用户上传的任意代码，会被真实落盘并以子进程执行（skill_runner.py:78-83）。如果直接在编排进程内执行，一个恶意导入的 skill 就等于在服务端拿到任意代码执行权（读 `.env`、打内网、改任务数据）。独立子进程是最低限度的隔离边界——至少把崩溃、exit、信号和主进程隔开。

### 4.2 调用方（模型）不可信，必须 fail-closed

模型通过 `skill_run` 意图只能传 `item_id + script + args`，沙箱做双重校验（skill_runner.py:36-50）：

- 只能执行 `item.scripts` 里**精确登记过的 path**——目录穿越被 `lstrip("./")` + 精确匹配堵死，模型不可能临场"发明"脚本名；
- `executable=False` 的 skill 一律拒绝，只当提示词文本；
- 解释器白名单（py/sh/js），其他语言"宁可不跑，不可乱跑"（skill_runner.py:26-33）。

与整个系统"模型只选不发明、授权收敛在编排层"的设计一脉相承。

### 4.3 故障隔离：不拖垮编排主循环

阶段执行跑在同一个 asyncio 事件循环上。脚本用 `create_subprocess_exec` + `asyncio.wait_for` 超时 kill（skill_runner.py:109-116），死循环/卡死脚本不会冻结整个任务引擎。

### 4.4 输出限流：防上下文爆炸

模型能看到 stdout/stderr（skill_run 闭环的意义），但截断到 8000 字符（`SKILL_OUTPUT_FLOAT_LIMIT`，skill_runner.py:118-122），防止海量输出撑爆阶段上下文预算。

### 4.5 诚实的边界说明

目前的"沙箱" = **进程级隔离 + 白名单校验**，还不是完整安全边界（skill_runner.py:9-10 docstring 自述）：子进程仍继承父进程环境（仅叠加 item.env），**网络和文件系统没有限制**——恶意脚本理论上仍能联网或读写工作区外文件。文件系统/网络白名单标为 P2。后续若要执行真正不可信的代码，需补容器级隔离（firecracker/gVisor/docker + 网络策略）。

业内通用做法一致：**先进程隔离快速可用，再逐步收紧到容器隔离**（如 Claude Code 的 skill 执行、OpenAI code interpreter）。

---

## 五、tool / mcp / skill 写入 WeKnora 的时机

镜像挂在 `ToolkitStore` 存储层，**不区分 kind，三类条目时机完全一致**。

### 5.1 存储层四个触发点（每个方法尾部调 `_mirror_sync`，toolkit.py:464-487）

| 动作 | 方法 | 镜像操作 | 位置 |
|---|---|---|---|
| 新建条目 | `create()` | `mirror_upsert` | toolkit.py:560 |
| 导入 upsert / 覆盖同名 | `upsert_by_name()` | `mirror_upsert` | toolkit.py:581 |
| 编辑（含启用/停用、改 summary/tags/capabilities 等） | `update()` | `mirror_upsert` | toolkit.py:604 |
| 删除条目 | `delete()` | `mirror_remove` | toolkit.py:615 |

`_mirror_sync` 是唯一关口：backend 不是 weknora 直接跳过；同步失败只打 warning，绝不阻塞条目主流程。

### 5.2 上游所有入口（API 层）都汇入这四个方法

| 用户操作 | API 端点 | 走哪个方法 |
|---|---|---|
| 表单手动创建 tool/mcp/skill | `POST /api/toolkit`（app.py:1386） | `create` |
| 编辑/启停任意条目 | `PATCH /api/toolkit/{id}`（app.py:1396） | `update` |
| 删除条目 | `DELETE /api/toolkit/{id}`（app.py:1406） | `delete` |
| 导入 MCP JSON / MCP zip / skill md / skill zip / skill json | `POST /api/toolkit/import`（app.py:1420） | `import_payload` → `upsert_by_name`/`create` |
| GitHub 一键安装 skill | `POST .../skills/install`（app.py:1552） | `create` |
| 市场精选安装 skill | `POST /api/market/skills/install`（app.py:1586） | `create` |
| 手写 SKILL.md + skill-install 优化后确认选版 | 选版接口（app.py:1632） | `create` |

**准确说法**：tool 在表单创建时、mcp 在表单创建或 JSON/zip 导入时、skill 在创建/GitHub 安装/市场安装/选版确认时——即任何"落到本地 toolkit.json"的写操作发生的那一刻，同步镜像进 WeKnora。启停（enabled 翻转）走 `update`，所以停用条目会重建镜像（检索时再被 enabled 过滤掉，toolkit_index.py:213-214），而不是从库里删除。

### 5.3 两个特殊时机

1. **种子条目不会自动进 WeKnora**：`ToolkitStore` 首次初始化写 `_SEED` 时直接 `_write`，不经过 `_mirror_sync`（toolkit.py:453）——种子条目要靠全量重建才进镜像。
2. **管理员全量重建**：`POST /api/toolkit/reindex`（app.py:1433-1444）→ `sync_all`（toolkit_index.py:154-179），以本地 toolkit.json 为准"多退少补"。

### 5.4 "写入"在 WeKnora 侧的具体动作

`mirror_upsert`（toolkit_index.py:122-136）每次都是**删旧建新**（WeKnora 无更新接口）：

1. 按 title 锚点 `toolkit/{kind}/{name}#{item_id}` 找到旧 knowledge；
2. 删除；
3. `create_manual(kb, title, content)`——向量化/建索引由 WeKnora 内部完成，本系统只投喂 title + content。

镜像正文（`item_content`，toolkit_index.py:42-70）：name/summary/tags/capabilities/category + endpoint/command + skill 正文（≤4000 字符截断）；**`env`（可能含密钥）与脚本源码永不写入镜像**。

同步是请求路径内的同步调用，超时 10 秒（`_MIRROR_TIMEOUT`，toolkit_index.py:34）。

### 5.5 检索侧对应关系

- `mirror_available()`：backend=weknora 且配了 KB id 才启用镜像路径（toolkit_index.py:73-78）；
- `weknora_recall`：hybrid_search（多取一倍留过滤损耗）→ title 锚点映射回条目 → 过滤 enabled/kinds/白名单 → 复合评分直接作为 recall 得分（toolkit_index.py:182-223）；
- 未配置/异常 → `recall()` 静默降级本地三层加权打分（loadout.py:111-135）。

---

## 六、阶段入口主动召回（模式 1.5，2026-10-21 新增）

> 计划文档：`docs/plans/2026-10-21-阶段入口主动召回工具注入.md`

### 6.1 解决什么问题

第三节的流程有两个自主发现漏洞：

1. **无库存意识**：未绑定条目连名字都不在上下文，模型可能想不到去搜（"召回失败即静默缺失"）；
2. **跨轮时间差**：harness 每阶段单轮对话（executor.py:8），`toolkit_search` 结果在回复完成后才解析回 brief，单趟不重入的阶段永远消费不到搜索结果。

解法：在既有两层供给（绑定常驻 / 模式 2 自主搜索）之间补一层**编排层主动召回**——任务创建时与每个阶段入口，由编排层直接调 `recall()`（不经模型），把匹配条目自动注入当前阶段；无匹配不注入。

### 6.2 注入链路（graph.py）

```
run_stage 入口
  └─ _stage_proactive_recall(state, stage, role, cfg)
      ├─ 幂等检查: stage_recall_done[stage] 命中 → 跳过（重试/HITL 重入不重复召回）
      ├─ 查询构造（三路聚焦短查询）:
      │    ① role + stage 名   ② 阶段 label + label_en   ③ 任务 brief 摘要(≤300 字符)
      └─ run_stage_recall(store, queries=...):
          ├─ 每路 recall()（WeKnora 混合检索 ∥ 本地打分），同条目多路命中取最高分
          ├─ 分数 ≥ loadout_recall_min_score（默认 0.2）才保留——"无需则不调用"的实现
          ├─ 去重: 已绑定(stage_toolkits/role_toolkits) / 已上浮(toolkit_floats) 跳过
          ├─ probe_item 探活: ok/skipped 才注入（注入调不上的工具比不注入更糟）
          └─ 三类条目各自闭环落点:
              tool  → 仅进 brief（端点文本，无执行通道）
              mcp   → 回写 state["stage_toolkits"][stage]（mcp_calls 可真实调用）
              skill → 并入 state["toolkit_floats"][stage]（skill_run 免 pick）
```

上浮文本前缀 `Proactively recalled toolkit items for this stage (role/stage match):`，同时并入 `toolkit_briefs["stage:{stage}"]`（重入时 brief 不丢失），并产生 `toolkit_meta` 事件（data 里分 tool/mcp/skill 三列 id）供前端展示与审计。

### 6.3 关键设计决策

1. **编排层直接召回，而非强制模型调 toolkit_search**：确定性、零模型成本、无跨轮时间差；
2. **多路短查询而非一条长查询**：`loadout._score` 按查询词元数归一（分数=命中权重/查询词元数），长查询会稀释得分——"角色+阶段+标签+brief"拼一起后纯文本匹配只有 ~0.08，全被阈值卡死；拆成聚焦短查询后单路分数有区分度（graph.run_stage_recall docstring 有完整说明）；
3. **幂等**：`PipelineState.stage_recall_done` 每阶段置位一次，重试/HITL 重入零开销跳过；
4. **预算复用**：每路 ≤5 条（SEARCH_TOP_K）× 3 路，skill 正文上浮走既有 5000 字符截断，无新增预算键；
5. **fail-soft**：开关关闭 / store 缺失 / 单路召回异常 / 探活失败全部降级跳过，不中断阶段。

### 6.4 配置开关（settings.py）

| 开关 | 默认 | 含义 |
|---|---|---|
| `loadout_stage_recall` | True | 阶段入口主动召回总开关，关闭时零行为变更 |
| `loadout_recall_min_score` | 0.2 | 最低分阈值；多路合并后的最高分低于即丢弃 |

三层供给开关关系：`loadout_auto`（创建时 JIT）/ `loadout_stage_recall`（阶段入口）/ `toolkit_search`（模式 2）三者独立，可任意组合。

### 6.5 已知边界

- 本地打分后端对中文分词有局限（CJK 无分隔符，`慢查询日志并给出索引建议` 切不出 `慢查询日志`）——条目的 tags/capabilities 应显式登记关键词；WeKnora 后端（向量+BM25+rerank）无此问题；
- brief 摘要（≤300 字符）作为一路查询，词元较多时得分仍会被稀释，主要发现力来自结构化查询（角色+阶段）与 WeKnora 语义检索；
- tool 类条目只有 brief 文本，无编排层执行通道（见 3.6 节）。
