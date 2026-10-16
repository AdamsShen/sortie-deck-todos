# 任务记忆（mission_memory）使用说明

## 1. 概述

任务绑定文件记忆：每个任务一个目录，滚动总结 + 会话转录 + 需求绑定文件，
新 stage 开始时自动注入 prompt。设计原理见
[principles/mission-memory.md](../principles/mission-memory.md)。

| 项 | 值 |
|----|-----|
| 代码 | `mission_memory.py` |
| 存储 | `data/missions/<initiative_id>/memory/` |
| 读写入口 | `harness_providers/executor.py` |
| 配置 | `TDT_MISSION_CONTEXT_TOKEN_LIMIT`（token 预算） |

## 2. 目录布局

```
data/missions/<initiative_id>/memory/
├── memory.md              # 滚动总结（自动追加 + LLM 压缩）
├── sessions/<sid>.jsonl   # 每 stage 会话转录
└── context/<name>.md      # 需求绑定文件
```

## 3. 数据产生时机

| 时机 | 动作 | 产物 |
|------|------|------|
| 任务启动（`start_pipeline`） | 写入 brief / plan / harness 三个绑定文件 | `context/brief.md`、`context/plan.md`、`context/harness.md` |
| 每个 stage 执行后 | 记录转录 + 追加滚动总结 | `sessions/<sid>.jsonl` 追加一行；`memory.md` 追加一节 |
| `memory.md` 超预算 | LLM 压缩并写回（失败降级为截断） | `memory.md` 重写 |
| 新 stage bootstrap 超预算 | 再触发一次压缩 | 同上 |

## 4. 数据消费时机

唯一消费方是 `HarnessStageExecutor`：每个 stage 开始时
`bootstrap_context` 组装 `ContextPack`（滚动总结全文 + context 文件
单个截 6000 字符 + artifact 索引），拼进 stage prompt。API 层无暴露。

## 5. 已知限制

- 转录只存 prompt/reply 头 2000 字符，不含工具调用记录，且当前只写不读
  （`ContextPack.session_log` 未进入 prompt）。
- `memory.md` 为流水账式追加，长任务压缩频繁，总结质量受截断影响。
- context 注入为线性拼接，无检索。
