# 小群（Sub-room）：仅任务所有者可见 + 按任务分组

- 日期：2026-10-29
- 状态：已实施（2026-10-29，含用户确认的两条口径：admin 非 owner 不可见；小群页顶部任务切换器）

## 1. 需求

来自使用反馈（截图）：

1. **简介溢出**：「小群」页左栏卡片的 goal 文本溢出卡片边界，压住了状态标签与相邻卡片。
2. **按任务分类**：小群列表要按任务（initiative）归类展示。
3. **空任务不显示**：当前任务没有小群时，不展示任何小群内容（包括演示数据）。
4. **可见性收紧**：小群只有任务的**所有者**（`created_by`）能看到；被共享的用户看不到；admin 角色也看不到。

## 2. 现状与问题定位

| 位置 | 现状 | 问题 |
|---|---|---|
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/api/app.py:2482` | `GET /api/subrooms` 只做了 `require_user`，直接 `service.store.list(initiative_id=...)` | 任意登录用户（含共享用户、admin）能拉到全站小群记录与 goal / conclusion |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/api/app.py:2498` | `GET /api/subrooms/{id}/transcript` 同样零校验 | 知道 id 即可读完整对话 |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/api/app.py:2325` | 任务 SSE 只校验 `_ensure_mission_access`（owner / 共享 / admin） | `subroom`、`subroom_message` 事件会推给共享用户与 admin |
| `/Library/git-root/sortie-deck-todos/apps/web/src/components/SubRoomPanel.tsx:97` | 接口返回空时 `setSubrooms(MOCK_SUBROOMS)` | 没有小群的任务也显示三条演示数据（截图里的 `camp-v2-01` 是 mock） |
| `/Library/git-root/sortie-deck-todos/apps/web/src/App.tsx:5879` | 独立小群页只传 `initiativeId={selectedId}`，未传任务标题 | 无分组信息；未选任务时拿的是全站小群 |
| `/Library/git-root/sortie-deck-todos/apps/web/src/styles.css:6417` | `.wr-rail-item .wr-g` 是 `<span>`（inline） | `overflow/ellipsis` 对 inline 无效 → 长 goal 溢出卡片 |

权限模型现状（`app.py:124`、`app.py:164`）：任务有 `created_by` 与 `shared_with`；`_ensure_mission_control` 给 owner 与共享用户控制权；admin 对他人任务只读可看。小群此前完全没有独立权限判断。

## 3. 业内调研

- **LangGraph 持久化实践**：checkpointer 只认 `thread_id`，不知道线程归属，官方/社区建议在应用库单独建 threads 表存 `user_id`，"conversation list 只查自己的线程"，权限必须在业务层做，不能指望存储层。
  - https://www.learnwithparam.com/courses/conversational-state-machines-langgraph
  - https://www.jahanzaib.ai/blog/langgraph-session-management
- **多 Agent 子会话可见性**：OpenClaw 的 session 工具把子 Agent 会话分为 `visible: true`（受监督的可见子会话）与默认 hidden（仅父级可见），显式用 `visible` 控制暴露范围，默认最小可见。
  - https://github.com/openclaw/openclaw/blob/8fd6d59282677e164067e251b04fab7448586772/docs/concepts/session-tool.md
- **行业共性**：子会话/子线程的可见性 = "归属主体（owner）+ 显式扩展"，默认继承父实体的可见性上限，但**不自动继承父实体的共享关系**；越权查看需要单独授予，不做角色豁免。

面向本项目的结论：小群属于"执行过程的临时内部讨论"，其可见性应比父任务更窄 —— 只认 `initiative.created_by`，共享关系与 admin 角色都不自动传递。

## 4. 拟采用设计

### 4.1 后端：三处入口统一按 owner 判定

新增内部辅助（放在 `_visible_initiatives` 附近）：

```python
def _owned_initiative_ids(user: PublicUser) -> set[str]:
    """当前用户作为所有权人的任务 id 集合（供小群这类更严格的资源复用）。"""
    return {
        i.id for i in orch().list_initiatives()
        if getattr(i, "created_by", None) == user.id
    }
```

- `GET /api/subrooms`
  - 带 `initiative_id`：不在 `_owned_initiative_ids` 内 → `403`（"sub-rooms are owner-only"）。
  - 不带：先按 owner 集合过滤，再返回。
- `GET /api/subrooms/{id}/transcript`：先取 sub-room → 取其 `initiative_id` → 同上校验，不通过 `403`。
- `GET /api/initiatives/{id}/events`：在 `gen()` 内计算 `is_owner = ini.created_by == user.id`，收到 `subroom` / `subroom_message` 事件且 `is_owner` 为假时**不 yield**（其余事件照常推送）。

判定口径（关键决策点，待确认）：**只看 `created_by == user.id`，与角色无关**。admin 不是 owner 时看不到别人任务的小群；admin 是 owner 时能看到自己任务的。

### 4.2 前端：去掉 mock 回退、按任务分组、修溢出

1. `SubRoomPanel.tsx`
   - 删除"接口为空 → MOCK_SUBROOMS"的兜底（`SubRoomPanel.tsx:100-105`），空就是空。
   - `page` 变体改为**按任务分组**：按 `initiative_id` 聚合，组头显示任务标题（新增可选 prop `initiativeTitles: Record<string, string>`），组内条目沿用现有轨道卡；组的排序按任务创建时间倒序，组内按 `updated_at` 倒序。
   - 传入 `initiativeId` 时只渲染该任务一组；该组为空 → 显示 `noSubrooms` 空态。
   - `dock` 变体（任务内抽屉）保持单任务语义不变。
2. `App.tsx`
   - `view === "subrooms"` 传入 `initiativeTitles`（仅 `items` 中 `created_by === user.id` 的任务，避免泄露他人任务标题）。
   - 独立小群页顶部加任务切换（chips 或 select）：候选 = 我拥有的任务；默认选中 `selectedId`（若属于我）。
   - 任务内「小群」抽屉入口（`App.tsx:3165`）仅在 `detail?.created_by === user.id` 时渲染，共享用户与 admin 连入口都看不到。
3. `styles.css`
   - `.wr-rail-item .wr-g { display: block; }`（让 `overflow:hidden` + `text-overflow: ellipsis` 生效）。
   - 分组容器样式（`.wr-subroom-group` / `.wr-subroom-group-head`）。

## 5. 涉及文件

- `/Library/git-root/sortie-deck-todos/src/sortie_deck/api/app.py`（3 处路由 + 1 个辅助函数）
- `/Library/git-root/sortie-deck-todos/apps/web/src/components/SubRoomPanel.tsx`（去掉 mock 兜底 + 分组渲染 + 新 prop）
- `/Library/git-root/sortie-deck-todos/apps/web/src/App.tsx`（传标题、任务切换、抽屉入口权限）
- `/Library/git-root/sortie-deck-todos/apps/web/src/types.ts`（如新增 prop 类型）
- `/Library/git-root/sortie-deck-todos/apps/web/src/styles.css`（溢出修复 + 分组样式）
- `/Library/git-root/sortie-deck-todos/src/sortie_deck/i18n`（新增空态/分组文案，`apps/web/src/i18n.ts`）
- 测试：`tests/` 新增小群权限用例（owner 可见 / 共享用户 403 / admin 非 owner 403 / admin owner 可见）

## 6. 备选方案

- **A（未采用）**：保留 `/api/subrooms` 全量返回，只在前端过滤。→ 数据仍经网络下发，等于没做权限，不安全。
- **B（未采用）**：给 `SubRoom` 增加 `owner_id` 字段并在写入时固化。→ 更"自洽"，但需要兼容历史数据（旧记录无该字段），且 owner 变更时口径不一致；当前直接用 initiative.created_by 即可，后续确需转移所有权再补。
- **C（未采用）**：admin 保留只读能力（与任务详情一致）。→ 与本次需求明确冲突，仅在需要审计时另行加开关。

## 7. 验证方式

- 三账号手测：owner（创建者）/ 共享用户 / admin（非 owner），分别访问小群页、任务抽屉、`/api/subrooms`、`/api/subrooms/{id}/transcript`、SSE。
- 前端：`npm run typecheck`；后端：`pytest -q` + `ruff check src tests`。
