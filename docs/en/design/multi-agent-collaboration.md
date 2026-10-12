# Multi-Agent Collaboration Model

This page describes who decides the next step, who merely records, and when a
mission must stop and ask a human.

## Division of labour in one table

| Layer | Owner | Responsibility |
|-------|-------|----------------|
| Graph | **The manager's control plane** | Carries the mission plan, steps and progress; it is how the manager governs and visualises, **not** the sole routing authority. Its structure is fixed at compile time |
| Big room · execution | Role agents | Serial steps **hand off autonomously**; during execution an agent may **open a sub-room at any time** |
| Manager | A decider separate from the agents | Decides only for 【parallel join】【conflict / undecidable】【human escalation】; marks steps complete afterwards |
| Sub-room | Initiating agent + participants + **a brand-new manager object** | Its own goal, plan and budget; the transcript never flows back; **the conclusion returns only to the agent that opened it** |

## Routing rules (two explicit paths, not an ambiguity)

1. **Serial**: the finishing agent hands off on its own. The manager does **not**
   choose the next step — it only marks the step complete.
2. **Parallel join**: several agents finished at once and none may arbitrate for
   the others — the **manager** decides which agent runs next.
3. **Undecidable ladder**: the agent solves it → escalate to the manager → if the
   manager cannot decide either, escalate to a human (DingTalk / Feishu), pause,
   and resume when they reply.

### Hard invariants

- **Hand-off whitelist**: an agent may choose where to hand off, but the target
  must be both a *plan-declared node* and inside the source role's contract
  `handoff_targets`. Anything else is denied and falls back to the compiled edge.
- **HITL cannot be bypassed**: a node with `hitl_after` may never be skipped by an
  agent — a human must sit between the agent and the next node.
- **Bounded budgets**: per-target handoffs, mission-wide handoffs, sub-room count
  and tokens all have caps; exceeding one falls back to the default edge instead
  of looping forever.
- **A fixed manager trigger table**: only 【join】【conflict / undecidable】
  【escalate】 reach the decision path; every other route costs zero LLM calls.

## Sub-rooms

- Opened **autonomously by an executing agent at any time** (agent-scoped, not
  pre-wired to a stage node), seeded with the question and the context it holds.
- A sub-room gets **a new manager object**: an independent instance with its own
  goal and plan.
- **Depth is capped at 1**: a sub-room cannot open another sub-room.
- The transcript **never flows back** to the big room. Only the conclusion is
  stored as `mission` scope / `kind=episode` memory tagged `stage:<name>`, and it
  is injected into **the stage that asked for it** so parallel branches stay clean.

## Interrupt / resume consistency

- Every start freezes a **plan digest** (a stable hash of nodes, roles and
  parallel grouping) into `ini.meta.plan_digest`.
- On resume the digest is verified: **if the plan changed, the run is refused**
  and must be re-confirmed instead of resuming against a graph it never ran on.

## Escalation ladder

```
the agent solves it
   └─ cannot → the manager decides
         └─ cannot → a human (DingTalk / Feishu / workbench)
                       └─ no reply within the SLA → fallback (default: pause)
```

Reliability guardrails around human decisions: `decision_id` idempotency (a
duplicate reply never resumes twice), decider authorisation (an unrelated member
replying is just chat), SLA timeout degradation (default `pause`), and redacted
outbound content.

## Switches

**Agent switching has no switch.** It *is* a graph node jump and the only
routing mechanism — the old logic is gone. There are no static edges between
stages: every node announces its next hop and a conditional edge consumes it, with
the frozen plan as the router's default. Serial steps follow the agent's proposal
(contract-whitelist validated), joins follow the manager, and a human verdict is
the same kind of jump.

Everything else defaults to **on**. Each capability degrades safely when its
dependency is missing (no LLM → rule fallback; no IM channel configured → the
question still lands in the decision centre instead of being pushed), so the
defaults cannot stall a mission. Set a switch to `false` when you do not want it:

| Switch | Default | Effect |
|--------|---------|--------|
| `TDT_PLAN_DIGEST_CHECK` | `true` | Verify the plan is unchanged before resuming |
| `TDT_SUBROOM_ENABLED` | `true` | Let an executing agent open sub-rooms |
| `TDT_LOADOUT_AUTO` | `true` | Recall and load capabilities (MCP / Skill) just in time |
| `TDT_MCP_CALL_ENABLED` | `true` | Let MCP tools actually be called (whitelist + write confirmation) |
| `TDT_SKILL_RUN_ENABLED` | `true` | Let skill scripts actually run (whitelist + write confirmation) |
| `TDT_IM_ESCALATION` | `true` | Let questions escalate to a human over IM |

Implementation details: [Collaboration modules](../modules/collaboration.md).
