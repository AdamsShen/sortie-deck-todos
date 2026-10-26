# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Agent harness capability (system-wide)**: pluggable harness providers — `claude_code` (claude-agent-sdk), `opencode` (serve + HTTP), `codex` (headless JSON protocol), `cursor` (cursor-agent headless), `mock` (fallback) — all running cloud-side via SDK/protocol, never the user's local machine (`harness_providers/`). Selected per mission at creation, locked on `/start` (`Initiative.harness` / `harness_locked`, `TDT_DEFAULT_HARNESS=claude_code`); every stage conversation routes through the generic `HarnessStageExecutor` (legacy mock/coding executors remain as degraded fallback, `TDT_HARNESS_FALLBACK`); mid-run switch requires the HITL `switch_harness` action. Probe API `GET /api/harnesses` (TTL-cached), draft-time `PATCH /api/initiatives/{id}/harness`
- **Mission-bound memory**: `mission_memory.py` — per-initiative files under `data/missions/<id>/memory/` (`memory.md` rolling summary, `sessions/*.jsonl` multi-session transcripts, `context/*.md` bound files like brief/plan/harness). New sessions auto-load memory + bound files (`bootstrap_context`), token budget `TDT_MISSION_CONTEXT_TOKEN_LIMIT` triggers LLM auto-summary written back to `memory.md`
- **mem0 shared-memory backend**: `TDT_MEMORY_BACKEND=mem0` (`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY` / `TDT_MEM0_USER_PREFIX`) runs alongside the local `MemoryStore` (`CompositeMemory`), initiative-scoped isolation, hits merged into context blocks; RAG (`knowledge_gateway`) results are injected into harness context packs
- Workbench: harness selector with probe status on draft missions, locked-harness badge, HITL "switch harness" approval action
- **Tech design stage**: `eng_tech_design` (standard/default tracks, after PRD, before build) produces a reviewable `tech_design.md` (approach / module breakdown / risks / rollout-rollback), validated by the stage contract and gated by HITL; mock fallback `mock_tech_design`; harness missions route it through the same `harness_stage` executor
- **Mission-scoped RAG namespace**: stage key artifacts auto-indexed per initiative (`KnowledgeGateway.index_mission_artifact`, tags `ini:<id>`, WeKnora folder `missions/<id>`, idempotent + best-effort); `retrieve(query, initiative_id=...)` ranks the mission namespace first and fills from the global pool; harness context packs retrieve with mission scope (`TDT_KB_MISSION_INDEX` switch)
- **Mission-page sub-rooms**: the task view embeds its mission's sub-rooms — turn-by-turn live transcript, read-only member roster (opener / lead / participants, no editing) and a fullscreen conversation view; `SubRoomService.on_event` (wired through the orchestrator) pushes `subroom` / `subroom_message` onto the initiative SSE stream, so the panel needs no polling

- Apache-2.0 license and open-source project docs (EN/ZH README, MkDocs Material i18n site)
- Architecture overview, data-flow diagrams, and per-module technical + implementation plans
- Expanded per-module specs (entry → core → platform) with API contracts, sequences, P0–P2 task tables and acceptance criteria
- GitHub CI (`ruff` / `pytest` / workbench `tsc` / `mkdocs`), Issue & PR templates
- `TDT_TOKEN_SECRET` / `TDT_ENV` auth hardening, artifact path traversal guards
- Workbench extracts: `api` client, `OperatorChip`, `PipelineEditor`
- Toolkit examples under `packages/examples/`; executor soft-fail when CLI missing
- P1: `TDT_STORAGE=sqlite` initiative repository, optional LLM planner / room LLM / MCP runtime flags
- P2: sqlite run queue + leases, S3 artifact backend, org policy, Slack/Feishu bridges, `sortie_deck.executors` entry points
- **Multi-agent collaboration**: `AgentContract` + persona front-matter contracts (`agent_contract.py`), open `RoleRegistry` (`roles.py`), LLM agent forge with human review (`agent_forge.py`), narrow-scope manager (`manager.py`), temporary sub-rooms (`subroom.py`), just-in-time capability loading (`loadout.py`), skill script execution (`skill_runtime.py`), human decision centre (`decisions.py`), multi-channel notify (`notify.py`), mission guardrails (`guardrails.py`)
- **Agent-driven hand-off**: a finishing agent may hand off to a plan node inside its contract whitelist (conditional edges; HITL gates and the frozen plan are never bypassed)
- **Harness control-plane intents**: real harnesses declare a `control` fenced block with `handoff` (role → plan stage via `StageContext.next_targets`, still contract / budget / HITL-checked) or `escalate` (manager answers first, then the human decision centre); the question is asked before the stage runs so LangGraph replay never drops the answer, and escalated replies never grant permissions or trigger publish
- **Manager join decision**: parallel waves arbitrate through the manager when branches disagree
- **Plan freezing**: `plan_digest` is written on start and verified on resume; a changed plan refuses to resume
- **Human escalation**: DingTalk bot + Feishu/webhook channels, `decision_id` idempotency, decider authorisation, SLA timeout fallback, redacted outbound content
- **Toolkit metadata**: `capabilities` / `executable` / `read_only` on toolkit items for loadout recall and degradation
- **Research track**: `research_pipeline.yaml` (采集 → 结论 → 建议) proves non-engineering requests use the same machinery
- **Workbench views**: Role Forge review, Sub-rooms panel, Decision centre
- **API**: `/api/roles`, `/api/forge/drafts*`, `/api/subrooms*`, `/api/decisions*`, `/api/bridges/dingtalk`
- **Mission code repos**: `CreateInitiativeRequest.role_repos` (slot → list of `RepoConfig{url, branch, name, token}`) — dev agents clone & read the configured git/github repos into their workspace (`workspace/repos/<name>`) before iterating; diffs from those checkouts are collected into `diff.patch`; briefing UI lets you bind repos & branches per dev slot before confirming (`PATCH /api/briefing/sessions/{id}/repos`)
- Docs: `design/multi-agent-collaboration.md` and `modules/collaboration.md` (ZH/EN)

### Changed

- Product rename: **Rally** → **Sortie Deck**; Python package `trusted-dev-teams` → `sortie-deck` (`sortie_deck`); CLI `sortie` (legacy `tdt` alias); legacy `rally_*` storage keys / `rally.executors` still accepted
- Orchestrator `startup()` recompiles confirmed custom graphs for HITL resume after restart
- Structured API error `pipeline_not_confirmed` when starting an unconfirmed mission
- **Routing is now conditional edges** rather than node `Command(goto=…)`: this LangGraph version still follows a node's static edges when it returns a `Command`, so both destinations would run in one superstep and collide on the shared state channels
- `RoleId` accepts unregistered role keys as first-class members; `AgentSlot` is no longer a closed union
- `compile_pipelines()` compiles every registered track by default; `list_tracks()` appends tracks beyond express/standard/default
- Executors gained an optional `run_session(ctx, host)` entry point; `run()`-only executors are unaffected
- **Collaboration capabilities are enabled by default** (`subroom_enabled`, `loadout_auto`, `mcp_call_enabled`, `skill_run_enabled`, `im_escalation`); each degrades safely when its dependency is missing, and setting it to `false` disables it
- **Agent switching is a graph node jump and the only routing mechanism**: there are no static edges between stages any more — every node announces its next hop in `handoff_target` and a conditional edge consumes it, with the frozen plan as the router's default. Agent hand-offs, manager join decisions and HITL verdicts all travel through that one router; the legacy `TDT_AGENT_HANDOFF` / `TDT_MANAGER_JOIN_DECISION` switches were removed

## [0.1.0] — 2026-09-23

### Added

- FastAPI control plane with LangGraph pipelines (express / standard / default tracks)
- Pipeline planner agent with human confirm and manual stage editing
- Squad rooms, HITL slash commands, SSE progress
- Auth & RBAC, knowledge (Codex), memory, agent forge, toolkit (Tools / MCP / Skills)
- React TypeScript workbench (Signal Deck) with CN/EN UI and themes
- CLI: `sortie smoke`, `sortie api`
- Pluggable executors: mock, Claude Code, Cursor CLI, deploy shell

[Unreleased]: https://github.com/lpliu-art/sortie-deck-todos/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lpliu-art/sortie-deck-todos/releases/tag/v0.1.0
