# Collaboration modules (contract · roles · forge · manager · sub-rooms · loadout · decisions)

An implementation index for the [multi-agent collaboration model](../design/multi-agent-collaboration.md):
which modules were added, which fields were extended, and which endpoints exist.

## New modules

| Module | Responsibility |
|--------|----------------|
| `sortie_deck/agent_contract.py` | `AgentContract` (inputs / outputs / handoff_targets / timeout / on_fail / tools_allowed), persona front-matter parsing, `validate_handoff`. Pure data layer, no orchestration imports |
| `sortie_deck/roles.py` | `RoleRegistry`: built-in + persona + custom roles (`data/roles.json`) with normalisation, semantic de-duplication and lookup |
| `sortie_deck/agent_forge.py` | Generate a role (persona + contract) on demand, self-check it, store it as `pending-review`, publish after human review |
| `sortie_deck/manager.py` | The narrow manager: join arbitration, conflict/escalation, and the plan digest (`plan_digest`) |
| `sortie_deck/subroom.py` | Sub-room sessions: an independent manager instance, budgets, depth cap 1, stage-scoped conclusion memory |
| `sortie_deck/loadout.py` | Just-in-time capability loading: recall → select → probe → load or degrade, with an auditable manifest |
| `sortie_deck/skill_runtime.py` | Skill script execution: language whitelist, timeout, sandbox dir, write confirmation |
| `sortie_deck/decisions.py` | Human decision centre: `decision_id` idempotency, decider authorisation, SLA degradation |
| `sortie_deck/notify.py` | Multi-channel outbound (DingTalk / Feishu / generic webhook) with redaction levels |
| `sortie_deck/guardrails.py` | Mission budget, waiting-graph deadlock detection, `trace_id` causal chain |

## Where contracts live

Contracts are front-matter on the persona files. `load_persona` returns only the
body, so the YAML never leaks into prompts:

```markdown
---
role_key: qa
title: QA Engineer
outputs: [test_plan.md, test_cases.md, test_report.json]
handoff_targets: [eng_web, eng_ios, eng_android, eng_backend, eng, deploy]
timeout_seconds: 900
on_fail: escalate
---
You are a QA engineer …
```

An empty `handoff_targets` means **no autonomous hand-off** — the compiled edge
always decides.

## Extended domain fields

| Model | Addition |
|-------|----------|
| `RoleId` | `_missing_` lets unregistered role keys become first-class members (`parenting`, `stocks`, …) while `.value` and `== RoleId.X` keep working |
| `StageContext` | `memory_brief` (layered memory + sub-room conclusions), `handoff_targets` |
| `RoomMessage` | `room_key` (`main` is the big room), `msg_type` gains `subroom` |
| `TimelineEvent` | `kind` gains `handoff`, `subroom` |
| `PipelineState` | `plan_digest`, `handoff_target`, `handoff_counts`, `handoff_total`, `wave_attempts`, `halt` |
| `ToolkitItem` | `capabilities`, `executable`, `read_only` (recall + degradation input) |
| `Initiative.meta` | `plan_digest`, `loadout_plans`, `guardrails`, `pending_decision_id`, `forged_agents` |

## Routing notes

- **One routing mechanism only**: there are **no static edges between stages**.
  Every node writes its next hop into `handoff_target` and a conditional edge
  consumes it; the frozen plan is that edge's default branch. Agent hand-offs,
  manager join decisions and HITL verdicts all travel through this one router.
- **Conditional edges instead of `Command(goto=)`**: with the LangGraph version
  pinned here, a node returning `Command(goto=…)` **still follows its static
  outgoing edges**, so both destinations run in one superstep and collide on the
  shared state channels. Unifying on conditional edges removes the problem at the
  root, and the graph no longer needs `Command` at all.
- **A HITL verdict is a node jump too**: the gate writes `handoff_target`
  (approve → plan's next node, reject → `on_reject`, reroute → the chosen node,
  stop → `stopped`); its default branch is the plan's approve target.
- **HITL first**: a `hitl_after` node never takes part in autonomous hand-off;
  its default branch points at `hitl__<stage>`.
- **Target validation**: the target must be a plan node; 3 hand-offs per target,
  40 per mission; `done` can never be jumped to.
- **Not switchable**: routing has no flag (`TDT_AGENT_HANDOFF` /
  `TDT_MANAGER_JOIN_DECISION` were removed) — it *is* the collaboration model,
  not an option.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/roles` | Role registry (built-in + persona + custom) |
| POST | `/api/roles` | Register a custom role |
| DELETE | `/api/roles/{role_key}` | Delete a custom role |
| GET | `/api/forge/drafts` | Forge drafts (all statuses; `?status=pending-review` to filter) |
| POST | `/api/forge/drafts` | Generate a draft from a capability description |
| PATCH | `/api/forge/drafts/{id}` | Human edit (persona / contract / tags) |
| POST | `/api/forge/drafts/{id}/approve` | Approve: register the role, optionally publish the agent |
| POST | `/api/forge/drafts/{id}/reject` | Reject |
| GET | `/api/subrooms` | Sub-room records (filter by `initiative_id`) |
| GET | `/api/subrooms/{id}/transcript` | Sub-room transcript (never merged into the big room) |
| GET | `/api/decisions` | Human decisions (filter by `initiative_id` / `status`) |
| POST | `/api/decisions/{decision_id}/answer` | Answer a decision (idempotent + authorised) |
| POST | `/api/decisions/sweep` | Expire overdue decisions and apply their fallback |
| POST | `/api/bridges/dingtalk` | DingTalk bot inbound callback |

## Workbench

Three additive views, with no change to the existing page structure:

- **Role Forge**: the draft queue with self-check findings and possible
  duplicates; edit the persona/contract and publish.
- **Sub-rooms**: read-only timeline plus conclusion card and budget usage.
- **Decisions**: pending items, SLA countdown, fallback, and a reply box.

## Tests

| File | Coverage |
|------|----------|
| `tests/test_agent_contract.py` | Contract parsing, denied hand-offs, every shipped persona's contract self-check |
| `tests/test_roles_forge.py` | Role registration/dedupe/forge review flow, loadout whitelist and collaboration assessment |
| `tests/test_handoff_join.py` | Conditional hand-off, denied target fallback, HITL is never bypassed, ping-pong budget, join decisions, plan digest |
| `tests/test_subroom.py` | Opening a sub-room, independent manager, depth cap, budgets, conclusion scoping, transcript isolation |
| `tests/test_executor_session.py` | `run_session` delegation, sub-room pause/resume, in-graph end-to-end |
| `tests/test_loadout.py` | Recall-select-probe-degrade, skill execution and timeout, MCP whitelist and write confirmation |
| `tests/test_decisions.py` | Redaction, DingTalk signing, timeout degradation, duplicate-answer idempotency, unauthorised rejection |
| `tests/test_im_escalation.py` | DingTalk inbound parsing, escalate-pause-reply loop, timeout sweep |
| `tests/test_guardrails.py` | Budget dimensions, deadlock detection, causal chain, sub-room budget refusal |
| `tests/test_research_track.py` | The non-engineering sample track (research → conclusion → recommendation) |
