# Memory

## 1. Overview

Scoped memories injected as context blocks when assembling a mission.

| Item | Value |
|------|-------|
| Code | `memory.py` |
| Store | `data/memory.json` |
| API | `/api/memory` |

## 2. Scopes & kinds

| Scope | scope_id | Typical use |
|-------|----------|-------------|
| workspace | fixed/global | team norms |
| user | user id | personal prefs |
| mission | initiative id | episode facts |
| agent | agent id | specialist notes |

Kinds: `fact` | `preference` | `episode` | `lesson`.

`context_block(...)` concatenates recent matching memories for create_initiative.

## 2.1 Shared backends

`TDT_MEMORY_BACKEND` selects the shared layer: `local` (default) or `mem0`.
With `mem0` the local store is wrapped in a `CompositeMemory` that mirrors
mission-scope writes to mem0 (`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY` /
`TDT_MEM0_USER_PREFIX`) and merges mem0 search hits into context blocks,
isolated per initiative via `user_id = "<prefix>:<initiative_id>"`. Remote
failures degrade silently to the local store. Mission-bound files (rolling
summary, session transcripts, context files) live in `mission_memory.py` —
see `harness.md`.

## 3. Technical design (target)

- Ranking by recency × importance
- Auto-write `lesson` on initiative `done`
- Encrypt `user` scope at rest
- Retention TTL policies

## 4. Development plan

### P0 (1–2 days)

| Task | Acceptance |
|------|------------|
| UI copy clarifies scopes | fewer wrong scope picks |
| Unit tests for context_block filtering | pytest |
| Cap token length of brief | hard max chars |

### P1 (1 week)

| Task | Acceptance |
|------|------------|
| On DONE write lesson stub | memory row created |
| Importance field | sort uses it |

### P2 (1–2 weeks)

| Task | Acceptance |
|------|------------|
| User-scope encryption | key from env |
| TTL sweeper job | expired gone |

## 5. Dependencies

- Orchestrator create, Web memory page, Auth user id
