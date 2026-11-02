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
- `sortie mission create / bind-repos / attach / start / forge` — 装配式建任务（与 MCP 工具面对齐）

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

任务相关命令分两类：**一步式**（`mission start --brief`）与**装配式**（`create` → `bind-repos` / `attach` → `start <id>`）。

```bash
# 装配式：先建草稿，绑仓库、挂材料，再启动
sortie mission create --brief "给设置页加导出按钮" --track standard
sortie mission bind-repos ini_xxx --repo eng=git@github.com:org/web.git#dev
sortie mission attach ini_xxx --file prd.md --kind prd --pinned
sortie mission start ini_xxx --wait

# 建队缺角色时：先看草稿，再审批
sortie mission forge list ini_xxx
sortie mission forge approve fgd_xxx
```

- `--repo SLOT=URL[@BRANCH]` 可重复；`SLOT` 取 `product/eng/qa/deploy`，分支带 `/` 时用 `URL#branch`
- `--file` 只收文本（二进制请在网页上传原件），`--pinned` 的材料进每个阶段提示词
- 仓库只能在**启动前**绑定，启动后返回 `conflict`（退出码 5）
- 远程与嵌入式两种模式行为一致；嵌入式下 `start` 会一直等到门禁（进程即执行者）

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
