# 记忆

## 1. 概述

按作用域存记忆；组任务时注入上下文块。

| 项 | 值 |
|----|-----|
| 代码 | `memory.py` |
| 存储 | `data/memory.json` |
| API | `/api/memory` |

## 2. 作用域与类型

| 作用域 | scope_id | 用途 |
|--------|----------|------|
| workspace | 全局 | 团队约定 |
| user | 用户 id | 个人偏好 |
| mission | 任务 id | 战例事实 |
| agent | agent id | 角色笔记 |

类型：`fact` / `preference` / `episode` / `lesson`。  
`context_block` 在 `create_initiative` 时拼接。

### 2.1 共享后端

`TDT_MEMORY_BACKEND` 选择共享记忆层：`local`（默认）或 `mem0`。选 `mem0`
时本地存储被包装为 `CompositeMemory`：mission 范围写入镜像到 mem0
（`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY` / `TDT_MEM0_USER_PREFIX`），检索命中
并入上下文块，按 `user_id = "<prefix>:<initiative_id>"` 任务级隔离。远端失败
静默降级本地。任务绑定文件（滚动总结 / 会话转录 / 上下文文件）见
`mission_memory.py` 与 `harness.md`。

## 3. 技术方案（目标）

- 近因 × 重要性排序
- 任务 done 自动写 lesson
- user 域静态加密
- TTL 清理

## 4. 开发计划

### P0（1–2 天）

| 任务 | 验收 |
|------|------|
| UI 作用域文案 | 少选错 |
| context_block 单测 | pytest |
| brief 长度硬顶 | 截断 |

### P1（1 周）

| 任务 | 验收 |
|------|------|
| DONE 写 lesson | 有记录 |
| importance 字段 | 参与排序 |

### P2（1–2 周）

| 任务 | 验收 |
|------|------|
| user 域加密 | env 密钥 |
| TTL 清扫 | 过期删除 |

## 5. 依赖

- Orchestrator 创建、Web 记忆页、Auth 用户 id
