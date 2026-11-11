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
- **No checkpointer**: `PlannerSessionStore` (`missions/<id>/chat/planner.*`)
  remains the single source of truth, so there is no double write.
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
| `claude_code` | `claude-agent-sdk` (`query()` + `resume`) | Permission mode from `TDT_CODING_PERMISSION_MODE`, task-level override wins (see 2.2); needs `ANTHROPIC_API_KEY` |
| `dsh` | official `deepseek-harness-sdk` (`DeepSeekHarness` + `resume_session`, stdio JSON-RPC with a bundled runtime) | credentials from `TDT_DSH_API_KEY` or `DEEPSEEK_API_KEY`; profile `sdk`; sandbox from `TDT_DSH_PERMISSION_MODE` (default `workspace-write`); a fresh runtime per turn, resumed by session id — see "2.1 DSH notes" |
| `opencode` | HTTP to a deployed `opencode serve` (`TDT_OPENCODE_BASE_URL`) | server-side, no local CLI |
| `codex` | `codex exec --json` inside the cloud container | session resume via `codex exec resume`; credentials from `OPENAI_API_KEY` / `CODEX_API_KEY` or `$CODEX_HOME/auth.json` (`codex login`); sandbox from `TDT_CODEX_SANDBOX` (default `workspace-write`); parses both `item.completed` and legacy `agent_message` events |
| `cursor` | `cursor-agent -p --output-format json` (cloud container) | `CURSOR_API_KEY` / `cursor-agent login` |
| `mock` | builtin echo | always-available fallback |

### 2.1 DSH (DeepSeek Harness) notes

- **Dependency**: `pip install -e ".[harness-dsh]"` (pinned to `deepseek-harness-sdk==0.2.1rc2`; the wheel bundles the matching runtime, ~95MB per platform — **no system Node required**).
- **Isolation**: each mission gets its own `DSH_HOME` (`<data_dir>/dsh_home/<initiative_id>`); `~/.dsh` is never read. The default patch disables session-log upload (`session-log-deepseek` — otherwise every turn ships session events to DeepSeek) and cuts host skill roots (`skill-filesystem.includeDefaultRoots: false`) so `~/.agents/skills` cannot leak into the task prompt.
- **Lifecycle**: the executor never calls `provider.close()` and `Session.run()` blocks without a cancel handle, so the provider spawns a fresh runtime per turn, closes it in `finally`, and resumes via `resume_session(native_id)` (verified cross-process; ~0.7s cold start). A per-turn watchdog closes the runtime on timeout.
- **Stage capabilities**: `disallowed_tools` become "disable the whole plugin row" patches (Bash→`tool-bash`, Grep/Glob→`tool-fs-search`, Task→`tool-subagent`, WebFetch/WebSearch→`tool-web`, TodoWrite→`tool-todo`); **`Read` cannot be disabled alone** (`tool-fs` owns read/write/edit) and is logged as a warning. Stage MCP servers are mounted by inserting `@deepseek-ai/dsh-mcp-client` patches — `stdio` and `streamable-http` only; `sse` is skipped with a warning.
- **Known gaps**: one writable root per session, so `extra_dirs` get no write access (reads are unrestricted; artifacts rely on the existing archive fallback); no max-turn knob (only request/stage timeouts); no token usage or cost in events.
- **Security**: the DSH sandbox limits **writes only** — reads are unrestricted (`~/.ssh`, `.env` are reachable), so cloud deployments must rely on container isolation and must not leave secrets on any path the agent can read.

Selection & locking:

- Chosen at mission creation (`CreateInitiativeRequest.harness`), default
  `TDT_DEFAULT_HARNESS=dsh`; validated against the registry
  (`UnknownHarnessError` otherwise).
- Locked when the pipeline starts (`Initiative.harness_locked=True`); draft
  missions can switch via `PATCH /api/initiatives/{id}/harness`.
- Mid-run switch requires HITL approval (`switch_harness` action) — the graph
  swaps the harness into the pipeline state and re-runs/re-enters the stage.

### 2.2 Permissions and environment blocks

- **Resolution**: `permissions.resolve_permission_mode()` applies "task-level override (`[sortie:coding_permission_mode=...]` in a HITL instruction, or `StageContext.permissions`) > `TDT_CODING_PERMISSION_MODE` > `acceptEdits`", accepting only SDK whitelisted values. The CLI executor and the SDK harness share this function; the result reaches `ClaudeAgentOptions` via `context_pack["permission_mode"]`.
- **Why it matters**: `acceptEdits` auto-approves file edits and the read-only command set only — `node --test`, `npm` and `curl` still need approval and are denied outright in unattended runs (official headless docs). Isolated containers can set `bypassPermissions` and use `TDT_PERMISSION_DENY_RULES` for scoped deny rules (deny rules still apply under bypass; empty by default).
- **Preflight**: with `TDT_PERMISSION_PREFLIGHT=true` (default) the worker runs one interpreter command on its first stage and only warns on failure; `/api/health` exposes the effective `permission_mode` so a missing env var is visible immediately.
- **Failure classification**: after a stage, `environment_blocked` / `auth_blocked` is derived from `selftest_log.json` (`status=blocked`), `test_report.json` (`verdict.not_a_code_defect` / `status=blocked`) and provider markers. Classification is **post-hoc** — no pre-check ever skips a stage.
- **Circuit breaker**: with `TDT_ENV_BLOCK_PAUSE_ENABLED=true` (default) an environment failure stops the run on the first occurrence instead of following `qa_passed=False` back to `eng_implement`; `TDT_MAX_CONSECUTIVE_SAME_FAILURE` (default 3) backstops any other repeated failure. A terminal `stopped` state is used rather than `interrupt` because not every stage has a HITL gate; re-launch the mission after fixing the environment.
- **Sandbox rollout (2026-11-10)**: `docker/claude-sandbox.json` ships in the `runtime-harness` image and is activated by `TDT_HARNESS_SETTINGS_FILE` (missing file is skipped). The worker container needs `cap_add: [SYS_ADMIN]` plus `security_opt: [seccomp=unconfined, apparmor=unconfined]`; on Ubuntu 24.04 (`kernel.apparmor_restrict_unprivileged_userns=1`) all three are required. Harness-neutral modes live in `context_pack["sandbox"]["mode"]` (`read_only|workspace_write|full_access`): `dsh` maps it to `DSH_PERMISSION_MODE`, `codex` to `--sandbox` with `TDT_CODEX_ASK_FOR_APPROVAL` / `TDT_CODEX_NETWORK_ACCESS`. Dependencies are constrained to the workspace; global installs are denied by the sandbox.

### 2.3 DSH sandbox auto-fallback (off by default)

Some runtimes (observed in the cloud linux wheel) ship without a sandbox runner: under
`workspace-write` bash is refused before the command starts (`SANDBOX_UNAVAILABLE` /
`no usable backend`), while escalating to `danger-full-access` fails because no approval
channel exists. `DSH_PERMISSION_MODE` is resolved at process startup and the SDK exposes no
hot-switch RPC, so the fallback is "probe → close runtime → relaunch that turn with
`danger-full-access`", not a mid-run variable change.

| Setting | Default | Meaning |
|---|---|---|
| `TDT_DSH_SANDBOX_AUTO_FALLBACK` | `false` | Master switch; when off there is no probe and behaviour is unchanged |
| `TDT_DSH_SANDBOX_PROBE_TTL_SECONDS` | `600` | Process-level cache TTL (key includes host / SDK version / initial mode / cwd); `0` = probe every turn |
| `TDT_DSH_SANDBOX_PROBE_TIMEOUT_SECONDS` | `30` | Probe timeout; on timeout or error the original mode is kept — never escalate on a failed probe |
| `TDT_DSH_SANDBOX_PROBE_PROMPT` | fixed `echo __dsh_probe__` | No task content, no MCP/tool patches, no task artifacts |
| `TDT_DSH_SANDBOX_UNAVAILABLE_HINTS` | built-in list | Comma-separated custom markers matched against `tool/result` and reply text |

Explicit task-level `full_access` skips probing. The probe uses its own
`dsh_home/_probe/<uuid>` and session id and is deleted afterwards, so the task's
`native_id` is untouched. On a hit, that turn runs with `danger-full-access` and
`reply.meta` carries `permission_fallback` / `probe_reason` / `effective_mode` /
`probe_cached`; a `WARNING` records initiative, stage, session and reason (never secrets).

**Semantics**: `approval.policy='never'` means "reject without asking", not "auto-allow".
Under `workspace-write`, bash inside the workspace already needs no approval; only
out-of-root escalation does, and it fails closed in unattended runs. This feature does
**not** add "approval-free commands inside the sandbox" — it only falls back to an
unconfined mode when the sandbox backend is unusable.

**Security**: until the outer per-run launcher (mount namespace / env allowlist / egress
proxy) lands, enabling this on a public multi-tenant container sharing `/data` widens the
permission surface. It is temporary mitigation, not a safe default; see
`docs/plans/2026-11-10-dsh-sandbox-auto-fallback.md` §3. Turn the switch off to roll back.

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

Failed runs and gate semantics:

- **Degraded self-check**: when a stage ends with `status=fail` and self-review produced no verdict
  (all lenses `skipped` / `failed`), the graph re-enters the stage with a self-check instruction,
  bounded by `TDT_STAGE_SELF_CHECK_ATTEMPTS` (default 1, 0 disables). The instruction only asks for
  completing the contract artifacts, aligning `diff.patch`, running the tests and ticking the
  checklist — no scope expansion. Deploy stages are never re-run. This keeps half-finished output
  from flowing into QA and burning a full review round.
- **Recommended gate action**: with `qa_passed=false`, `pending_hitl.recommended_action` is
  `{action: "reroute", next_stage: on_fail, label: "Rework"}`, so the workbench renders "Rework"
  as the primary button and shows the reason; "Approve anyway" (still bounces) and "Stop" remain.

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
