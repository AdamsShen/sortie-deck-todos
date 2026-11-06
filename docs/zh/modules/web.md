# Web 工作台（Signal Deck）

## 1. 概述

工作台是真人的 **UI 入口**：登录、作战图、任务、小队、航线确认/编辑、Codex、记忆、角色工坊、工具箱、管理、中英、主题。

| 项 | 值 |
|----|-----|
| 技术栈 | Vite + React + TypeScript |
| 根目录 | `apps/web` |
| API | 同源 / 代理到 `:8787` |

## 2. 范围与非目标

**范围内**

- 上述已登录 SPA 视图
- 草稿期航线提案确认与手动改节点
- 操作员身份 chip（头像 / 角色 / callsign）
- 中英 i18n、主题坞

**非目标**

- 原生移动 App
- 独立设计系统发包
- SSR / Next 迁移（仅 P2 讨论）

## 3. 代码地图

| 路径 | 职责 |
|------|------|
| `apps/web/src/App.tsx` | 单体视图路由 + 状态（待拆） |
| `apps/web/src/types.ts` | DTO |
| `apps/web/src/i18n.ts` | 文案 |
| `apps/web/src/styles.css` | 主题 token + 布局 |
| `apps/web/src/themes.ts` | 主题 id / 持久化 |
| `apps/web/src/RippleField.tsx` | 氛围特效 |

## 4. 视图地图（入口 → 深度）

```text
login
  → home（作战图）
  → tasks（新建 + 列表）→ squad（频道 + 航线编辑 + HITL）
  → knowledge | memory | agents | toolkit | admin
```

## 5. 关键 UX 契约

### 设置中心 · Git 凭据的只读可见性

- 「Git 凭据」页对所有已登录用户开放；工作区凭据与工作区仓库（名称 / 地址 / 默认分支 / 绑定凭据）全员可查看。
- 仅 `admin` 能看到工作区凭据与工作区仓库的新增、停用入口；`member` 只读。个人凭据与个人私有仓库仍只有本人可见。
- 后端 `GET /api/git/workspace-repos`、`GET /api/git/workspace-credentials` 是 `require_user` 的脱敏只读接口；`/api/admin/git/*` 写端点由 `require_admin` 把关。

### 航线确认（小队草稿）

1. 展示规划官 rationale + `meta.pipeline_specs`
2. 编辑：重排 / 删除 / 从目录添加 / 开关门禁
3. `POST …/pipeline/confirm` 或带 `confirm` 的 `PATCH …/pipeline`
4. 未 `pipeline_confirmed` 不可出击

### 认证

- Token：`localStorage.sortie_token`
- 401 → 清 token → 登录页

## 6. 技术方案

### 现状

- 大部分状态与请求在 `App.tsx`
- 选中任务时 SSE 订阅事件

### 目标

```text
apps/web/src/
  api/client.ts
  features/auth|tasks|squad|...
  components/OperatorChip, PipelineEditor, PipelineRail
  routes.tsx
```

- React Query 管服务端状态
- 路由级拆包
- 无障碍：焦点环、reduced-motion（已部分支持）

## 7. 开发计划

### P0 — 入口面组件化（3–5 天）

| 任务 | 验收 |
|------|------|
| 抽出 `OperatorChip` / 顶栏 | 视觉不回退 |
| 抽出 `PipelineEditor` | 确认/编辑仍可用 |
| 抽出 `api()` 客户端 | 请求统一走 client |
| 冒烟：登录→创建→确认→出击 | 手测清单 |

### P1 — 路由与数据层（1–2 周）

| 任务 | 验收 |
|------|------|
| React Router | 深链 `/squad/:id` |
| initiatives/agents/toolkit 缓存 | 少重复拉取 |
| Error boundary + toast | API 失败不白屏 |

### P2 — 质量门禁（1–2 周）

| 任务 | 验收 |
|------|------|
| Playwright：登录+建任务 | CI |
| Storybook（Rail/chip） | 可选 |
| 包体预算 | 记录 Lighthouse 基线 |

## 8. 测试计划

- `npx tsc --noEmit`
- 手测中英、主题切换
- 航线编辑：不可删 `intake`/`done`

## 9. 依赖

- 依赖：Auth、Initiatives/Pipeline API
- 阻塞：多数人对人演示路径
