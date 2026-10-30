# 任务输入快照与「一任务一目录」改造计划

> 日期：2026-10-30
> 状态：阶段 1–8 全部完成（2026-10-30）
> 范围：任务新建（规划官会话）→ 执行 → 结束全过程的数据落盘；含 input/ 快照、规划官会话、知识包、materials 注入策略

## 1. 需求与目标

### 1.1 用户诉求

1. 从任务新建到任务结束，**导入的与生成的所有必要文件都落到 `missions/<id>/` 下合理的子目录**。
2. 是否进入上下文**由设计动态决定**，不是"落盘即全量注入"。
3. `BriefingStore(self.cfg.data_dir / "briefings.json")` 这种全局单文件必须消除：不同任务聊天内容不同，且多用户并发建任务，全局文件不可行。
4. 知识包落盘位置需要修正（现在写在 workspace 里，会随沙箱回收而丢失）。

### 1.2 已确认决策

| 决策点 | 选择 |
|---|---|
| 规划会话归属 | **预生成任务 ID**：`start()` 即生成 `ini_` id 并建 `missions/<id>/`，会话写 `chat/planner.jsonl`，确认建队复用同一 id |
| 原件保存 | **原件 + 尽力抽文本**：原件落 `input/files/`，PDF 复用 pypdf，docx/xlsx 新增可选依赖 |
| 存量数据 | **不兼容**：直接删除 `data/briefings.json`，不做迁移 |

### 1.3 非目标

- 不改 artifacts 契约校验逻辑、不改航线/阶段模型。
- 不引入对象存储版 input（S3 backend 仅预留接口，本期落本地）。
- 不重做前端建队交互，只改文件上传与数据来源。

### 1.4 本期范围（2026-10-30 确认）

- 阶段 1–2（已完成）：任务目录布局 + 输入模块骨架 + 规划会话一任务一目录（消除 `briefings.json` 与并发风险）。
- 阶段 3–8（已完成）：上传接口与前端改造、brief 单一事实源、执行期动态注入、知识包落盘、孤儿目录回收、双语文档与 CHANGELOG。
- **不做老数据迁移**：`data/briefings.json` 直接删除；旧 `materials` 记录保持"仅摘录"语义，不回流 `input/`。

## 2. 业内调研摘要

详见 `docs/research/2026-10-30-multi-agent-coding-directory-layout.md`：

- 目录语义收敛为 6 类（输入 / 工作区 / 交付物 / 轨迹 / 会话 / 记忆状态），本项目 5 件套已命中 5 类，缺 `input/`。
- 三条强共性：一任务一根目录、交付物与过程物料分家、生命周期分级回收（workspace 可回收 → logs TTL → artifacts/input/memory 长期）。
- 可直接借鉴：Harbor 把「输入/工作区/轨迹/评测」四区物理隔离；AWP 用 `data/{input,output,state}`；ADK 给 artifacts 加 `versions/{n}/metadata.json`。

本方案的差异点：input 同时保留**原件**与**抽取文本**，并用 manifest 作为唯一索引，避免"摘录层层截断导致资料静默丢失"。

## 3. 目标目录与数据契约

### 3.1 目录结构（保持 5 个顶层子目录）

```
data/missions/<ini_id>/
├── input/                          # 新增：输入快照（建队会话开始即可写）
│   ├── manifest.json               #   文件清单（唯一索引）
│   ├── brief.md                    #   brief 快照（materials 变化时重建）
│   ├── files/<mat_id>__<name>      #   原始二进制（原名保留可读）
│   └── text/<mat_id>.md            #   抽取文本（失败/跳过则缺失）
├── chat/
│   ├── planner.jsonl               #   规划官消息流（append-only）
│   ├── planner.events.jsonl        #   harness 事件流（append-only）
│   ├── planner.state.json          #   会话可变状态（小文件，原子写）
│   ├── <ini_id>.jsonl              #   主房间（不变）
│   └── <ini_id>__<room>.jsonl      #   子房间（不变）
├── artifacts/<stage>/              # 不变
├── logs/
│   ├── <stage>/                    # 不变
│   └── knowledge-pack/<stage>.md   # 新增：知识包正文副本
├── memory/                         # 不变
└── workspace/                      # 不变（可回收）
```

理由：不新增顶层目录，`input/` 承担"只读输入区"，`chat/` 承担"会话区"，与调研中 Harbor/WeKnora 的三段式一致。

### 3.2 `input/manifest.json`

```json
{
  "version": 1,
  "mission_id": "ini_xxx",
  "updated_at": "2026-10-30T12:00:00Z",
  "files": [
    {
      "id": "mat_xxx",
      "name": "prd.md",
      "kind": "prd",
      "mime": "text/markdown",
      "size": 12345,
      "sha256": "…",
      "pinned": true,
      "origin": "intake",
      "stored": "files/mat_xxx__prd.md",
      "text": "text/mat_xxx.md",
      "extract_status": "ok",
      "extract_error": "",
      "added_at": "2026-10-30T11:00:00Z"
    }
  ]
}
```

- `stored` / `text` 为相对 `input/` 的路径，禁止绝对路径（任务目录可整体搬迁）。
- `extract_status`: `ok | skipped | failed | pending`。
- `origin`: `intake`（新建期导入）/ `mission`（任务中追加）/ `stage`（阶段产出物归档，预留）。
- 写入方式：临时文件 + `os.replace` 原子替换；文件级 `fcntl` 锁（`.manifest.lock`）。

### 3.3 `chat/planner.state.json`

```json
{
  "version": 1,
  "mission_id": "ini_xxx",
  "status": "planning | awaiting_confirm | done | failed",
  "title": "",
  "brief": "",
  "coding_executor": "auto",
  "owner_id": "usr_xxx",
  "draft": {},
  "created_at": "",
  "updated_at": "",
  "committed_at": null
}
```

- `messages` / `events` **不放进 state**，改为 append-only JSONL，消除"每轮全量重写"的写放大。
- `materials` **不放进 state**，以 `input/manifest.json` 为唯一事实源；state 只存 material id 列表（可省，直接读 manifest）。

### 3.4 `ExternalMaterial` 扩展字段

```python
class ExternalMaterial(BaseModel):
    ...
    stored_path: str = ""       # 相对 input/，空表示仅摘录（兼容期）
    text_path: str = ""         # 相对 input/
    sha256: str = ""
    extract_status: str = "pending"
    extract_error: str = ""
```

`text_excerpt` 保留（用作 UI 预览与快速注入），但不再作为唯一载体。

## 4. 设计决策

### 4.1 预生成任务 ID 的边界

- `start()` 生成 `ini_` id → `ensure_mission_dirs()` 建目录 → 写 `planner.state.json`（含 `owner_id`）。
- **不写 `initiatives` 表**，避免草稿污染任务列表、分享、统计。
- 会话期授权：`owner_id` 存在 state 里，非 owner 访问返回 403。
- `_commit()` 调 `create_initiative(..., initiative_id=pre_id)`，复用同一 id；建记录后 state 置 `committed_at`。
- 空目录回收：`sweep_orphan_missions()` 扫描 `missions/*`，满足「store 无记录 + `planner.state.json` mtime 超 TTL（默认 7 天）+ 无 `workspace/` 内容」→ `rmtree`。接入现有小时级 sweep 调度点。

### 4.2 规划会话存储（替换 BriefingStore）

新增 `PlannerSessionStore`：

| 操作 | 实现 |
|---|---|
| `start(owner_id)` | 生成 id、建目录、写 state、append 欢迎语 |
| `get(mission_id)` | 读 state + 尾部 N 行 jsonl |
| `append_message` | 追加 `planner.jsonl`（O(1)） |
| `append_event` | 追加 `planner.events.jsonl` |
| `save_state` | 原子写 `planner.state.json` |
| `list()` | 扫 `missions/*/chat/planner.state.json`（仅当前用户过滤在 API 层） |

删除 `BriefingStore` 与 `data/briefings.json`（启动时若存在则 `unlink` 并 warn 日志）。

### 4.3 上传与抽取

新增 `src/sortie_deck/mission_input.py`：

- `save_upload(mission_id, filename, raw) -> ExternalMaterial`：落盘 + sha256 + 去重（同 sha 复用已有文件）+ 写 manifest。
- `extract_text(path, mime) -> (status, text, error)`：
  - 文本类（md/txt/json/csv/yaml/log/code）→ 直接 decode
  - `.pdf` → `pypdf`；`.docx` → `python-docx`；`.xlsx` → `openpyxl`
  - 图片/二进制 → `skipped`（保留原件，供人查看）
  - 依赖缺失 → `failed` + 提示 `pip install 'sortie-deck[ingest]'`，**不阻断上传**
- `read_manifest / write_manifest`：加锁 + 原子写。
- `rebuild_brief(mission_id, brief_base)`：pinned 且可读的文本重算 `input/brief.md`。

配额：单文件 50MB（与服务端知识库导入一致），单任务 500MB，超限返回 400。

### 4.4 动态注入策略（分层）

| 资料形态 | 是否进 prompt | 方式 |
|---|---|---|
| pinned + 抽取成功 + 文本 ≤ 4000 字符 | 是 | inline 进 brief（`compose_brief`） |
| pinned + 文本 > 4000 字符 | 是（仅索引） | prompt 给 `Input index`：路径 + size + 首行摘要，agent 用工具按需 read |
| 未 pinned + 文本存在 | 否 | 只在 manifest 里；agent 可主动 `ls input/` 发现 |
| 图片/无文本 | 否 | 索引里标注 `binary`，提示人工确认 |

`StageContext` 新增 `inputs: list[dict]`（id/name/kind/size/pinned/file_path/text_path），由 `graph.py` 从 manifest 填充；`harness_providers/executor.py` 与 `plugins/coding.py` 两处 prompt 拼装都要加 `Input index` 段。

### 4.5 brief 单一事实源

- `input/brief.md` = `compose_brief(meta["brief_base"], pinned materials)`，每次 manifest/pin 变化重建。
- 删除 `_bind_mission_context_files` 里 `memory/context/brief.md` 的写入（保留 `plan.md`、`harness.md`），消除"两份 brief 不同步"。
- `StageContext.brief` 仍从 store 快照（`orchestrator.py:2045`），但 resume 时会重读，语义一致。

### 4.6 知识包落盘

- `load_pack_for_cursor(..., mission_root: Path | None = None)`：额外写 `missions/<id>/logs/knowledge-pack/<stage>.md`（含 query、检索来源、命中列表、全文）。
- workspace 内两份（`.cursor/rules/*.mdc`、`.sortie-deck/knowledge-pack.md`）**保留**，仍供 Cursor 自动加载。
- prompt 的 `kb_hint` 增加任务目录副本路径。
- 修 `_count_impl_files` 的 `skip`，加入 `.cursor`、`.sortie-deck`。

## 5. 实施步骤

> **本期执行：阶段 1 + 阶段 2。** 阶段 3–8 为后续排期，此处保留设计以备续做。

### 阶段 1（本期）：布局与输入模块（无行为变更，可独立合入）

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/layout.py` | `_MISSION_SUBDIRS` 加 `input`；新增 `mission_input_dir / mission_input_files_dir / mission_input_text_dir / mission_chat_dir / mission_knowledge_logs_dir` |
| `src/sortie_deck/mission_input.py`（新） | manifest 读写、sha256、原子写、文件锁、抽取分发、`rebuild_brief`、`input_index_block()` |
| `pyproject.toml` | 新增 extra `ingest = ["pypdf", "python-docx", "openpyxl"]` |
| `tests/test_mission_input.py`（新） | 落盘/去重/抽取降级/manifest 原子写 |

验收：`pytest tests/test_mission_input.py -q` 通过；旧测试不受影响。

### 阶段 2：规划会话改为一任务一目录

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/mission_briefing.py` | 删 `BriefingStore`；新增 `PlannerSessionStore`；`start()` 预生成 `ini_` id 并建目录；`handle()/ _commit()` 改为读写 `chat/planner.*`；`_commit()` 传 `initiative_id=pre_id` |
| `src/sortie_deck/orchestrator.py` | `create_initiative(..., initiative_id: str | None = None)`，不再强制 `new_id`；供 `_commit()` 复用 |
| `src/sortie_deck/api/app.py` | `/api/briefing/sessions` 返回 `{session_id, initiative_id}`（同值）；`get_briefing()` 装配新 store；启动时删除 `briefings.json` |
| `src/sortie_deck/harness/waterfall.py:15` | 注释同步（持久化改由 PlannerSessionStore 负责） |
| `scripts/backfill_mission_owner.py` | 移除 `briefings.json` 回退（文件已删），改读 `chat/planner.state.json` |
| `apps/web/src/components/MissionBriefingChat.tsx` | 使用返回的 `initiative_id` 作为后续所有请求的 key |
| `tests/test_mission_briefing.py` | 改为断言 `missions/<id>/chat/planner.*` 落盘、消息 append 不重写、owner 403 |
| `tests/test_briefing_concurrency.py`（新） | 20 线程并发建会话，互不覆盖；同一会话并发追加消息行数正确 |

验收：并发测试通过；`data/briefings.json` 不再被创建/读取。

### 阶段 3：原件上传端到端

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/api/app.py` | 新增 `POST /api/missions/{id}/input`（multipart）、`DELETE .../input/{mat_id}`、`PATCH .../input/{mat_id}`（pin/kind）；统一 owner 校验 |
| `apps/web/src/components/NewTaskPage.tsx` | 选文件后立即上传（不再只读文本）；失败降级为"仅摘录"并提示 |
| `apps/web/src/components/MissionBriefingChat.tsx` | 附件上传走同一接口，`data_url` 仅用于本地预览 |
| `apps/web/src/api.ts`、`types.ts` | 新增上传/删除/pin 的类型与调用 |
| `tests/test_input_api.py`（新） | 上传 200/超限 400/非 owner 403/抽取失败仍 200 |

验收：`npm run typecheck` 通过；PDF/docx/xlsx 能落原件并抽取。

### 阶段 4：materials 投影与 brief 单一事实源

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/models.py` | `ExternalMaterial` 增 `stored_path/text_path/sha256/extract_status/extract_error` |
| `src/sortie_deck/materials.py` | `compose_brief` 改为"≤4000 字符 inline，其余只留索引"；新增 `input_index_block()` |
| `src/sortie_deck/orchestrator.py` | `set_materials` 以 manifest 为准重建 `input/brief.md`；`_bind_mission_context_files` 不再写 `memory/context/brief.md` |
| `src/sortie_deck/api/app.py` | `PATCH /api/initiatives/{id}/materials` 保留为兼容入口，内部转调 manifest |
| `tests/test_materials.py` | 覆盖 inline 阈值、索引段落、brief 重建幂等 |

### 阶段 5：执行期注入

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/models.py` | `StageContext` 增 `inputs: list[dict]` |
| `src/sortie_deck/graph.py` | 从 manifest 读取并填充 `inputs` |
| `src/sortie_deck/harness_providers/executor.py` | prompt 增加 `Input index` 段（两级执行器都要） |
| `src/sortie_deck/plugins/coding.py` | 同上；`_count_impl_files` skip 加 `.cursor`、`.sortie-deck` |
| `tests/test_stage_prompt_inputs.py`（新） | 断言 prompt 含索引路径、不含大文件全文；`_count_impl_files` 不再计入知识包 |

### 阶段 6：知识包落盘

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/knowledge_pack.py` | `load_pack_for_cursor` 增 `mission_root`，写 `logs/knowledge-pack/<stage>.md`；返回增加 `copy` 路径 |
| `src/sortie_deck/plugins/coding.py` | 传 mission_root；`kb_hint` 加副本路径 |
| `tests/test_knowledge_pack.py` | 断言任务目录副本存在、内容与 workspace 版本一致、workspace 回收后仍可读 |

### 阶段 7：清理与回收

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/layout.py` 或新 `maintenance.py` | `sweep_orphan_missions(data_dir, ttl_days)` |
| `src/sortie_deck/runtime.py` / 调度点 | 接入小时级 sweep；`sortie doctor` 增加 input 目录体检 |
| `scripts/backfill_mission_owner.py` | 移除 `briefings.json` 回退，改读 `chat/planner.state.json` |
| 启动逻辑 | `data/briefings.json` 存在则删除并 warn |

验收：`sortie doctor` 无误报；放弃会话 7 天后目录被回收。

### 阶段 8：文档与变更记录

- `docs/zh/`+`docs/en/`：模块文档（briefing / artifacts / memory / pipeline）与 `data-directory-lifecycle` 同步新布局。
- `CHANGELOG.md` 加 `[Unreleased]` 条目（破坏性：`briefings.json` 移除、新增上传接口）。
- `CLAUDE.md` 关键文件地图补 `mission_input.py`、`PlannerSessionStore`。
- 本文档状态置为"已完成"。

## 6. 兼容、迁移与回滚

- **不做数据迁移**（用户明确）。`briefings.json` 直接删除；`initiatives` 里已有 `materials` 的旧记录在读取时若 `stored_path` 为空，按"仅摘录"处理（不回流到 `input/`）。
- 回滚：阶段 1、5、6 可单独回退；阶段 2 涉及 API 契约，回退需同步前端。建议按阶段独立提交，每阶段可单独 revert。

## 7. 测试计划

- 单元：manifest 原子写/并发写、抽取降级、`compose_brief` 阈值、`_count_impl_files`。
- 集成：上传→抽取→manifest→prompt 注入全链路（mock 执行器）。
- 并发：20 会话并发 start/append，断言无覆盖、无丢失。
- 回归：`pytest -q` 全绿；`ruff check src tests`；`npm run typecheck`；`mkdocs build --strict`。

## 8. 风险与备选

| 风险 | 影响 | 应对 |
|---|---|---|
| 预生成 id 未落 store | 会话期无法用 store 做权限/列表查询 | owner 写 state + 每次请求校验；会话不进入任务列表 |
| 文件锁仅单机有效 | 多 worker 跨主机时 manifest 可能竞争 | 本期限定单机/共享卷，文档标注；预留 S3 实现位 |
| 上传占用磁盘 | 大文件堆积 | 单文件 50MB / 单任务 500MB 配额；任务删除即回收 |
| 抽取依赖缺失 | 资料不可读 | `extract_status=failed` + 明确安装提示，不阻断上传 |
| 删除 `briefings.json` 不可逆 | 历史会话丢失 | 用户已确认；删除前打一条 warn 日志记录 session 数 |

备选方案（未采用）：

- A. 暂存区 `data/intake/<session_id>/` 后迁移 —— 用户已否决（多一次迁移与 id 映射）。
- B. 规划会话直接复用主房间 `chat/<ini>.jsonl` —— 实现最省，但会污染主房间时间线，且建队前的规划对话不适合出现在小队群聊里。

## 9. 实施结果与偏差（2026-10-30 核对）

| 阶段 | 状态 | 关键证据 |
|---|---|---|
| 1 目录与输入模块 | 完成 | `layout.py` 增 `input` 与路径 helper；`mission_input.py`（manifest/原子写/抽取/配额/`render_input_index`） |
| 2 规划会话一任务一目录 | 完成 | `PlannerSessionStore`；`create_initiative(initiative_id=...)`；启动删 `briefings.json`；`test_briefing_concurrency.py` |
| 3 上传端到端 | 完成 | `POST/GET/PATCH/DELETE /api/missions/{id}/input`；`uploadMissionInput` + `onSessionReady` 自动补传；聊天附件 dataURL→File |
| 4 brief 单一事实源 | 完成 | `compose_brief` 4000 字符阈值 + 指针；`set_materials` 走 manifest 投影；不再写 `memory/context/brief.md` |
| 5 动态注入 | 完成 | `StageContext.inputs` + `graph._mission_inputs`；executor/coding 注入 `## Input index`；`_count_impl_files` 排除知识包 |
| 6 知识包落盘 | 完成 | `load_pack_for_cursor(mission_root=, stage=)` → `logs/knowledge-pack/<stage>.md` |
| 7 孤儿目录回收 | 完成 | `maintenance.sweep_orphan_missions` + `TDT_INTAKE_TTL_DAYS` + 启动/每小时 sweep；doctor `missions` 体检 |
| 8 文档与变更记录 | 完成 | CHANGELOG；`docs/{zh,en}/modules/artifacts.md` §7；`docs/{zh,en}/modules/harness.md`；`docs/data-directory-lifecycle.md`；CLAUDE.md |

### 偏差（有意为之，非遗漏）

1. **未新增 `[ingest]` extra**：`pypdf` / `python-docx` / `python-pptx` / `openpyxl` 已是 `pyproject.toml` 核心依赖，再加 extra 属重复；依赖缺失时仍按 `extract_status=failed` 降级，不阻断上传。
2. **模块文档范围**：只更新了 artifacts（新增 §7）与 harness（`BriefingStore` → `PlannerSessionStore`），以及 `data-directory-lifecycle.md`。memory / pipeline / rooms 文档未描述输入快照或会话存储，无过期内容需要改。
3. **`mkdocs build --strict` 未通过**：3 条既有跨包链接告警（`zh/design/*` 指向 `TODO.md` / `SECURITY.md`，这些文件不在 `docs/` 内），与本次改动无关。
4. **全量 `pytest` 剩 1 个既有失败**：`test_memory_agents::test_memory_context_block_uses_summaries_and_scoring`，已用 HEAD 工作树复现，与本次无关。
5. **未执行 git commit**：用户未要求；建议按阶段拆分提交，注意工作区另有并行的 api_keys/service 改动。

### 核对时补齐的缺口

- `PATCH /api/missions/{id}/input/{material_id}` 原仅支持 `pinned`，已补 `kind`（`mission_input.update_material`）。
- 原只有分块单测，补了 `tests/test_input_flow.py`（上传 → 抽取 → manifest → brief → prompt 全链路）。
