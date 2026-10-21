# 工具箱 V4 落地实施计划（前端改版 + 后端接口对接 + 连通性测试）

状态：已完成（后端 55 测试 + API smoke 13/13 + UI e2e 11/11 全过）

## 需求背景

用户选定工具箱重设计 V4 方案（Stripe 市场感版，见 `design-demos/toolkit-redesign-v4-stripe-market.html` 与 `toolkit-redesign-spec.md`），要求：

1. 按 V4 改造真实工具箱页面（`apps/web/src/App.tsx` 的 toolkit 视图）；
2. 补齐后端接口并对接：外网 GitHub 技能安装、skill-install 优化（手写 MD → 双版本对照 → 选版后删除未选版本）、MCP 测试连接（stdio/SSE/Streamable HTTP）、stdio 连接器 zip 包导入、市场推荐数据、常驻/按需（load）与挂载计数、常驻预算；
3. 端到端测试工具箱页面功能连通性。

## 业内调研（延续 2026-10-20/21 调研结论）

- Dify 插件市场：市场/已装分区、一键安装、MCP 双向支持（dify.ai）
- Claude Code marketplace：GitHub 仓库一键安装、Verified 标记、stdio/sse/streamable-http 三种 transport（claudemarketplaces.com、claude.com/plugins）

## 现状盘点（关键定位）

- 视图：`apps/web/src/App.tsx:4181` 起 `toolkit-market`；表单逻辑 1788-1956；卡片网格 4533-4640
- 类型：`apps/web/src/types.ts:131` `ToolkitItem`（无 load 字段）
- 后端模型：`src/sortie_deck/toolkit.py:30`（ToolkitItem）/`:70`（Create）/`:95`（Update）/`:579`（import_payload，mcp 仅支持 JSON、skill 支持 md/zip/json）
- MCP 会话：`src/sortie_deck/mcp_runtime.py:215` `_create_session`（stdio）+ `src/sortie_deck/mcp_http.py`（SSE/Streamable HTTP + `config.bearer_token`/`config.headers` 认证头）——测试连接可直接复用
- LLM：`src/sortie_deck/llm.py:14` `LlmClient`（未配 key 时不可用 → 需规则优化兜底）
- Agent 挂载：`src/sortie_deck/agents.py:51` `toolkit_ids`；`api/app.py:208` `get_agents()`

## 技术设计

### 后端（新增/修改）

| 项 | 位置 | 说明 |
|---|---|---|
| `load` 字段（always/defer） | `toolkit.py` 三个模型 + create() | 常驻/按需，默认 defer |
| MCP zip 导入 | `toolkit.py` `import_payload` | kind=mcp + zip_base64：解压到 `data/mcp_packages/<slug>/`，生成 stdio 条目，command 预填 `npx -y @modelcontextprotocol/server-<slug>` 可后改 |
| 技能安装/市场/优化 | 新模块 `src/sortie_deck/skill_install.py` | `install_from_github(url)`（raw.githubusercontent 拉 SKILL.md，main→master）；`MARKET_SKILLS` 内置精选；`rule_optimize()` 规则优化兜底；`optimize_skill_md()`（LlmClient 可用走 LLM，否则规则）；`SkillDraftStore`（`data/skill_drafts.json`，双版本草稿，确认即删） |
| 新端点 | `api/app.py` | `POST /api/toolkit/mcp/test`（复用 `_create_session`，15s 超时，返回 tools/log）；`POST /api/toolkit/install_skill`；`GET /api/market/skills`（含 installed 标记）；`POST /api/market/skills/install`；`POST /api/toolkit/optimize_skill`；`POST /api/toolkit/optimize_skill/{draft_id}/confirm`（选版建条目 + 删草稿 = 未选版本从服务器本地存储删除）；`GET /api/toolkit/mounts`（{toolkit_id: agent 数}） |

### 前端（App.tsx / types.ts / i18n.ts / styles.css）

- 页头：常驻预算 chip（启用且 load=always 计数 / 建议上限 12）
- 三 tab 带计数；发现带：技能=市场推荐卡（安装按钮）+ 添加技能；连接器=接入方式说明 + 添加连接器；工具=添加工具
- 卡片：meta 标签（load/分类/transport 或 runtime/挂载数）+ 右上「…」菜单（停用/编辑/删除）
- 添加技能弹窗：外网（GitHub URL 校验）/ 本地（上传 zip·md 走既有 `/api/toolkit/import`；手写 MD → optimize → 双版本对照 → confirm）
- 添加连接器抽屉：transport 分段；stdio 仅 zip 导入 → 命令预填可改 → 测试连接；SSE/HTTP → URL + Bearer/OAuth → 测试连接
- 添加工具抽屉：名称/摘要/分类/runtime 分段/endpoint/load
- 详情页：连接器加「测试连接」，条目显示挂载 Agent 数

### 测试

- 新增 `tests/test_toolkit_v4.py`：load 字段、规则优化 + 草稿确认删除、市场列表/安装、GitHub 安装（monkeypatch httpx）、mcp/test（复用 test_mcp_pool 的 fake server 模式）、mcp zip 导入、mounts
- 连通性：`pytest` 全绿 + `tsc/vite build` 通过 + 起真实 uvicorn 跑 smoke 脚本（create→test mcp→optimize→confirm→market→install_github mock）

## 备选方案

- 市场推荐改为抓取外部市场 API：放弃（首版内置精选，接口形状已预留）
- skill 优化必须 LLM：放弃强依赖（LlmClient 未配置时规则兜底，保证功能可用）

## 文件清单

后端：`src/sortie_deck/toolkit.py`、`src/sortie_deck/skill_install.py`（新）、`src/sortie_deck/api/app.py`
前端：`apps/web/src/App.tsx`、`apps/web/src/types.ts`、`apps/web/src/i18n.ts`、`apps/web/src/styles.css`
测试：`tests/test_toolkit_v4.py`（新）
