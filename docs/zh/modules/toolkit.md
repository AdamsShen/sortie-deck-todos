# 工具箱

## 1. 概述

Tools / MCP / Skills（含脚本）目录。绑定到角色 / 编制位 / 阶段；现状注入为 persona 工具简述。

| 项 | 值 |
|----|-----|
| 代码 | `toolkit.py` |
| 存储 | `data/toolkit.json` |
| API | `/api/toolkit`、`/import` |
| 示例 | `packages/examples/demo-mcp.json`、`packages/examples/demo-skill/SKILL.md` |

## 2. 类型

| 类型 | 关键字段 | 导入 |
|------|----------|------|
| tool | runtime、endpoint | 手工 |
| mcp（连接器） | transport、command/url… | mcp.json |
| skill | body、scripts[] | SKILL.md / zip |

工作台按 **工具 / 技能 / 连接器** 页签浏览，并可用 `category` 领域目录筛选（engineering · product · qa · ops · knowledge · browser · office），`featured` 进入精选区。

## 3. 绑定解析

运行时优先级：`stage_toolkits` > `role_toolkits` / `RoleAgent.toolkit_ids` > 角色默认。编排器生成 `toolkit_briefs`。

## 4. 技术方案（目标）

现状：给模型/CLI 看的文本。  
目标：真实调用层 `McpSessionManager` / `SkillScriptRunner` / `ToolProcessRunner`；密钥用引用而非明文（P1）。

## 5. 开发计划

### P0（2–3 天）

| 任务 | 验收 |
|------|------|
| `packages/examples/` 示例 | 导入成功 |
| 绑定说明文档 | README+本文 |
| 停用项不进 brief | 测试 |

### P1（1–2 周）

| 任务 | 验收 |
|------|------|
| 编码执行器 MCP session | 有调用日志 |
| env 密钥引用 | JSON 无明文 |
| 脚本后缀白名单 | 拒绝非法 |

### P2（2 周）

| 任务 | 验收 |
|------|------|
| 编制位 ACL | 未授权拒绝 |
| URL 市场导入 | 签名包 |

## 6. 测试计划

- import 用例；brief 仅含选中 id

## 7. 依赖

- Agents、Orchestrator、Graph、Web 工具箱页
