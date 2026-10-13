# Knowledge

## 1. Overview

Enterprise knowledge uses Tencent OSS **[WeKnora](https://github.com/Tencent/WeKnora)**.  
Sortie Deck only **imports** and **catalogs**; **retrieval uses WeKnora** (hybrid-search) — no in-repo RAG engine.

Coding tools (Cursor) load a per-mission pack via the knowledge-for-cursor skill.

| Item | Value |
|------|-------|
| Code | `knowledge.py`, `weknora.py`, `knowledge_gateway.py`, `knowledge_pack.py` |
| Local catalog | `data/knowledge.json` (metadata mirror, not a search engine) |
| Backend | `TDT_KB_BACKEND=local\|weknora` |
| API | `/api/knowledge` · `/status` · `/folders` · `/import` · `/sync` · `/retrieve` |
| CLI | `tdt knowledge {status,sync,import,pack}` |
| Skill | `packages/skills/knowledge-for-cursor` |

## 2. Boundaries

| Sortie Deck | WeKnora |
|-------|---------|
| Import Markdown / URL / file | Parse, chunk, embed, index |
| Catalog + folder mirror + `sync` | Hybrid search |
| Mission pack → Cursor | FAQ / Wiki / Agent QA (optional direct) |

## 3. Config

```bash
TDT_KB_BACKEND=weknora
TDT_WEKNORA_BASE_URL=http://127.0.0.1:8080
TDT_WEKNORA_API_KEY=...
TDT_WEKNORA_KB_ID=<knowledge_base_id>
```

Without WeKnora, Sortie Deck falls back to local catalog keyword ranking (dev only).

## 4. Model

`KnowledgeDoc`: title / summary / tags / category / `folder_path` / `remote_id` / `backend`.  
Body may be empty (content lives in WeKnora).

## 5. API

| Method | Path | Behavior |
|--------|------|----------|
| GET | `/api/knowledge` | Local catalog |
| GET | `/api/knowledge/status` | Backend + WeKnora probe |
| GET | `/api/knowledge/folders` | Folder tree (WeKnora) |
| POST | `/api/knowledge/import` | Import → WeKnora + catalog |
| POST | `/api/knowledge/sync` | Refresh catalog from WeKnora |
| POST | `/api/knowledge/retrieve` | `{query,limit}` → hybrid-search |

## 6. Cursor pack

`load_pack_for_cursor(..., gateway=)` writes:

- `.cursor/rules/sortie-knowledge.mdc`
- `.sortie-deck/knowledge-pack.md`

`cursor_cli` preloads before coding.

## 7. Mission namespace (per-requirement isolation)

Each requirement owns a RAG namespace so its knowledge stays isolated from (and
ranked ahead of) the global library:

- **Writeback**: after a stage finishes, the orchestrator indexes key artifacts
  (`prd.md` / `tech_design.md` / `design.md` / `handoff.md` / `integrate.md`)
  via `KnowledgeGateway.index_mission_artifact(...)` — best-effort, idempotent
  per `(stage, kind)`, tracked in `Initiative.meta["kb_indexed"]`.
- **Local**: catalog entries tagged `ini:<initiative_id>`.
- **WeKnora**: manual doc moved to folder `<prefix>/<initiative_id>`
  (`TDT_KB_MISSION_FOLDER_PREFIX`, default `missions`); folder-move failures are
  cosmetic and ignored.
- **Retrieval**: `retrieve(query, initiative_id=...)` searches the mission
  namespace first (boosted), then fills from the global pool (WeKnora hybrid
  search or local keyword ranking). Harness prompts (`_rag_brief`) always pass
  the mission id.
- **Switch**: `TDT_KB_MISSION_INDEX=true` (default) enables writeback;
  `TDT_KB_MISSION_INDEX_MAX_CHARS` caps per-artifact body size.

Skills / MCP / tools are NOT mission-scoped — they come from the global toolkit
library and are loaded just-in-time per stage (see `toolkit.md`).
