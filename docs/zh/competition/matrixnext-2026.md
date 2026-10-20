# Sortie Deck · MatrixNEXT 2026 作品说明文档

> **赛事**：[MatrixNEXT 2026](https://matrixorigin.cn/matrixnext) · AI Builder Festival  
> **赛道**：创意赛道 · 流程自动驾驶（工作流 / Agent 自动化）  
> **队伍**：SortieDeck（可按报名页最终队名替换）  
> **作品名**：Sortie Deck — AI 原生办公迭代控制面  
> **文档用途**：评委可访问的「说明文档链接」材料（含技术栈、创新点、使用说明）

---

## 1. 问题：我们已经有 AI，为什么协作还是乱的？

办公迭代里，产品 / 研发 / 测试往往已经各自使用 ChatGPT、Claude、Cursor 等工具，但**总线仍是人和 IM**：

1. 产品打开 AI → 写出需求文档  
2. 开澄清会 → 纪要再经 IM 转发  
3. 开发、测试各自把同一份需求丢进自己的 AI  
4. 联调、提测继续靠传话、截图当契约  

结果是：真相源碎成多份粘贴、航线不可见、门禁不可审计——看起来「很 AI」，实际仍是**传话筒式协作**。

![改造前 · 传话筒式 AI 协作](../assets/sortie-before-relay-handdrawn.jpg)

> 产品 → AI → 需求文档 → 澄清会议 → IM 传话 → 开发/测试各自的 AI → 提测？

---

## 2. 方案：把「聊天接力」换成控制面

**Sortie Deck** 是一个开源的 **AI Native 办公迭代控制面**（不是又一个聊天框）。

按任务集结角色 Agent，用 **LangGraph** 编排出击航线，在关键门禁保留**人在环（HITL）**，阶段之间用**产物契约**传递结果——产品 → 工程 → 测试 → 发布，不再依赖人工誊写。

| 改造前 | 改造后（Sortie Deck） |
|--------|----------------------|
| IM 当总线、人当传话筒 | 小队频道可见协作；IM 不再当真相源 |
| 每人各开 AI、粘贴接力 | 任务级角色 Agent + 可插拔执行器 |
| 截图 / 聊天当契约 | 命名产物文件，下游读文件 |
| 航线口头约定 | Brief → 航线规划官提案 → **真人确认** 后出击 |
| 群里 @ 当门禁 | LangGraph `interrupt()` HITL（可恢复、可审计） |

![改造后 · Sortie Deck 控制面](../assets/sortie-after-control-plane-handdrawn.jpg)

> Brief → 可确认航线 → 角色 Agent 按契约交付 → HITL 门禁 → 小队可见协作 → 提测 / 交付

与创意赛道「流程自动驾驶」的对应关系：从一个高频重复动作（办公迭代接力）出发，让 Agent **按可确认航线接手流程**，并在门禁处持续把结果反馈给真人。

---

## 3. 技术栈

| 层级 | 技术 |
|------|------|
| 控制面 / API | Python 3.12+ · FastAPI · Pydantic v2 · SSE |
| 编排 | LangGraph（StateGraph · `interrupt` / `Command` · checkpointer） |
| 存储 | JSON / SQLite（任务、队列）；产物本地或 S3 兼容 |
| 执行器 | `mock` · `claude_code` · `cursor_cli` · `deploy_shell` · 社区入口组 |
| 工作台 | React 19 · TypeScript · Vite |
| 知识 / 记忆 | 本地 Codex 知识库 · 可选 WeKnora；多作用域记忆 |
| 工具箱 | Tools · MCP（连接器）· Skills（按领域目录浏览） |
| 文档 | MkDocs Material（中英双语） |
| 许可 | Apache-2.0 |

仓库（开源）：[lpliu-art/sortie-deck-todos](https://github.com/lpliu-art/sortie-deck-todos) · 分支 `dev`

---

## 4. 创新点

1. **控制面优先，而不是聊天优先**  
   任务、航线阶段、HITL、产物都是一等公民；Agent 是执行单元，不是唯一界面。

2. **可确认航线（Pipeline Specs First）**  
   规划官根据 Brief 提案节点（含并行分支）；**真人确认或手改后才编译图并出击**，避免「Agent 自说自话跑飞」。

3. **契约化产物，而不是粘贴接力**  
   阶段写出命名文件；下游读文件。并行阶段用路径作用域避免互相覆盖。

4. **人在环是图语义，不是群里 @ 一下**  
   用 LangGraph `interrupt()` 暂停，支持放行 / 打回 / 改写指令 / 停止 / 重路由，状态可恢复、可审计。

5. **可组合编制**  
   角色工坊发布人设；工具箱按领域挂载 Tools / Skills / 连接器；记忆与知识跨任务复利。

6. **可插拔执行器**  
   编码阶段可接 Claude Code / Cursor CLI；CLI 缺失时 soft-fail 并给出可操作提示，不静默假成功。

---

## 5. 使用说明（评委 / 演示路径）

### 5.1 本地快速启动

```bash
git clone https://github.com/lpliu-art/sortie-deck-todos.git
cd sortie-deck-todos
git checkout dev
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# API（默认 http://127.0.0.1:8787）
sortie api

# 另开终端：工作台（默认 http://127.0.0.1:5173）
cd apps/web && npm install && npm run dev
```

开发种子账号（本地演示）：

| 用户 | 密码 | 角色 |
|------|------|------|
| `admin` | `admin123` | admin |
| `operator` | `operator123` | member |

冒烟（自动确认航线并走过 HITL）：

```bash
sortie smoke
```

### 5.2 推荐演示剧本（约 3–5 分钟，可剪成 ≤3 分钟视频）

1. **登录工作台** → 进入建队 / 新建任务，填写 Brief（示例：多端小功能或热修）。  
2. **查看航线规划官提案** → 展示 express / standard / 自定义节点；可微调阶段与 HITL 开关。  
3. **确认航线并出击** → 观察轨道上阶段推进与并行分支（若有）。  
4. **命中 HITL** → 在工作台或小队频道 `/approve` · `/rewrite` · `/reject`。  
5. **查看产物** → `data/artifacts/<initiative>/<stage>/` 或工作台产物面板，强调「读文件不读聊天」。  
6. **（可选）** 工具箱按领域浏览连接器 / 技能；知识库或记忆挂载到任务。

### 5.3 在线演示环境（提交前填写）

| 项 | 内容 |
|----|------|
| 可运行智能体 / 工作台链接 | _待部署后填写，例如 `https://…`_ |
| 演示账号 | _待填写（建议只读或演示专用账号）_ |
| 访问密码（如有） | _无则写「无」；有则在此注明_ |
| 演示视频链接 | _待上传后填写（≤3 分钟）_ |
| 源码仓库 | https://github.com/lpliu-art/sortie-deck-todos/tree/dev |

> 报名与作品提交是两个环节：完成本文档后，请在活动页「作品提交」入口用同一 MOI 账号提交三项链接。

### 5.4 常用斜杠与点名（小队频道）

`/confirm` · `/start` · `/approve` · `/reject` · `/rewrite …` · `/stop`  

`@product` · `@eng` · `@qa` · `@deploy`

---

## 6. 系统架构（摘要）

```text
工作台 (React) ──HTTP/SSE──► FastAPI 控制面
                                │
                                ▼
                           Orchestrator
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
           LangGraph 图    产物/任务存储    执行器注册表
           (HITL interrupt)                 (编码/发布/mock)
```

更多设计见：

- [为什么需要 Sortie Deck](../design/why-sortie-deck.md)  
- [改造方案介绍](../design/transformation.md)  
- [架构总览](../architecture/overview.md)

---

## 7. 与评分维度的对应（自评提纲）

| 维度 | 本作品如何体现 |
|------|----------------|
| 主题契合度 | AI Agent 编排真实办公交付流程，而非单点问答 |
| 技术实现度 | 可运行开源仓库 + LangGraph HITL + 可插拔执行器 + 工作台 |
| 业务价值 | 减少传话成本、统一真相源、门禁可审计，可落地小团队本地部署 |
| 创新性 | 「可确认航线 + 产物契约」把控制面与聊天机器人区分开 |
| 展示效果 | 改造前/后手绘图 + 一条完整出击 Demo（建议视频按第 5.2 节剪辑） |

---

## 8. 许可与联系

- 许可证：Apache-2.0  
- 仓库 Issues：https://github.com/lpliu-art/sortie-deck-todos/issues  
- 队长联系方式：_报名页所填邮箱 / 手机（此处可不公开）_

---

*文档版本：MatrixNEXT 2026 提交用 · 随 `dev` 分支更新*
