# 航线规划

## 1. 概述

内部 Agent **航线规划官**（`pipeline_planner`）根据简报提案节点；出击前须确认或手改。这是创建任务后的**配置入口**。

| 项 | 值 |
|----|-----|
| 代码 | `pipeline.py` |
| 模板 | `packages/templates/{express,standard,default}_pipeline.yaml` |
| Agent | `pipeline_planner` |

## 2. 范围与非目标

**范围内**

- 启发式选轨 + 增删节点
- 阶段目录并集；normalize intake…done
- 物化为 LangGraph 模板 dict
- API：tracks / suggest / catalog；任务 patch/confirm

**非目标**

- 未经确认自动出击
- 未注册的随意执行器（P2 插件）

## 3. 航线

| 航线 | 适用 | 典型节点 |
|------|------|----------|
| express | 热修 / 一行 | intake → eng → qa → deploy → done（提到设计/多端时自适应加节点） |
| standard | 正常需求 | PRD → **技术方案** → **设计** → 用例 → 开发 → 自测 → 联调 → 提测 → qa → deploy → done |
| default | 办公迭代 | PRD → **技术方案** → **设计** → 开发 → qa → deploy → done |

### 3.1 技术方案阶段（`eng_tech_design`）

在 `product_prd` 之后、任何 build 阶段之前运行（仅 standard/default —— express
热修线不加）。eng 角色读取 PRD 与验收标准，产出可评审的 `tech_design.md`
（技术路线、模块拆分、风险、上线/回滚方案），由阶段契约校验并在其
`hitl_after` 门禁评审；后续 build 阶段按已批准的技术方案实现。任务锁定
harness 时该阶段与其他阶段一样走 `harness_stage` 执行器；mock 回退为
`mock_tech_design`。

## 3.2 角色编制位

| 编制位 | 说明 |
|--------|------|
| `product` | 产品 / PRD |
| `design` | 设计（交互 / 视觉） |
| `eng` | 通用工程（快轨或未指明端） |
| `eng_ios` / `eng_android` / `eng_web` / `eng_backend` / `eng_agent` | 研发细化 |
| `qa` | 测试 |
| `deploy` | 发布 |

简报命中 iOS / Android / Web / 后端 / Agent 时，规划官用对应 `eng_*` 节点替换通用 `eng_implement`。

## 4. 提案 schema

```json
{
  "agent": "pipeline_planner",
  "track_hint": "express",
  "rationale": "...",
  "status": "pending|confirmed|edited",
  "stages": [
    {"id": "eng_implement", "label": "...", "role": "eng", "hitl_after": false}
  ]
}
```

落在任务 `meta`：`pipeline_proposal`、`pipeline_specs`、`pipeline_rail`、`pipeline_confirmed`。

## 5. API

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/api/pipelines` | tracks + catalog |
| GET | `/api/pipelines/suggest` | `plan_pipeline` 预览 |
| GET | `/api/pipelines/catalog` | 阶段目录 |
| PATCH | `/api/initiatives/{id}/pipeline` | 规范化；可选确认 |
| POST | `/api/initiatives/{id}/pipeline/confirm` | 确认 |

## 6. 算法（现状）

1. 显式 hint / complexity / 正则启发式解析航线。
2. 加载 YAML 阶段。
3. 自适应增减（如 express 提到用例 → 插入 `qa_cases`）。
4. 修正 `on_fail` / `on_reject` 到存在 id。
5. 返回 pending 提案。

## 7. 技术方案（目标）

```text
PlannerBackend
  HeuristicPlanner
  LlmPlanner          # 同 Proposal schema
HumanOverride 确认时永远优先
PolicyPacks: regulated | hotfix | frontend_only
```

UI：确认前展示提案 vs 手改 diff。

## 8. 开发计划

### P0（2–3 天）

| 任务 | 验收 |
|------|------|
| 金牌简报 → 期望航线与关键节点 | pytest 表驱动 |
| 目录与 YAML 完整性 | CI 断言 |
| `diff_specs` | 单测 |

### P1（1 周）

| 任务 | 验收 |
|------|------|
| `LlmPlanner` + 环境开关 | 失败回退启发式 |
| meta 写入 confidence/notes | UI 展示 |
| 策略包 YAML | 可加载 pack id |

### P2（2 周）

| 任务 | 验收 |
|------|------|
| 自定义阶段插件进目录 | 动态 id |
| 组织默认航线策略 | 管理配置 |

## 9. 测试计划

- 扩展 `test_pipeline_tracks.py`
- 性质：normalize 必以 intake 始、done 终
- 确认后编译图 HITL 边完整

## 10. 依赖

- Graph 构建、Orchestrator 确认、Web 编辑器、新阶段 Contracts
