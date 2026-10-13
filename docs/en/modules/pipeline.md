# Pipeline Planner

## 1. Overview

Internal agent **pipeline_planner** proposes a stage list from the mission brief; humans confirm or edit before launch. This is the **mission configuration entry** after create.

| Item | Value |
|------|-------|
| Code | `pipeline.py` |
| Templates | `packages/templates/{express,standard,default}_pipeline.yaml` |
| Agent | `pipeline_planner` / 航线规划官 |

## 2. Scope & non-goals

**In scope**

- Heuristic track selection + adaptive add/drop stages
- Stage catalog union; normalize intake…done
- Materialize LangGraph-ready template dict
- APIs: list tracks, suggest, catalog; mission patch/confirm

**Out of scope**

- Autonomous start without confirm
- Arbitrary user-defined executors without registry (P2 plugins)

## 3. Tracks

| Track | Use | Typical nodes |
|-------|-----|---------------|
| express | hotfix / one-liner | intake → eng → qa → deploy → done (adds design / platform stages when brief asks) |
| standard | normal feature | PRD → **tech design** → **design** → cases → build → selftest → integrate → handoff → qa → deploy → done |
| default | office iteration | PRD → **tech design** → **design** → build → qa → deploy → done |

### 3.1 Tech design stage (`eng_tech_design`)

Runs after `product_prd` and before any build stage (standard/default only —
the hotfix express track skips it). The eng agent reads the PRD/acceptance
criteria and produces a reviewable `tech_design.md` (approach, module
breakdown, risks, rollout/rollback) that is validated by the stage contract and
reviewed at its `hitl_after` gate. Build stages then implement against the
approved design. Under a mission harness this stage runs through the same
`harness_stage` executor as every other stage; mock fallback is
`mock_tech_design`.

## 3.1 Role slots

| Slot | Role |
|------|------|
| `product` | Product / PRD |
| `design` | Design (UI / UX) |
| `eng` | General engineering (express / unspecified) |
| `eng_ios` / `eng_android` / `eng_web` / `eng_backend` / `eng_agent` | Specialist build |
| `qa` | QA |
| `deploy` | Ship |

When the brief mentions iOS / Android / Web / backend / Agent, the planner replaces generic `eng_implement` with matching `eng_*` stages.

## 4. Proposal schema

```json
{
  "agent": "pipeline_planner",
  "track_hint": "express",
  "rationale": "...",
  "status": "pending|confirmed|edited",
  "stages": [
    {"id": "eng_implement", "label": "...", "role": "eng", "hitl_after": false, "on_fail": null, "on_reject": null, "output_contract": []}
  ]
}
```

Stored on initiative `meta`: `pipeline_proposal`, `pipeline_specs`, `pipeline_rail`, `pipeline_confirmed`.

## 5. API

| Method | Path | Behavior |
|--------|------|----------|
| GET | `/api/pipelines` | tracks + catalog |
| GET | `/api/pipelines/suggest` | run `plan_pipeline` preview |
| GET | `/api/pipelines/catalog` | stage catalog |
| PATCH | `/api/initiatives/{id}/pipeline` | normalize + optional confirm |
| POST | `/api/initiatives/{id}/pipeline/confirm` | confirm current/override stages |

## 6. Algorithm (current)

1. Resolve track from explicit hint / complexity / `suggest_track` regex heuristics.
2. Load YAML stages for track.
3. Adaptive mutations (e.g. express + “用例” → insert `qa_cases`; standard + skip-PRD phrase → drop `product_prd`).
4. Fix `on_fail` / `on_reject` targets to existing ids.
5. Return pending proposal.

## 7. Technical design (target)

```text
PlannerBackend
  HeuristicPlanner   # current
  LlmPlanner         # same Proposal schema
HumanOverride always wins on confirm
PolicyPacks: regulated | hotfix | frontend_only
```

UI: diff proposal vs edited specs before confirm.

## 8. Development plan

### P0 (2–3 days)

| Task | Acceptance |
|------|------------|
| Golden briefs → expected track + key stages | pytest table |
| Catalog completeness vs YAML | CI assert |
| Diff helper `diff_specs(a,b)` | unit tested |

### P1 (1 week)

| Task | Acceptance |
|------|------------|
| `LlmPlanner` adapter behind env flag | falls back to heuristic on failure |
| Proposal confidence + notes in meta | UI shows notes |
| Policy pack YAML | loadable pack id |

### P2 (2 weeks)

| Task | Acceptance |
|------|------------|
| Custom stage plugins registered into catalog | dynamic id |
| Org default track policy | admin setting |

## 9. Test plan

- `tests/test_pipeline_tracks.py` expand matrix
- Property: normalize always starts intake ends done
- Confirm path compiles graph without missing HITL edges

## 10. Dependencies

- Graph `build_graph_from_template`, Orchestrator confirm, Web editor, Contracts for new stages
