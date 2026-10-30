# 任务并发与多进程执行能力

日期：2026-10-30
状态：已实施（2026-10-30，验收与端到端结果见第七节）

## 一、背景

当前系统存在四类阻塞「多人同时使用 / 对外提供 MCP 服务」的问题：

| # | 问题 | 现状证据 |
|---|---|---|
| 1 | 进程内任务串行 | `orchestrator.py` 的 `self._lock` 包住整段 `graph.astream`，一个进程同一时刻只能跑一个 mission |
| 2 | 默认 JSON 存储不支持并发写 | `store.py` 的 `upsert` 是「整文件读-改-写」，并发必然丢更新；全局锁实质是在替它兜底 |
| 3 | 租约不续期 | `SqliteRunQueue.heartbeat` 无任何调用点，`run_lease_seconds=120` 到期后另一个 worker 会把运行中的任务重复领取 |
| 4 | 无独立 worker 入口 | 只有 `sortie api` 会启动 worker loop，`worker_id` 默认恒为 `worker-1`，多副本部署会互相踩 |

由此推论：**并发上限 = 进程数 = 1**。`standard` 航线单任务 10–60 分钟，5 人同时使用排队 1–5 小时。

## 二、业内调研

1. **长任务不应阻塞协议请求**：MCP 官方 Tasks 扩展（SEP-1686，2025-11 实验性）明确「call-now, fetch-later」，把耗时操作转成任务句柄轮询；[Tasks 概览](https://modelcontextprotocol.io/extensions/tasks/overview)。
2. **执行与接入分离**：LangGraph 的部署形态是「API 只负责写入/查询图状态，执行由独立 worker 消费队列」，与 Celery/Arq 的 producer-consumer 一致。这样 API 可以水平扩，worker 按 CPU/CLI 额度扩。
3. **状态存权威源**：多进程下事件流不能走进程内队列，必须先落持久层再由任意进程读取（LangGraph 的 checkpointer + 事件表即此模式）。
4. **并发粒度是「任务」而不是「进程」**：同一 mission 必须互斥（避免双写 checkpoint），不同 mission 必须可并行。

与本项目现状的差异：项目已经有 lease 队列与 SQLite 仓库，属于「设计对了、只实现了一半」，因此不需要引入新中间件。

## 三、技术设计

### 3.1 并发粒度：per-initiative 锁

- `Orchestrator._lock`（全局）→ `_run_locks: dict[initiative_id, asyncio.Lock]`，`_run_locks_guard` 只保护字典本身。
- 语义：**同一 mission 的 start/resume 互斥；不同 mission 完全并行。**
- `submit_hitl` 中「若上一段运行还在跑，等它停到门禁再投递 resume」保持原语义，只是等待对象从全局锁改为该 mission 的锁。

### 3.2 存储：并发安全 + 自动迁移

- `InitiativeStore`（JSON）加进程内 `RLock`：单进程并发读写不再互相覆盖。
- `SqliteInitiativeStore` 加 `WAL` + `busy_timeout=5000` + 连接锁：支持多进程。
- **存储后端收敛为 sqlite 一种**（2026-10-30 决定，按用户要求不再兼容旧逻辑）：`TDT_STORAGE` 默认 `sqlite`，`build_initiative_store` 只返回 SQLite 实现，配成 `json` 直接在启动期报错——不做「静默降级到 JSON」，否则并发丢数据会以最难排查的方式出现。
- `Orchestrator.startup()` 做一次性迁移：sqlite 表为空且 `initiatives.json` 存在时导入，迁移前 `copy2` 备份为 `initiatives.json.bak-<时间戳>`，迁移后写 `data/.storage-migration.json`。marker 的作用是启动时比对 JSON 的 mtime：若迁移之后 JSON 仍被写入，说明旧版本进程还在跑，日志告警（只告警不自动合并，不替用户猜谁是权威源）。

### 3.3 租约续期

- `_worker_loop` 领取 job 后拉起 `_lease_heartbeat` 后台任务，按 `run_lease_seconds/3`（最小 5s）续租，job 结束后取消。
- `worker_id` 默认值改为 `{hostname}-{pid}`，显式配置仍优先；写死同名会让 `claim()` 的 `lock[0] != worker_id` 互斥判断失效。

### 3.4 独立 worker 入口

- 新增 `TDT_RUN_WORKER`（默认 true）。为 false 时 `startup()` 不启动 worker loop，该进程只做 API/前端。
- 新增 `sortie worker` 子命令：要求 `TDT_RUN_BACKEND=sqlite`，只跑 worker 不监听 HTTP，可水平扩副本。
- **每个 worker 进程一次只跑一个 job**（`_worker_loop` 顺序 claim → await → complete）。这是刻意的：一个 mission 会拉起完整 Agent 链（多 CLI 子进程），单进程叠加会让资源不可控。并发度 = worker 副本数；`inline` 模式下则由 `asyncio.create_task` 在一个进程内并行。

### 3.5 跨进程事件流

- 新增 `event_log.py`：`mission_events(seq, initiative_id, payload, created_at)`，WAL + 索引，按 seq 顺序读。
- `Orchestrator._emit` 先 append 到事件表（拿到 seq），再扇出到进程内订阅者（兼容旧调用）。
- SSE 路由改为**以事件表为唯一数据源**：连接时先发 `snapshot` + `room_snapshot`，随后按 `seq > last_seq` 轮询（300ms），15s 无事件发 `ping`。
- 好处：worker 进程与 API 进程解耦；断线重连不丢事件；顺带修掉「重连后错过事件」的老问题。
- 保留期 `TDT_EVENT_RETENTION_DAYS`（默认 7 天），随 `_sweep_loop` 每小时清理；删除 mission 时同删其事件。

## 四、文件清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/event_log.py` | 新增：持久事件日志 |
| `src/sortie_deck/settings.py` | 新增 `run_worker` / `event_retention_days` / `events_sqlite`；`worker_id` 默认置空 + `resolved_worker_id` |
| `src/sortie_deck/store.py` | JSON 存储加 RLock |
| `src/sortie_deck/repositories/sqlite_initiatives.py` | WAL + busy_timeout + 连接锁 |
| `src/sortie_deck/orchestrator.py` | per-initiative 锁、事件落库、heartbeat、`run_worker` 开关、启动迁移、事件清理 |
| `src/sortie_deck/api/app.py` | SSE 改为事件表续读 |
| `src/sortie_deck/cli.py` | 新增 `sortie worker` |
| `src/sortie_deck/runtime.py` | 存储/队列组合强校验；doctor 增加存储与 worker 检查 |
| `tests/test_concurrency.py` | 新增：并发执行、锁隔离、heartbeat、事件日志 |
| `CHANGELOG.md` / `CLAUDE.md` / `docs/{zh,en}` | 配置项与行为变更同步 |

## 五、验收标准

1. 两个 mission 同时 `start_pipeline`，两者的 `astream` 能同时进入（用屏障图验证，串行实现会超时）。
2. 同一 mission 的第二次 run 仍互斥。
3. `run_backend=sqlite` + `storage=json` 启动报错，错误信息给出修复方式。
4. worker 运行超过 `run_lease_seconds` 后，另一个 worker 无法重复领取该 job。
5. 事件表能跨 `EventLog` 实例（模拟跨进程）按 seq 读到全部事件。
6. `sortie worker` 在 inline 模式下给出明确报错；在 sqlite 模式下可持续消费队列。

## 六、备选方案与取舍

| 方案 | 结论 |
|---|---|
| 引入 Celery/Redis 做队列 | 否决：现有 lease 队列已够用，多一个外部依赖只会增加部署成本 |
| 直接去掉锁（不做 per-initiative） | 否决：同一 mission 的 resume 会双写 checkpoint |
| 事件走 Redis pub/sub | 否决（当前阶段）：sqlite 事件表已满足，且自带持久化与重连续读 |
| 保留进程内队列 + 心跳检测补漏 | 否决：跨进程永远会漏事件，且要两套路径 |

## 七、实施记录（2026-10-30）

| 验收项 | 结果 |
|---|---|
| 不同 mission 同时进入 `astream` | `tests/test_concurrency.py::test_two_missions_run_concurrently`（屏障图，串行实现会超时） |
| 同一 mission 的 run 互斥 | `test_same_mission_run_is_mutually_exclusive` |
| `storage=json` 启动失败（任何 run_backend） | `test_storage_backend_must_be_sqlite`、`test_factory_only_accepts_sqlite` |
| 旧 JSON 导入 + 备份 + marker | `test_legacy_json_missions_migrate_to_sqlite` |
| 迁移后 JSON 又被写 → 告警 | `test_warns_when_legacy_json_written_after_migration` |
| 长任务租约被续期 | `test_lease_heartbeat_keeps_long_job_locked` |
| 事件跨实例（跨进程）按 seq 可读 | `test_event_log_is_readable_across_instances` |
| SSE 按事件表补播 + 小群过滤 | `test_sse_stream_replays_persisted_events` |
| `sortie worker` 拒绝 inline 模式 | `test_worker_cli_requires_sqlite_backend` |

端到端（API `TDT_RUN_WORKER=false` + 独立 `sortie worker` + sqlite 存储）：

- 两个任务均跑到 `done`，事件表每个任务各 74 条；
- API 进程的 SSE 收到 worker 进程写入的 70 个事件（`snapshot` / `room_snapshot` / `progress` / `update` / `hitl` / `room_message` / `ping`）；
- worker 身份自动派生为 `{hostname}-{pid}`，API 日志无异常。

全量测试：`602 passed`（3 个改动前既已失败的用例已单独确认与本次改动无关）。

## 八、风险

- 事件表写入在事件循环内同步执行（与项目内既有 sqlite 用法一致），单次 <1ms；若后续事件量增大再迁 `asyncio.to_thread` 或改批量写。
- `update` 事件携带整个 mission 快照，事件表体积随任务规模增长；由保留期与删除任务时清理兜住。
- LangGraph 同一编译图被多个 mission 并发 `astream` 依赖其无共享运行态；已通过并发冒烟测试覆盖。
