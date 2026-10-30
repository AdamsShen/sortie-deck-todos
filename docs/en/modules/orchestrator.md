# Initiatives & Orchestrator

## 1. Overview

The orchestrator is the **control-plane core** after login: mission lifecycle, pipeline confirm binding, graph selection, HITL resume, squad posts, SSE, artifact sync.

| Item | Value |
|------|-------|
| Code | `orchestrator.py`, `store.py` |
| Store | `data/initiatives.sqlite` (only backend; a legacy `initiatives.json` is imported and backed up on first start) |
| Runtime graphs | `default` / `express` / `standard` + `custom:{initiative_id}` |

## 2. Scope & non-goals

**In scope**

- CRUD missions, start/stop, HITL, pipeline update/confirm
- Bind agent catalog + toolkit + memory briefs at create
- Compile custom graph after confirm; refuse start if unconfirmed
- Room slash commands wired to lifecycle

**Out of scope**

- Replacing LangGraph

**Concurrency model**

- Run locks are per initiative (`_run_lock_for`): different missions run in parallel, while start/resume of the same mission stays mutually exclusive so two runs never write the same `thread_id` checkpoint
- `inline` mode runs multiple missions in parallel inside one process (`asyncio.create_task`), bounded only by executor resources
- Multi-process execution: `TDT_RUN_BACKEND=sqlite`, API replicas set `TDT_RUN_WORKER=false` and execution is consumed by `sortie worker` instances; **each worker runs one mission at a time, so concurrency = worker replicas** (a mission drives a whole agent chain, so stacking them in one process is not desirable)
- `_lease_heartbeat` renews the lease every `run_lease_seconds/3` so long missions are not judged stale and re-run
- SSE reads a durable event table (`data/events.sqlite`, `event_log.py`): the worker process writes, API processes tail by `seq`, so the split and reconnects never lose events
- SQLite is the only mission store (row-level upsert + WAL + busy_timeout), safe for multiple processes on one host; cross-host sharing still needs an external database

## 3. Code map

| Symbol / area | Responsibility |
|---------------|----------------|
| `Orchestrator.create_initiative` | planner proposal, meta, room seeds |
| `confirm_pipeline` / `update_pipeline` | specs normalize, compile custom graph |
| `start_pipeline` | gate on `pipeline_confirmed`, spawn `_run_until_interrupt` |
| `submit_hitl` / `stop` | resume `Command` |
| `_graph_for` / `_compile_custom_graph` | track vs custom |
| `InitiativeStore` | JSON persistence |

## 4. State machine

```text
draft ──confirm──► draft(confirmed)
  │ start
  ▼
running ◄──approve/reject/rewrite── waiting_hitl
  │
  ├── stop ► stopped
  ├── error ► failed
  └── complete ► done
```

## 5. API surface

| Method | Path | Notes |
|--------|------|-------|
| GET/POST | `/api/initiatives` | list / create |
| GET/DELETE | `/api/initiatives/{id}` | |
| POST | `.../start` | 400 if not confirmed |
| PATCH | `.../pipeline` | edit specs; optional confirm |
| POST | `.../pipeline/confirm` | lock + compile |
| POST | `.../hitl` | HitlSubmitRequest |
| POST | `.../stop` | |
| GET | `.../events` | SSE |

Create body highlights: `brief`, `coding_executor`, `role_agents`, `role_toolkits`, `pipeline_track`, `complexity`, `auto_confirm_pipeline` (CLI/tests).

## 6. Critical path

```mermaid
sequenceDiagram
  participant U as User
  participant O as Orchestrator
  participant P as plan_pipeline
  participant G as LangGraph

  U->>O: create_initiative
  O->>P: plan_pipeline
  P-->>O: proposal pending
  U->>O: confirm_pipeline
  O->>O: compile custom graph
  U->>O: start_pipeline
  O->>G: astream
  G-->>O: interrupt HITL
  U->>O: submit_hitl
  O->>G: Command resume
  G-->>O: done
```

## 7. Technical design

### Current risks

- Large `Orchestrator` class (god-object)
- In-process custom graph map; lost on restart until re-confirm/compile
- JSON store not multi-writer safe beyond coarse locks

### Target structure

```text
services/
  MissionService
  PipelineBindingService   # confirm/compile
  RunService               # astream + HITL
  RoomBridge
repositories/
  InitiativeRepository     # JSON → SQL
```

On process boot: rebuild custom graphs for draft/running confirmed missions from `pipeline_specs`.

## 8. Development plan

### P0 (3–5 days)

| Task | Acceptance |
|------|------------|
| Integration test: create → edit → confirm → start → HITL → done | pytest |
| Recompile custom graphs on `startup()` for confirmed ids | restart mid-HITL works |
| Structured error codes (`pipeline_not_confirmed`) | API JSON + UI |

### P1 (1–2 weeks)

| Task | Acceptance |
|------|------------|
| Split PipelineBinding + Run services | import graph cleaner |
| SQL initiative repository behind interface | feature flag |
| Idempotent start | second start no double task |

### P2 (2+ weeks)

| Task | Acceptance |
|------|------------|
| External run queue / lock | 2 workers safe |
| Outbox for webhooks + SSE | at-least-once |

## 9. Test plan

- Existing `tests/test_pipeline*.py`, `test_rooms.py` extended
- Fail start without confirm
- Confirm after manual stage drop still validates contracts at runtime

## 10. Dependencies

- Pipeline planner, Graph builder, Rooms, Auth (API), Executors
