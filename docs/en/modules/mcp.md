# Remote MCP (Streamable HTTP)

## 1. Overview

Exposes mission capabilities as MCP tools for external agents (local Claude Code,
Cursor, ...). The server is a **`/mcp` route inside the API process**, not a
separate service — that way it shares the single `Orchestrator` instance and the
`data/` directory, so missions started over MCP show up in the workbench live.

| Item | Value |
|------|-------|
| Code | `mcp_server.py` (server), `mcp_http.py` (client, used for self-test) |
| Endpoint | `POST/GET {public_base_url}/mcp` |
| Auth | `Authorization: Bearer sk_sortie_...` (per-user API key) |
| State | **Stateless** — no protocol-level session, any replica can serve |

## 2. Why stateless

The 2026-07-28 revision removed protocol-level sessions and `Mcp-Session-Id`.
The only cross-call state this project needs is the **mission id**, which is
exactly the "server-minted handle" the spec describes — pass it as a normal
argument.

Benefits: the endpoint scales horizontally, needs no sticky sessions, and
clients keep working across server restarts.

## 3. Tool surface (12)

| Tool | R/W | Notes |
|------|-----|-------|
| `start_mission` | write (reversible) | create + confirm pipeline + start; returns `mission_id` and queue depth |
| `await_mission` | read | long-poll; returns on gate/terminal state, otherwise `still_running` |
| `get_mission` | read | status + gate (`response_format=concise\|detailed`) |
| `list_missions` | read | missions I own or that are shared with me |
| `submit_hitl` | write (gated) | `approve` / `reject` / `edit_instruction` / `stop` |
| `stop_mission` | write | stop a mission |
| `list_artifacts` / `read_artifact` | read | artifact list and content (>200k chars truncated) |
| `get_events` | read | tail events by `seq` |
| `list_tracks` | read | available tracks |
| `retrieve_knowledge` | read | knowledge retrieval |
| `create_memory` | write | write a shared memory |

Resources: `mission://{mission_id}/state`, `mission://{mission_id}/artifact/{path}`;
prompt: `plan_mission`.

**Deliberately not done**: mirroring the 117 REST routes; accepting `user_id` as a
tool argument — identity comes from the key only.

## 4. Gate semantics

A gate is a structured object, not a sentence:

```json
{
  "stage": "product_prd",
  "prompt": "Review outputs of `product_prd` and choose an action.",
  "summary": "`product_prd` produced prd.md, acceptance.json",
  "risk": "Read-only review: artifacts can be re-read, rejecting goes back to this stage",
  "irreversible": false,
  "options": ["approve", "reject", "edit_instruction", "stop", "reroute", "switch_harness"],
  "action_help": {"approve": "accept and continue", "...": "..."}
}
```

`deploy*` / `ship*` / `release*` / `publish*` stages are marked `irreversible: true`
automatically, or override with `irreversible: true` in the track config.

All three channels (web / CLI / MCP) hit the same gate: first one wins, the rest
get `conflict` with who handled it.

## 5. Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `TDT_PUBLIC_BASE_URL` | empty (`http://{host}:{port}`) | public URL used in generated snippets |
| `TDT_MCP_ALLOWED_HOSTS` | empty | host allowlist; empty disables DNS-rebinding protection (keys are the real gate) |
| `TDT_API_KEY_RATE_LIMIT_PER_MINUTE` | `240` | in-process soft rate limit per key |

## 6. Client setup

Issue keys from the "API keys" nav page (same level as Decisions / Admin). A key
belongs to one account and an account may hold several. Lifetime is **calendar
months** — 1 / 3 / 6 / 12, defaulting to 1 month. Once expired, `/api/*` and
`/mcp` return 401 immediately; the row stays in the list marked "expired" until
you revoke it.

The secret is shown once, together with four ready-to-paste client configs:

| Client | Target file | Shape |
|---|---|---|
| Claude Code | `.mcp.json` (project root) | `mcpServers.*` + `type: "http"` (required) |
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.*]` + `url` + `http_headers` |
| Cursor | `~/.cursor/mcp.json` (or project `.cursor/mcp.json`) | `mcpServers.*` + `url` + `headers` |
| OpenCode | `opencode.json` | `mcp.*` + `type: "remote"` + `headers` |

Claude Code can also run:

```bash
claude mcp add --transport http sortie-deck http://<host>:8787/mcp \
  --header "Authorization: Bearer sk_sortie_..."
```

Codex `config.toml`:

```toml
[mcp_servers.sortie-deck]
url = "http://<host>:8787/mcp"
http_headers = { Authorization = "Bearer sk_sortie_..." }
```

> The snippets contain the plaintext key. Keep them in a local (not committed)
> config. Revoking is a hard delete: the row disappears from
> `data/api_keys.sqlite` and clients using it fail on their next request.

## 7. Deployment notes

- MCP runs **in the API process**: if you split it out, both containers must share
  the same `data/` volume, otherwise the workbench and SSE cannot see the missions.
- The port must be reachable from clients; `127.0.0.1` is only for local debugging.
- Enable TLS before exposing it publicly — API keys are long-lived credentials.

## 8. Self-test

```python
from sortie_deck.mcp_http import HttpMcpSession
from sortie_deck.toolkit import ToolkitItem

item = ToolkitItem(kind="mcp", name="sortie", transport="http", url="http://127.0.0.1:8787/mcp",
                   config={"headers": {"Authorization": "Bearer sk_sortie_..."}})
session = HttpMcpSession(item)
await session.start()
await session.call_tool("start_mission", {"brief": "..."})
```

`tests/test_mcp_server.py` uses exactly this path as a protocol conformance test.
