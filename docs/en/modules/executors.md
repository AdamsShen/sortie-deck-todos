# Executors & Plugins

## 1. Overview

Stage runners that turn `StageContext` into `StageResult` artifacts. Coding/deploy providers are swappable.

| Item | Value |
|------|-------|
| Registry | `registry.py`, `plugins/factory.py` |
| Queue | `plugins/queue.py` (`ENG_QUEUE`) |
| Impl | `mock.py`, `coding.py`, `deploy.py` |

## 2. Protocol

```python
async def run(self, ctx: StageContext) -> StageResult:
    # returns status, message, artifacts{filename: content}, qa_passed?, proposed_next?
```

`StageContext` includes: brief, persona (+ toolkit brief), upstream artifact texts, workspace_dir, permissions.

## 3. Built-ins

| Name | Stage use | Notes |
|------|-----------|-------|
| `mock_intake` / `mock_product` / `mock_eng` / `mock_qa` / `mock_deploy` | demos | deterministic artifacts |
| `mock_qa_cases` / `mock_selftest` / `mock_integrate` / `mock_handoff` | standard track | |
| `claude_code` / `cursor_cli` | eng_implement | worktree under `data/worktrees` |
| `deploy_shell` | deploy | `TDT_DEPLOY_CMD` template |
| `llm_product` | optional PRD | needs LLM keys |

Resolution: template executor field, else role/stage heuristics in `graph._executor_name` (coding_executor / deploy_executor from state).

## 4. Technical design (target)

- Provider health: `probe()` → version/path
- Streaming logs → room `msg_type=stage`
- Sandbox profiles for shell (cwd allowlist, env scrub)
- Entry points: `sortie_deck.executors` pkg for community plugins

## 5. Development plan

### P0 (2–3 days)

| Task | Acceptance |
|------|------------|
| README section: env for Claude/Cursor | copy-paste works |
| Executor list test equals factory register | pytest |
| Fail soft when CLI missing | clear StageResult message |

### P1 (1–2 weeks)

| Task | Acceptance |
|------|------------|
| Log streaming hook | squad sees chunks |
| `probe()` on `/api/executors` | status field |
| MCP tool calls from coding executor | flag gated |

### P2 (2 weeks)

| Task | Acceptance |
|------|------------|
| Plugin discovery via entry points | load external |
| Resource limits (time/cpu) | enforced |

## 6. Test plan

- Mock contract satisfaction per stage
- Eng queue concurrency smoke
- Missing binary → non-crash

## 7. Dependencies

- Graph stage mapping, Toolkit briefs, Artifacts store, Settings

## 8. Context cost control

A long stage costs roughly `requests × per-request context`. Measured on 2026-10-27: 196 requests
with a median 287K input tokens per request. Three orthogonal, individually revertible measures:

### 8.1 Upstream artifacts: summary + path only

`artifact_summary.py` builds an `ArtifactBrief` per upstream artifact (name / absolute path / chars /
summary). The prompt lists briefs instead of full text; the agent reads a path on demand.

- Short artifacts (`TDT_PROMPT_INLINE_ARTIFACT_CHARS`, default 1200 chars) are inlined verbatim
- Longer ones are summarised concurrently by the LLM (`TDT_PROMPT_ARTIFACT_SUMMARY_TIMEOUT`, default 12s);
  failures / timeouts / no LLM fall back to a rule summary (markdown outline, JSON keys)
- `diff.patch` is always rule-summarised (file list + added/removed line counts)
- Summaries are cached per `path + content sha1` in `data/missions/<id>/memory/artifact_summaries.json`
- Revert: `TDT_PROMPT_ARTIFACT_BRIEFS=false` restores full PRD injection

`StageContext` therefore gains `upstream_paths` (same keys as `upstream_artifacts`, absolute paths);
the full-text semantics of `upstream_artifacts` stay unchanged (consult / mock / diff rely on it).

### 8.2 On-demand MCP mounting

`harness_providers/mcp_binding.py` converts MCP items resolved from the stage binding / proactive
recall / `toolkit_pick` into SDK `mcp_servers` with `strict_mcp_config=True`, instead of loading every
global `mcpServers` entry from the local `~/.claude.json`.

- Nothing bound means no MCP for the stage — no tool schemas in context
- The set is fixed before the session starts; MCPs picked mid-stage take effect from the next stage
- The claude coding CLI writes `.tdt/mcp-config.json` and passes `--mcp-config` + `--strict-mcp-config`;
  cursor CLI has no such flag and logs that MCP is not mounted
- Revert: `TDT_HARNESS_MCP_STRICT=false`

### 8.3 In-session auto-compaction

The `claude_code` provider injects `CLAUDE_CODE_AUTO_COMPACT_WINDOW=<TDT_HARNESS_AUTO_COMPACT_WINDOW>`
(default 200000; official range 100K–1M) through `ClaudeAgentOptions.env`; coding CLI subprocesses get
the same variable.

- `compact_boundary` events land in `StageResult.meta["context"]` (`compacted` / `at_token` /
  `post_token` / `trigger`) plus one info log line
- `num_turns` / `usage` / `cost_usd` / `stop_reason` are recorded too
- `TDT_HARNESS_MAX_TURNS` (default 0 = unlimited) caps non-converging stages
