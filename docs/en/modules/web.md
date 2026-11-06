# Web Workbench (Signal Deck)

## 1. Overview

The workbench is the **human UI entry**: login, dashboard, missions, squad chat, pipeline confirm/editor, Codex, memory, agent forge, toolkit, admin, CN/EN, themes.

| Item | Value |
|------|-------|
| Stack | Vite + React + TypeScript |
| Root | `apps/web` |
| API base | same-origin / proxy to `:8787` |

## 2. Scope & non-goals

**In scope**

- Authenticated SPA views listed above
- Pipeline proposal confirm + manual stage edit while draft
- Operator identity chip (avatar / role / callsign)
- CN/EN i18n (`i18n.ts`), theme dock

**Out of scope**

- Mobile-native apps
- Full design-system package publish
- SSR / Next.js migration (P2 discussion only)

## 3. Code map

| Path | Role |
|------|------|
| `apps/web/src/App.tsx` | Monolithic view router + state (to split) |
| `apps/web/src/types.ts` | Shared DTO types |
| `apps/web/src/i18n.ts` | Copy tables |
| `apps/web/src/styles.css` | Theme tokens + layout |
| `apps/web/src/themes.ts` | Theme ids / persistence |
| `apps/web/src/RippleField.tsx` | Atmosphere FX |

## 4. View map (entry → deep)

```text
login
  → home (dashboard)
  → tasks (create + list) → squad (chat + pipeline editor + HITL)
  → knowledge | memory | agents | toolkit | admin
```

## 5. Key UX contracts

### Settings · read-only visibility of Git credentials

- The "Git credentials" page is open to every authenticated user; workspace credentials and workspace repos (name / URL / default branch / bound credential) are visible to all.
- Only `admin` sees the add/disable controls for workspace credentials and repos; `member` is read-only. Personal credentials and private repos remain owner-only.
- `GET /api/git/workspace-repos` and `GET /api/git/workspace-credentials` are `require_user` endpoints returning redacted metadata; write endpoints under `/api/admin/git/*` are gated by `require_admin`.

### Pipeline confirm (squad draft)

1. Show planner rationale + stage list from `meta.pipeline_specs`
2. Edit: reorder / remove / add from catalog / toggle HITL
3. `POST …/pipeline/confirm` or `PATCH …/pipeline` with `confirm`
4. Start disabled until `meta.pipeline_confirmed`

### Auth

- Token in `localStorage.sortie_token`
- 401 → clear token → login view

## 6. Technical design

### Current

- Single `App.tsx` owns most state and fetch helpers
- SSE for mission events when squad selected

### Target

```text
apps/web/src/
  api/client.ts          # fetch + errors
  features/auth|tasks|squad|...
  components/OperatorChip, PipelineEditor, PipelineRail
  routes.tsx
```

- React Query (or similar) for server state
- Route-level code splitting
- a11y: focus rings, reduced-motion already partially respected

## 7. Development plan

### P0 — Componentize entry surfaces (3–5 days)

| Task | Acceptance |
|------|------------|
| Extract `OperatorChip` / header meta | visual parity |
| Extract `PipelineEditor` + catalog picker | confirm/edit still works |
| Extract `api()` client module | all calls via client |
| Smoke: login → create → confirm → start | manual checklist |

### P1 — Routing & data layer (1–2 weeks)

| Task | Acceptance |
|------|------------|
| React Router views | deep-link `/squad/:id` |
| Query cache for initiatives/agents/toolkit | fewer refetch bugs |
| Error boundary + toast | no blank screen on API fail |

### P2 — Quality gates (1–2 weeks)

| Task | Acceptance |
|------|------------|
| Playwright: login + create mission | CI job |
| Storybook for PipelineRail / chips | optional package |
| Bundle budget | documented Lighthouse baseline |

## 8. Test plan

- `npx tsc --noEmit`
- Manual CN/EN switch, theme switch
- Pipeline editor edge: cannot remove `intake`/`done`

## 9. Dependencies

- Requires: Auth API, Initiatives / Pipeline APIs
- Blocks: most human-facing feature demos
