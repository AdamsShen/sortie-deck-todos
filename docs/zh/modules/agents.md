# 角色工坊

## 1. 概述

发布专业角色（人设、编制位、可见性、默认 `toolkit_ids`）供任务点名。

| 项 | 值 |
|----|-----|
| 代码 | `agents.py` |
| 存储 | `data/agents.json` |
| API | `/api/agents` CRUD |

## 2. 模型

`PublishedAgent`：slot、persona、persona_spec、toolkit_ids、visibility、version…
创建任务时 `resolve_slot_map`。

### 人设结构（六段式）

角色提示词按提示词工程范式分六段：**角色 / 背景 / 技能 / 工作流 / 示例 / 约束**。

- `persona`：编译后的 system prompt，**运行时唯一来源**（`graph` / `executor` / `plugins` 只读它）。
- `persona_spec`：角色工坊的结构化编辑视图（`persona.py::PersonaSpec`）；保存时由
  `render_persona` 编译为 `persona`，两者不会出现运行时不一致。
- 旧数据只有 `persona` 时，API 读取会用 `parse_persona` 现解析一份只读 spec，前端即可直接分段编辑。
- 约束：角色段必须写明「10 年经验」；forge 自检会检查技能 ≥ 3、示例 ≥ 1、约束 ≥ 3。

不同职能的写法侧重（参考 DeepSeek 官方提示词库的功能型模板）：

| Agent 类型 | 写法要点 |
|---|---|
| 决策 / 写作型（product、design、deploy） | 定位、能力、知识储备、输入输出说明 |
| 评测 / 裁决型（qa） | 判定标准前置，pass/fail 可观测、可复现 |
| 工程实现型（eng_*） | 工作流固定「读产物 → 说明方案 → 最小 diff → 自检」 |
| 结构化产出型 | 约束中写死字段、类型、缺失值约定 |

## 3. 种子角色（开发）

新建 `data/agents.json` 时内置公开专家：

| Slug | 编制位 | 说明 |
|------|--------|------|
| `prd-scout` | product | 前场 PRD 侦察 |
| `design-navigator` | design | 交互 / 视觉规格 |
| `platform-eng-raider` | eng | 通用工程突击 |
| `ios-raider` | eng_ios | iOS |
| `android-raider` | eng_android | Android |
| `web-raider` | eng_web | Web 前端 |
| `backend-raider` | eng_backend | 后端 / API |
| `agent-raider` | eng_agent | Agent / MCP / 评测 |
| `qa-gatekeeper` | qa | 门禁 QA |

编制位：`product` · `design` · `eng` · `eng_ios` · `eng_android` · `eng_web` · `eng_backend` · `eng_agent` · `qa` · `deploy`。

## 4. 流程

专家发布（可带默认工具）→ 建任务点名 → 运行时用已发布 persona。

## 5. 技术方案（目标）

- semver + 任务钉扎
- 人设评测架
- 组织目录同步

## 6. 开发计划

### P0（1–2 天）

| 任务 | 验收 |
|------|------|
| API/UI 展示 toolkit_ids | 可见 |
| 仅作者可改测试 | pytest |
| 种子角色说明 | README |

### P1（1 周）

| 任务 | 验收 |
|------|------|
| patch 升 version；创建钉扎 | meta 有 pin |
| 克隆角色 | 新 slug |

### P2（2 周）

| 任务 | 验收 |
|------|------|
| 评测 CLI | 出分报告 |
| 远程组织目录 | pull |

## 7. 依赖

- Toolkit、Orchestrator 编制、Web 工坊
