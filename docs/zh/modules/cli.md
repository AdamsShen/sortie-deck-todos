# 命令行

## 1. 概述

CLI 是**无浏览器的开发入口**：冒烟跑通任务环、拉起 API 进程。

| 项 | 值 |
|----|-----|
| 脚本 | `tdt`、`tdt-api`（`pyproject.toml`） |
| 代码 | `src/sortie_deck/cli.py`、`api/app.py:run` |

## 2. 范围与非目标

**范围内**

- `sortie smoke` — 自动确认航线并推 HITL
- `sortie api` / `tdt-api` — uvicorn 入口
- `sortie worker` — 只消费运行队列的执行进程（不监听 HTTP，可水平扩副本）
- `sortie doctor` — 运行时体检（含存储/队列组合校验）

**非目标**

- kubectl 式完整任务管理（P1+）
- 交互 TUI（P2）

## 3. 现有命令

```bash
sortie smoke [--brief "..."]
sortie api
# 多 worker：API 端 TDT_RUN_WORKER=false，执行端按副本数起 worker
TDT_RUN_BACKEND=sqlite TDT_STORAGE=sqlite sortie worker --id w1
```

实现要点：构造 `Orchestrator`，走创建/确认/出击，轮询 HITL 并 `APPROVE` 直到完成。

## 4. 技术方案（目标）

```text
tdt
  doctor
  smoke
  api
  mission create|confirm|start|status
  version
```

脚本友好：`--json`。

## 5. 开发计划

### P0（1 天）

| 任务 | 验收 |
|------|------|
| 完善 `--help` | README 有说明 |
| smoke 打印航线节点 | 可读 |
| 失败非 0 退出 | 可进 CI |

### P1（3–5 天）

| 任务 | 验收 |
|------|------|
| `sortie doctor` | 检查 venv、端口、data 可写、secret |
| `tdt mission status <id>` | 打印 status/stage |
| smoke/status 支持 `--json` | 可解析 |

### P2（1 周）

| 任务 | 验收 |
|------|------|
| CLI 内 SSE 进度 | 对齐工作台 |
| `tdt executors` | 对齐 `/api/executors` |

## 6. 测试计划

- 临时 `data_dir` 下跑 smoke（pytest 或子进程）
- doctor 在端口占用时明确失败（可选）

## 7. 依赖

- Orchestrator、航线确认语义、mock 执行器
