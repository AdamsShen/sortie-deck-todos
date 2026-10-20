# 工具箱页面重设计 · 设计 Spec（三版共用）

## 产品背景

Sortie Deck 是 AI 原生的多 Agent 编排系统（「前线小队 · 共交付 · 无传话」）。工具箱（Toolbox）是小队/Agent 挂载能力的仓库，当前页面问题：信息密度低、市场感缺失、只有本地手填和文件导入，缺外网一键安装能力。

本次重设计在保留既有能力的前提下，补齐业界 agent 产品（Claude Code marketplace、Dify 插件市场、Coze 插件生态）已经验证的两条路径：
1. **远程获取**：技能支持从外网 URL / GitHub 仓库 / 技能市场一键下载安装；连接器（MCP）支持配置外网 SSE / Streamable HTTP 端点（含鉴权 header、OAuth），一键连接测试。
2. **本地治理**：已安装条目保留 启用/停用、编辑、删除；连接器带连接健康状态。

## 功能需求（三版都必须完整表达）

### 三类对象（tab 切换，按功能分类）
- **工具 tool**：runtime = cli / http / function / plugin，带 endpoint。
- **技能 skill**：SKILL.md 正文 + scripts；来源标注（本地创建 / GitHub / 市场）、版本号、可更新提示。
- **连接器 mcp**：transport = stdio / sse / http(Streamable HTTP)；SSE/HTTP 需 URL + 鉴权（Bearer token 或 OAuth 授权按钮）；stdio 需 command。每个连接器有健康状态：已连接 / 待授权 / 已断开。

### 功能分类（7 类，必须保留）
工程研发 / 产品设计 / 质量测试 / 运维发布 / 知识资料 / 浏览器自动化 / 办公协作。

### 每个条目的治理操作（必须保留）
- 启用/停用开关（停用态条目视觉降级但仍可见）
- 编辑
- 删除（危险操作，视觉上要有确认暗示，如 hover 变红）

### 与 Tool Search 机制的呼应（差异化亮点）
- 条目可标记 **常驻 alwaysLoad**（每轮注入上下文）或 **按需 defer**（模型经 tool_search 检索后上浮）——用小标签表达。
- 显示「挂载到 N 个 Agent」计数。
- 页头显示启用预算：如「常驻 6 / 建议上限 12」，提示上下文成本。

### 新增流（至少画出入口与表单，不必真实现交互逻辑）
1. 技能「从外网安装」：输入 URL 或从市场列表挑选 → 显示安装进度 → 装完出现在列表。
2. 连接器「添加外网 MCP」：transport 选择（stdio / SSE / Streamable HTTP）→ 动态切换表单字段 → 「测试连接」→ 状态反馈。

## 示例数据（三版共用，禁止 Lorem）

工具：
- web_search · http · 工程研发 · 常驻 · 挂载 4 个 Agent · 启用
- code_runner · cli · 工程研发 · 按需 · 挂载 2 · 启用
- image_gen · function · 产品设计 · 按需 · 挂载 3 · 启用
- doc_export · plugin · 办公协作 · 按需 · 挂载 1 · 已停用

技能：
- log-analyzer · v1.2.0 · GitHub 来源 · 工程研发 · 有可用更新 v1.3.0 · 常驻 · 挂载 2
- ppt-builder · v0.9.1 · 市场 · 办公协作 · 按需 · 挂载 1
- frontend-review · 安装中（67%）· GitHub · 产品设计
- data-viz · 市场推荐未安装 · 质量测试

连接器：
- filesystem · stdio（npx -y @modelcontextprotocol/server-filesystem）· 知识资料 · 已连接 · 常驻 · 挂载 5
- browser · stdio · 浏览器自动化 · 已连接 · 按需 · 挂载 3
- postgres-mcp · SSE https://mcp.internal.xmly.dev/pg/sse · 工程研发 · 已连接 · 按需 · 挂载 2
- github-mcp · Streamable HTTP https://api.githubcopilot.com/mcp/ · 工程研发 · 待授权（OAuth）· 挂载 0
- notion-mcp · Streamable HTTP · 办公协作 · 已断开 · 按需 · 挂载 1

## 设计系统（从项目现有 tokens 出发，不得凭空发明）

浅色「编辑部风」（项目默认主题 paper）：
- 底色 `#f6f9fc`，次级底 `#edf1f7`，面板 `#ffffff`，面板次级 `#f8fafd`
- 线 `#e3e8ee` / 强线 `#cbd2dc`
- 正文 `#1a1f36`，弱化 `#697386`
- 语义绿 `#0e9f6e`（浅底 `#dff2e9`）、蓝 `#3b5bdb`（浅 `#6e8be8`）、危险红 `#c9503a`
- 字体：display 用宋体系（"Songti SC","Noto Serif SC",Georgia,serif），正文 `-apple-system,"PingFang SC",system-ui`，代码 `ui-monospace,"SF Mono",Menlo`
- 圆角 10px；页面有极淡网格纹理（`rgba(26,31,54,0.025)`）

允许在 tokens 内做布局与层级的重新演绎；禁止：紫渐变、emoji 图标、每卡片配无意义 icon、圆角卡+左彩边烂大街组合。图标一律不用（用文字缩写 monogram / 状态点 / 排版层级表达）。

## 输出要求（三版统一）

- 纯 HTML/CSS + 原生 JS 单文件，**零外部请求**（无 CDN、无 Google Fonts、无外链图片），双击可开。
- 桌面 1440×900 设计；至少可交互：三类 tab 切换、分类筛选 chip、一个「添加」modal（连接器版展示 SSE/HTTP 表单切换 + 测试连接按钮态；技能版展示 URL 安装输入）。
- 图标一律不用图片；条目图标用 1-2 字 monogram（如 `fs`、`pg`）+ 语义色。
- 页面语言：简体中文；产品名「Sortie Deck 工具箱」；顶部导航可简化为静态（作战面/任务/知识库/记忆/角色工厂/**工具箱**/角色频道/小队/决策中心/管理）。
- 文件命名：`toolkit-redesign-v1-*.html` / `v2-*` / `v3-*`，放在 `/Library/git-root/sortie-deck-todos/design-demos/`。
- 截图验证：`npx playwright screenshot "file://<path>" <out.png> --viewport-size=1440,900`，检查无 JS 报错。
