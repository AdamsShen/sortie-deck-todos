# CLI

## 1. Overview

CLI is the **developer entry** without the browser: smoke a full mission loop and launch the API process.

| Item | Value |
|------|-------|
| Scripts | `tdt`, `tdt-api` (`pyproject.toml`) |
| Code | `src/sortie_deck/cli.py`, `api/app.py:run` |

## 2. Scope & non-goals

**In scope**

- `sortie smoke` — create/start mission with auto pipeline confirm, drive HITL
- `sortie api` / `tdt-api` — uvicorn entry
- `sortie worker` — run-queue consumer only, no HTTP (scales horizontally)
- `sortie doctor` — runtime checks (including the storage/queue combination)
- `sortie mission create / bind-repos / attach / start / forge` — assemble a mission (parity with the MCP tools)

**Out of scope**

- Full kubectl-like mission management (P1+)
- Interactive TUI (P2)

## 3. Current commands

```bash
sortie smoke [--brief "..."]   # auto_confirm_pipeline + HITL approvals
sortie api                     # binds TDT_HOST:TDT_PORT
# Multi-worker: API replicas set TDT_RUN_WORKER=false, execution runs as workers
TDT_RUN_BACKEND=sqlite TDT_STORAGE=sqlite sortie worker --id w1
```

Mission commands come in two shapes: **one-shot** (`mission start --brief`) and **assembly**
(`create` → `bind-repos` / `attach` → `start <id>`).

```bash
sortie mission create --brief "add an export button" --track standard
sortie mission bind-repos ini_xxx --repo eng=git@github.com:org/web.git#dev
sortie mission attach ini_xxx --file prd.md --kind prd --pinned
sortie mission start ini_xxx --wait

sortie mission forge list ini_xxx
sortie mission forge approve fgd_xxx
```

- `--repo SLOT=URL[@BRANCH]` is repeatable; slots are `product/eng/qa/deploy`; use `URL#branch` for branch names containing `/`
- `--file` accepts text only (upload binaries in the workbench); `--pinned` materials ride along in every stage prompt
- Repos can only be bound **before start**; afterwards the CLI returns `conflict` (exit 5)
- Remote and embedded modes behave the same; embedded `start` blocks until a gate because the process is the executor

Implementation sketch: instantiate `Orchestrator`, `create_and_start` / confirm path, poll HITL, `submit_hitl(APPROVE)` until done.

## 4. Technical design (target)

```text
tdt
  doctor      # env, python, node, ports, secret
  smoke       # existing
  api         # existing
  mission
    create | confirm | start | status
  version
```

JSON output flag `--json` for scripting.

## 5. Development plan

### P0 (1 day)

| Task | Acceptance |
|------|------------|
| Expand `--help` / subparser clarity | documented in README |
| Smoke prints track + stage rail | stdout readable |
| Exit non-zero on failure | CI usable |

### P1 (3–5 days)

| Task | Acceptance |
|------|------------|
| `sortie doctor` | checks venv, ports 8787/5173, data dir writable, token secret |
| `tdt mission status <id>` | prints status/stage |
| `--json` on smoke/status | machine parseable |

### P2 (1 week)

| Task | Acceptance |
|------|------------|
| Live progress via SSE client in CLI | mirrors workbench |
| Plugin list `tdt executors` | matches `/api/executors` |

## 6. Test plan

- `sortie smoke` against tmp `data_dir` in pytest (or subprocess)
- Doctor fails loudly when port occupied (optional)

## 7. Dependencies

- Orchestrator, Pipeline confirm semantics, mock executors
