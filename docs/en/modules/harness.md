# Agent Harness

## 1. Overview

Two related but distinct layers:

1. **In-process harness loop** — shared agent loop for Sortie Deck flows
   (tools, turn limits, confirm gates, waterfall traces), inspired by AWS
   **Strands / AgentCore harness** ideas, without a vendor SDK dependency.
2. **External harness providers** *(new)* — the system-wide capability that
   embeds an external agent harness (Claude Code / OpenCode / Codex / Cursor)
   and runs **every stage conversation** of a mission inside it.

| Item | Value |
|------|-------|
| In-process loop | `harness/__init__.py` (first consumer: `mission_briefing.py`, `/api/briefing/sessions`) |
| Providers | `harness_providers/` (`base.py` registry + one module per vendor) |
| Stage executor | `harness_providers/executor.py::HarnessStageExecutor` (`harness_stage`) |
| Mission memory | `mission_memory.py` → `data/missions/<id>/memory/` |
| Shared memory | `memory.py` (`MemoryStore` + optional mem0) |
| API | `GET /api/harnesses`, `PATCH /api/initiatives/{id}/harness`, HITL `switch_harness` |
| Waterfall graph | `harness/waterfall.py` (five-node deterministic `StateGraph`) |
| LLM transport | `llm.py` façade + `llm_chain.py` (LangChain `ChatOpenAI`, the only implementation) |

### 1.1 Briefing waterfall graph (LangGraph)

The five-step briefing waterfall now lives in `harness/` instead of the HTTP
layer, driven by a LangGraph graph in `harness/waterfall.py`:

```text
START → search_history → retrieve_knowledge → propose_pipeline
      → propose_loadout → ensure_missing_roles → END
```

- **Fixed edges, not `create_react_agent`**: the workbench renders the waterfall
  animation from a fixed `data.step` sequence (`history` / `knowledge` /
  `pipeline` / `loadout` / `roles`), and `ensure_missing_roles` has a
  create-agents side effect that must not be model-driven.
- **Emit before calling the tool**, keeping event text and order identical to
  the previous implementation.
- **A failing tool does not break the chain**: `call_tool` folds exceptions into
  `ok=False`, the value is simply not written to `context`.
- **No checkpointer**: `BriefingStore` remains the single source of truth, so
  there is no double write.
- `harness/__init__.py` contracts (`HarnessEvent` / `ToolSpec` / `Harness`) are
  unchanged — the workbench, storage format and `harness_providers/` are untouched.

## 2. Providers

```text
HarnessProvider(Protocol)
  probe() → ProbeStatus           # availability / credentials, TTL-cached
  start_session() → HarnessSession  # multi-session missions
  send(session, prompt, cwd, context_pack) → HarnessReply
  close(session)
```

| Name | Transport | Notes |
|------|-----------|-------|
| `claude_code` | `claude-agent-sdk` (`query()` + `resume`) | Permission mode from `TDT_CODING_PERMISSION_MODE`; needs `ANTHROPIC_API_KEY` |
| `opencode` | HTTP to a deployed `opencode serve` (`TDT_OPENCODE_BASE_URL`) | server-side, no local CLI |
| `codex` | `codex exec --json` inside the cloud container | session resume via `codex exec resume`; credentials from `OPENAI_API_KEY` / `CODEX_API_KEY` or `$CODEX_HOME/auth.json` (`codex login`); sandbox from `TDT_CODEX_SANDBOX` (default `workspace-write`); parses both `item.completed` and legacy `agent_message` events |
| `cursor` | `cursor-agent -p --output-format json` (cloud container) | `CURSOR_API_KEY` / `cursor-agent login` |
| `mock` | builtin echo | always-available fallback |

Selection & locking:

- Chosen at mission creation (`CreateInitiativeRequest.harness`), default
  `TDT_DEFAULT_HARNESS=claude_code`; validated against the registry
  (`UnknownHarnessError` otherwise).
- Locked when the pipeline starts (`Initiative.harness_locked=True`); draft
  missions can switch via `PATCH /api/initiatives/{id}/harness`.
- Mid-run switch requires HITL approval (`switch_harness` action) — the graph
  swaps the harness into the pipeline state and re-runs/re-enters the stage.

## 3. Stage routing

`graph._executor_name()` returns `harness_stage` for **every** stage while the
mission state carries a non-empty `harness`. `HarnessStageExecutor` then:

1. resolves + probes the provider (TTL cache);
2. bootstraps the mission context pack (rolling memory + bound files + RAG brief);
3. sends the turn on a per-stage session (workspace = mission worktree);
4. records the turn into mission memory and auto-summarises over the token budget;
5. degrades to the legacy stage executors (mock / coding CLIs) when the provider
   is unavailable and `TDT_HARNESS_FALLBACK=true`;
6. always emits the stage-contract artifacts (`contracts.REQUIRED`).

## 3.1 Control-plane intents

Cross-agent routing and escalation must be executed by the orchestration layer, so the
executor advertises a declaration protocol and the model only states intent:

| Block | Fulfilled by |
|-------|--------------|
| ` ```control {"handoff": {...}} ` | `ctx.next_targets` lookup → `proposed_next` → conditional edge (contract whitelist / budget / HITL still enforced) |
| ` ```control {"escalate": {...}} ` | Manager advice first, then the human decision centre; the mission pauses until answered |
| ` ```subroom {...} ` | `StageSessionHost.open_subroom`, conclusion folded into a second turn (only `run_session`) |
| ` ```toolkit_intent {...} ` | JIT recall / float / skill execution by the orchestration layer |
| ` ```qa {"passed": bool} ` | `StageResult.qa_passed`; only `false` bounces to `on_fail`, undeclared follows the human gate |

Acceptance: `scripts/verify_collab.py` (scripted mock regression + real harness tracks).

## 4. Mission-bound memory

Layout under `data/missions/<initiative_id>/memory/`:

```text
memory.md                 rolling summary (auto-compacted, LLM-written)
sessions/<session>.jsonl  one transcript per harness session
context/*.md              requirement-bound files (brief.md / plan.md / harness.md, ...)
```

New sessions load everything via `bootstrap_context()`; the pack is trimmed to
`TDT_MISSION_CONTEXT_TOKEN_LIMIT` and, when over budget, the rolling summary is
LLM-summarised (`LlmClient.chat_text`) and written back.

## 5. Shared data layer

`TDT_MEMORY_BACKEND=mem0` wraps the local store in a `CompositeMemory` that
mirrors mission-scope writes to mem0 (`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY`)
and merges mem0 search hits into context blocks — initiative-scoped via
`user_id = "<prefix>:<initiative_id>"`. RAG results
(`knowledge_gateway.retrieve`) are injected into harness prompts. All remote
calls degrade silently to the local store.
