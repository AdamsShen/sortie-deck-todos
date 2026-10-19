# 知识库页面改版：仿「记忆」页设计 · 三版本设计稿

> 归档自 plan.md（2026-10-19）。状态：设计稿已交付（design-demos/kb-redesign-v1-candy.html / v2-benchmark.html / v3-hara.html 及截图），等待用户选版后进入实现。

## 一、需求背景

现知识库页面（`apps/web/src/App.tsx:2829-2956`）是旧式「左侧文档列表 + 右侧文档详情」布局，视觉与交互远落后于新改版的「记忆」页（`App.tsx:2958-3345`，编辑部风：工具行 + 卡片网格 + 大弹窗）。本次需求：

1. **整体仿照记忆页设计**改版知识库页面；
2. **支持文件导入**：docx / ppt / csv / excel / md / txt 等（后端 `POST /api/knowledge/import` → WeKnora 已备好但前端未接，见 `src/sortie_deck/api/app.py:422-432`、`src/sortie_deck/weknora.py:220-238`）；
3. **「添加知识库」改为弹窗**（新弹出页面，不再是内联表单）；
4. **知识库分类**：全局共享知识库（仅 admin 可添加）/ 我的知识库 / 需求知识库（挂在某个需求 initiative 下）；
5. **交付形式**：用 huashu-design 设计 **3 个版本**，每版都包含列表页 + 弹出页面。

**本轮先交付设计稿（HTML demo），用户选定版本后再进入代码实现。**

## 二、业内调研（真实检索结果）

| 来源 | 做法 |
|---|---|
| 51CTO《AI 智能会话原型解析：知识库管理》（https://blog.51cto.com/u_16099230/14222924） | 知识库管理页 3:7 结构：左侧分类导航 + 新建按钮，右侧**知识库卡片网格**（文件数 / 存储大小 / 创建时间）；库详情 = 文件清单 + 上传入口；「添加数据」用弹窗 |
| Cherry Studio 知识库 UI（https://deepwiki.com/hyhfish/cherry-studio/3.3-knowledge-base-ui） | 知识库列表 → 详情页文件列表 + 每个文件的处理状态图标（解析中/完成/失败） |
| RAGFlow 知识库 UI（https://deepwiki.com/leichangqing/ragflow/5.1-knowledge-base-ui） | dataset 卡片网格 + 文档级处理状态 + 重命名/删除对话框 |
| Eleken《Modern File Upload UI 2026》（https://www.eleken.co/blog-posts/file-upload-ui） | 拖拽区 + 点击选择 + 多文件批量上传 + 逐文件进度/状态反馈是现代 SaaS 标配 |

**共性归纳**：知识库是「库 → 文件」两层结构；列表用卡片网格展示库元信息；文件导入用拖拽 + 多格式 + 处理状态；新建/添加类操作走弹窗。
**与本项目兼容点**：卡片网格、工具行分段控件、`.mem-overlay + .panel` 弹窗、删除撤销 toast 均可直接复用记忆页模式与设计 token（`styles.css:1-50`）。
**冲突点**：现后端是「单库文档列表」模型（`KnowledgeDoc`，无库级 scope/归属），需求知识库需关联 initiative——实现阶段需后端扩展（见第五节）。

## 三、设计方案：三套逻辑并行出 3 版（huashu-design 流程）

用户已给出明确风格锚点（仿记忆页编辑部风），不走 Fallback 顾问模式，直接在记忆页设计语言之上做三版**差异化变体**（布局/交互维度不同，视觉 token 同源）。三版独立由 subagent 产出，互不参考。

### 每版必须包含的画面（用 mock 数据，真实感内容）

1. **知识库列表页**：页面标题 + 「＋ 新建知识库」主按钮；分类切换（**全局共享 / 我的知识库 / 需求知识库**，含各分类计数）；搜索框；知识库卡片网格（库名、简介、文档数、归属/创建者、更新时间、类型色条——复用记忆页卡片签名细节）；admin 视角下全局共享卡片带管理标识。
2. **新建知识库弹窗**（`.mem-overlay` 式居中大弹窗）：名称 / 简介 / 分类选择（全局共享仅 admin 可选，非 admin 置灰+说明）/ 需求知识库需选择关联需求（下拉列出 initiatives）/ 初始可见范围。
3. **文件导入弹窗**（从库详情或卡片进入）：拖拽上传区 + 点击选择 + 支持格式提示（docx/ppt/csv/xlsx/md/txt/pdf…）+ 多文件列表逐个显示解析状态（排队/解析中/完成/失败）+ 复用 WeKnora `/api/knowledge/import` 语义。
4. （每版附一张）**库详情态**：库内文档/文件清单 + 导入入口，体现两层结构。

### 三版逻辑分配

| 版本 | 逻辑 | 差异化方向 |
|---|---|---|
| V1 | 🎲 秒数轮盘（`date +%S % 20 + 1`，从 design-styles.md 网页 20 种取号） | 在编辑部风基调上引入该风格的结构/密度变化 |
| V2 | 🏆 现实参照 | 以 Dify / RAGFlow / Linear 级别的知识库控制台为标杆迁移（先 WebSearch 核实其真实设计语言） |
| V3 | 🧠 最佳设计师 | 以最适合「编辑部/写作工具」气质的设计团队（如 Pentagram / Stripe Docs 团队）的思维定制，强化衬线排版与纸感 |

### 产出与验证

- 产出目录：`design-demos/`，命名 `kb-redesign-v1-roulette.html` / `kb-redesign-v2-benchmark.html` / `kb-redesign-v3-designer.html`（❌ 禁 `_temp/`）
- 纯 HTML/CSS 单文件（可加少量 inline JS 做弹窗开关/tab 切换演示），复用 styles.css 里的 token 值（纸/墨双主题至少支持纸主题）
- Playwright 截图（1440×900）：每版列表页 + 两个弹窗，三版共 9 张左右，汇总对比后展示给用户选择
- ✅ 产出自检：`design-demos/` 下必须有 3 个新 .html，缺一不交付
