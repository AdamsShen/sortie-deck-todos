# Toolkit

## 1. Overview

Catalog of Tools, MCP servers, and Skills (with scripts). Bound to agents / role slots / stages and injected as persona loadout text today.

| Item | Value |
|------|-------|
| Code | `toolkit.py` |
| Store | `data/toolkit.json` |
| API | `/api/toolkit`, `/api/toolkit/import` |
| Examples | `packages/examples/demo-mcp.json`, `packages/examples/demo-skill/SKILL.md` |

## 2. Kinds

| Kind | Key fields | Import |
|------|------------|--------|
| tool | runtime, endpoint | manual |
| mcp (connectors) | transport, command/url, args, env | mcp.json `mcpServers` |
| skill | body markdown, scripts[] | SKILL.md / zip package |

Workbench browses **Tools / Skills / Connectors** tabs with optional `category` domain chips (`engineering` · `product` · `qa` · `ops` · `knowledge` · `browser` · `office`). `featured` items surface in a featured row.

## 3. Binding resolution

Priority at runtime (graph):

1. `stage_toolkits[stage]`
2. `role_toolkits[role]` / `RoleAgent.toolkit_ids`
3. Published agent defaults when mission omits override

Orchestrator builds `toolkit_briefs` strings for persona append.

## 4. Technical design (target)

**Today:** briefs are informational text for LLMs/CLIs.  
**Target:** real invocation layer:

```text
ToolkitRuntime
  McpSessionManager
  SkillScriptRunner (sandbox)
  ToolProcessRunner
```

Secrets: env values stored as secret refs, not plaintext in JSON (P1).

## 5. Development plan

### P0 (2–3 days)

| Task | Acceptance |
|------|------------|
| Example mcp.json + skill zip in `packages/examples/` | import succeeds |
| Docs for binding UI | README + this page |
| Disable item → omitted from brief | test |

### P1 (1–2 weeks)

| Task | Acceptance |
|------|------------|
| MCP client session in coding executor | tool call logged |
| Secret ref for env | not raw in file |
| Skill script allowlist (py/sh) | blocked ext rejected |

### P2 (2 weeks)

| Task | Acceptance |
|------|------------|
| Per-role policy ACL | deny unauthorized tool |
| Marketplace import URL | signed package |

## 6. Test plan

- `tests/test_toolkit.py` import paths
- Binding brief contains selected ids only

## 7. Dependencies

- Agents defaults, Orchestrator create, Graph persona, Web toolkit page
