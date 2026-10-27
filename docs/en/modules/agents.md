# Agent Forge

## 1. Overview

Publish specialist agents (persona, slot, visibility, default `toolkit_ids`) for mission loadout pickers.

| Item | Value |
|------|-------|
| Code | `agents.py` |
| Store | `data/agents.json` |
| API | `/api/agents` CRUD |

## 2. Model

```text
PublishedAgent
  id, slug, title,
  slot: product|design|eng|eng_ios|eng_android|eng_web|eng_backend|eng_agent|qa|deploy
  summary, persona, persona_spec?, tags, visibility: public|private
  toolkit_ids[], author_*, version, published
```

`persona` is the compiled system prompt and the **only runtime source**; `persona_spec` is
the workshop's structured view, shaped as six sections: **role / background / skills /
workflow / examples / constraints**. Saving a spec re-compiles `persona` via
`persona.py::render_persona`. Legacy records with only `persona` get a read-only spec
parsed on read (`parse_persona`), so the structured editor always has content.

`resolve_slot_map({slot: agent_id})` used when creating initiatives.

## 3. Seed agents (dev)

Fresh `data/agents.json` seeds public specialists:

| Slug | Slot | Role |
|------|------|------|
| `prd-scout` | product | Field PRD scout |
| `design-navigator` | design | UI / UX specs |
| `platform-eng-raider` | eng | General engineering |
| `ios-raider` | eng_ios | iOS |
| `android-raider` | eng_android | Android |
| `web-raider` | eng_web | Web frontend |
| `backend-raider` | eng_backend | Backend / API |
| `agent-raider` | eng_agent | Agent / MCP / eval |
| `qa-gatekeeper` | qa | QA gate |

## 4. Flows

1. Expert publishes agent in forge UI (with default tools).
2. Mission create picks agent per slot → copies toolkit defaults unless overridden.
3. Runtime persona comes from published agent text.

## 5. Technical design (target)

- Semver + pin `catalog_agent_id@v` on mission roles
- Eval harness: golden briefs → rubric scores for persona
- Org catalog sync / import-export pack

## 6. Development plan

### P0 (1–2 days)

| Task | Acceptance |
|------|------------|
| API returns toolkit_ids; UI chips | visible |
| Author-only edit enforcement tests | pytest |
| Seed agents documented | README |

### P1 (1 week)

| Task | Acceptance |
|------|------------|
| version bump on patch; pin on create | meta stores pin |
| Clone agent action | new slug |

### P2 (2 weeks)

| Task | Acceptance |
|------|------------|
| Eval job CLI | score report |
| Shared org registry | remote pull |

## 7. Dependencies

- Toolkit ids, Orchestrator role loadout, Web forge
