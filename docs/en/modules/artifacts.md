# Artifacts & Store

## 1. Overview

Filesystem-backed artifact storage for stage outputs; initiative keeps `ArtifactRef` index; API serves file bytes.

| Item | Value |
|------|-------|
| Code | `artifacts.py` |
| Root | `data/artifacts/<initiative>/<stage>/` |
| API | `GET /api/initiatives/{id}/artifacts/{path}` |

## 2. Write path

Executor returns map → `ArtifactStore.write_text` → relative path into graph `upstream` → orchestrator `_sync_artifacts_from_upstream` appends refs.

## 3. Security requirements

- Prevent path traversal (`..`, absolute paths)
- Authorize artifact read only for authenticated users (optionally mission members later)
- Do not execute artifact contents

## 4. Technical design (target)

- Content-addressed blob store (`sha256`) + thin refs
- Diff API between qa attempts / eng loops
- S3-compatible backend via settings
- Signed short-lived download URLs

## 5. Development plan

### P0 (2 days)

| Task | Acceptance |
|------|------------|
| Path traversal tests | rejected |
| Auth required on download | 401 without token |
| Document layout on disk | this page |

### P1 (1 week)

| Task | Acceptance |
|------|------------|
| Diff two artifact versions | API + simple UI |
| Content hash in ArtifactRef.meta | stored |

### P2 (2 weeks)

| Task | Acceptance |
|------|------------|
| S3 backend | feature flag |
| Retention job | old missions pruned |

## 6. Dependencies

- Graph writes, Orchestrator sync, Web preview pane, Contracts filenames

## 7. Mission input snapshots (`input/`, 2026-10)

Imported materials no longer live as a text excerpt inside the mission JSON: originals are stored on disk, text is extracted, and a manifest is the single index.

| Path | Content |
|---|---|
| `data/missions/<id>/input/manifest.json` | Single index: `id/name/kind/mime/size/sha256/pinned/extract_status` + relative paths |
| `input/files/<mat_id>__<name>` | Original binary (PDF / DOCX / XLSX / images …) |
| `input/text/<mat_id>.md` | Extracted text (text formats, pdf, docx, xlsx; images report `skipped`) |
| `input/brief.md` | The only brief snapshot (pinned materials inlined or referenced by path) |

- Code: `mission_input.py` — atomic writes (lock + `tmp`→`os.replace`), sha256 de-duplication, 50MB per file / 500MB per mission quota.
- API: `POST /api/missions/{id}/input` (multipart), `GET` (list), `PATCH` (pin), `DELETE` (removes the stored files too).
- Injection (dynamic loading): pinned text ≤4000 chars is inlined into the brief; longer or binary materials only leave an `input/...` pointer, and executors add an `## Input index` section (`StageContext.inputs`) so the agent reads content on demand.
- Lifecycle: kept with the mission (workspace recycling does not touch it); intake directories that never became a mission are reclaimed by `maintenance.sweep_orphan_missions` after `TDT_INTAKE_TTL_DAYS` (default 7 days).
