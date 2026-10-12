"""Generic harness stage executor — routes *every* stage to the mission's harness.

The initiative-locked harness name arrives in ``ctx.harness`` (set by the graph
from the pipeline state). This executor:

1. resolves the provider and probes it (TTL-cached);
2. bootstraps the mission context pack (rolling memory + bound files + RAG);
3. sends one conversation turn on a per-stage session;
4. records the turn into mission memory, auto-summarising over the token budget;
5. degrades to the legacy stage executors (mock / coding CLIs) when the provider
   is unavailable and ``TDT_HARNESS_FALLBACK`` is on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sortie_deck.models import CODING_ROLE_VALUES, ENG_BUILD_STAGES, StageContext, StageResult


def _legacy_executor_name(ctx: StageContext) -> str:
    """Pre-harness routing (mirrors graph._executor_name minus harness)."""
    stage = ctx.stage
    if stage == "intake":
        return "mock_intake"
    if stage == "product_prd":
        return "mock_product"
    if stage == "design_ui":
        return "mock_design"
    if stage in ENG_BUILD_STAGES:
        return "mock_eng"
    if stage == "eng_selftest" or stage.endswith("_selftest"):
        return "mock_selftest"
    if stage == "eng_integrate":
        return "mock_integrate"
    if stage == "qa_handoff":
        return "mock_handoff"
    if stage == "qa_cases":
        return "mock_qa_cases"
    if stage == "qa_verify":
        return "mock_qa"
    if stage == "deploy_preview":
        return "mock_deploy"
    if str(getattr(ctx.role, "value", ctx.role)) in CODING_ROLE_VALUES:
        return "mock_eng"
    return "mock_intake"


class HarnessStageExecutor:
    name = "harness_stage"

    def probe(self) -> dict[str, str]:
        from sortie_deck.harness_providers.base import get_harness_registry
        from sortie_deck.settings import settings

        harness = settings.default_harness
        status = get_harness_registry().probe(harness)
        return {"status": status.status, "detail": f"({harness}) {status.detail}"}

    async def run(self, ctx: StageContext) -> StageResult:
        from sortie_deck.harness_providers.base import ProbeStatus, get_harness_registry
        from sortie_deck.settings import settings

        harness = (ctx.harness or "").strip() or settings.default_harness
        registry = get_harness_registry()
        probe = ProbeStatus(status="missing", detail=f"harness `{harness}` is not registered")
        if registry.has(harness):
            probe = registry.probe(harness)
        if not probe.ok:
            if settings.harness_fallback:
                return await self._fallback_run(ctx, harness, probe)
            return StageResult(
                status="fail",
                message=f"harness `{harness}` unavailable: {probe.detail}",
                meta={"harness": harness, "probe": probe.as_dict(), "fallback": False},
            )
        return await self._run_harness(ctx, harness)

    # --- degradation ---------------------------------------------------------

    async def _fallback_run(self, ctx: StageContext, harness: str, probe: Any) -> StageResult:
        from sortie_deck.registry import get_registry, run_executor

        legacy_name = _legacy_executor_name(ctx)
        executor = get_registry().get(legacy_name)
        result = await run_executor(executor, ctx)
        result.meta = {
            **(result.meta or {}),
            "harness": harness,
            "harness_fallback": legacy_name,
            "harness_probe": probe.as_dict(),
        }
        if result.status == "fail" and not result.message:
            result.message = f"harness `{harness}` unavailable; legacy executor `{legacy_name}` also failed"
        return result

    # --- main path -------------------------------------------------------------

    async def _run_harness(self, ctx: StageContext, harness: str) -> StageResult:
        from sortie_deck.harness_providers.base import get_harness_registry
        from sortie_deck.mission_memory import get_mission_memory
        from sortie_deck.plugins.coding import (
            _count_impl_files,
            clone_repos,
            ensure_worktree,
        )
        from sortie_deck.settings import settings

        workspace = ensure_worktree(
            settings.data_dir.parent, ctx.initiative_id, settings.worktrees_dir
        )
        repo_section, repo_log = await clone_repos(ctx.repos, workspace)

        pack = await self._build_context_pack(ctx)
        prompt = self._build_prompt(ctx, workspace, pack, repo_section)
        provider = get_harness_registry().get(harness)
        session = await provider.start_session(
            initiative_id=ctx.initiative_id,
            stage=ctx.stage,
            meta={"role": str(getattr(ctx.role, "value", ctx.role))},
        )
        reply = await provider.send(session, prompt, cwd=str(workspace), context_pack=pack)

        mm = get_mission_memory()
        mm.record_turn(
            ctx.initiative_id,
            session.native_id or session.id,
            stage=ctx.stage,
            role=str(getattr(ctx.role, "value", ctx.role)),
            prompt=prompt,
            reply=reply.text,
            provider=harness,
        )
        mm.append_memory(
            ctx.initiative_id,
            f"Stage `{ctx.stage}` ({ctx.role}) via `{harness}`: {reply.text[:600] or reply.status}",
        )
        await mm.maybe_summarize(ctx.initiative_id)

        log_lines = [f"harness={harness}", f"session={session.native_id or session.id}"]
        log_lines.extend(repo_log)
        if ctx.toolkit_ids:
            log_lines.append(f"toolkits={','.join(ctx.toolkit_ids)}")
        log_lines.append(f"reply_status={reply.status}")
        log_lines.append((reply.text or "")[:3500])

        needs_hitl = reply.status in {"needs_permission", "needs_auth"}
        ok = reply.status == "ok" and bool(reply.text.strip())
        artifacts, diff = await self._artifacts(ctx, workspace, reply, log_lines, harness=harness)
        meta = {
            "harness": harness,
            "session_id": session.native_id or session.id,
            "provider_reply_status": reply.status,
            "workspace": str(workspace),
            "files_written": _count_impl_files(workspace),
            "context_tokens": int(pack.get("token_estimate") or 0),
        }
        if needs_hitl:
            return StageResult(
                status="needs_hitl",
                message=(
                    f"harness `{harness}` 需要授权/权限（{reply.status}）。"
                    "请在门禁放行重试，或经 HITL 授权更换 harness。"
                ),
                artifacts=artifacts,
                proposed_next=ctx.stage,
                meta={**meta, "permission_block": True, "auth_block": reply.status == "needs_auth"},
            )
        return StageResult(
            status="ok" if ok else "fail",
            message=f"harness `{harness}` stage finished" if ok else (
                f"harness `{harness}` 未产出有效回复（见 reply.md / implementation.md）"
            ),
            artifacts=artifacts,
            proposed_next=("qa_verify" if ok and str(getattr(ctx.role, "value", ctx.role)) in CODING_ROLE_VALUES else None),
            meta=meta,
        )

    # --- context -----------------------------------------------------------------

    async def _build_context_pack(self, ctx: StageContext) -> dict[str, Any]:
        from sortie_deck.mission_memory import get_mission_memory
        from sortie_deck.orchestrator import get_orchestrator

        mm = get_mission_memory()
        ini = None
        artifact_paths: list[str] = []
        try:
            ini = get_orchestrator().get(ctx.initiative_id)
        except Exception:  # noqa: BLE001 — orchestrator optional in unit tests
            ini = None
        if ini is not None:
            artifact_paths = [ref.path for ref in ini.artifacts][-40:]
        pack = await mm.bootstrap_context(ini, artifact_paths=artifact_paths) if ini is not None else None
        out: dict[str, Any] = {
            "memory_md": pack.memory_md if pack else "",
            "context_files": dict(pack.context_files) if pack else {},
            "artifact_index": list(pack.artifact_index) if pack else [],
            "token_estimate": pack.token_estimate if pack else 0,
            "rag_brief": self._rag_brief(ctx),
        }
        return out

    def _rag_brief(self, ctx: StageContext) -> str:
        try:
            from sortie_deck.knowledge_gateway import gateway_from_settings
            from sortie_deck.settings import settings

            gateway = gateway_from_settings(settings)
            hits = gateway.retrieve(f"{ctx.brief} {ctx.stage}", limit=4)
            if not hits:
                return ""
            lines = [f"- [{h.source}] {h.title}: {h.content[:300]}" for h in hits]
            return "Knowledge brief:\n" + "\n".join(lines)
        except Exception:  # noqa: BLE001 — RAG is an enhancement
            return ""

    def _build_prompt(
        self,
        ctx: StageContext,
        workspace: Path,
        pack: dict[str, Any],
        repo_section: str,
    ) -> str:
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)
        sections: list[str] = [
            ctx.persona or f"You are the {ctx.role} agent in Sortie Deck.",
            (
                "You are running inside a Sortie Deck mission. Complete THIS stage and "
                "produce the stage artifact. Do not ask the user questions — decide and act."
            ),
            f"Stage: `{ctx.stage}` — Role: `{getattr(ctx.role, 'value', ctx.role)}`",
            f"Workspace (cwd): `{workspace}` — you may create/edit files here.",
            f"Human instruction: {ctx.human_instruction or 'none'}",
            f"Mission brief:\n{ctx.brief}",
            f"Upstream PRD:\n{prd}",
        ]
        memory_md = str(pack.get("memory_md") or "")
        if memory_md.strip():
            sections.append(f"Rolling mission memory (from bound files):\n{memory_md.strip()}")
        context_files = pack.get("context_files") or {}
        for name, content in context_files.items():
            sections.append(f"Bound file `{name}`:\n{content.strip()[:6000]}")
        rag = str(pack.get("rag_brief") or "")
        if rag:
            sections.append(rag)
        if ctx.memory_brief:
            sections.append(f"Layered memory brief:\n{ctx.memory_brief}")
        if ctx.toolkit_brief:
            sections.append(ctx.toolkit_brief)
        if repo_section:
            sections.append(repo_section.strip())
        artifact_index = list(pack.get("artifact_index") or [])
        if artifact_index:
            sections.append(
                "Mission artifact index:\n" + "\n".join(f"- `{p}`" for p in artifact_index[:20])
            )
        sections.append(
            "Output contract: finish with a markdown section titled `## Result` summarising "
            "what this stage produced (files written, decisions, next step)."
        )
        return "\n\n".join(s for s in sections if s and s.strip())

    # --- artifacts ------------------------------------------------------------------

    async def _artifacts(
        self,
        ctx: StageContext,
        workspace: Path,
        reply: Any,
        log_lines: list[str],
        *,
        harness: str,
    ) -> tuple[dict[str, str], str]:
        """Build the stage's artifact set, always satisfying the stage contract."""
        from sortie_deck.contracts import REQUIRED
        from sortie_deck.plugins.coding import _count_impl_files, _git_diff
        from sortie_deck.settings import settings

        needs_diff = "diff.patch" in REQUIRED.get(ctx.stage, [])
        diff = ""
        if needs_diff:
            repo_root = settings.data_dir.parent
            diff = await _git_diff(workspace, repo_root, ctx.repos)
        log = "\n".join(log_lines)
        reply_md = (
            f"# Harness stage reply — `{ctx.stage}`\n\n{reply.text}\n\n"
            f"## Log\n{log[:4000]}\n"
        )
        artifacts: dict[str, str] = {"reply.md": reply_md}
        for name in REQUIRED.get(ctx.stage, []):
            if name == "diff.patch":
                artifacts[name] = diff
            elif name.endswith(".json"):
                artifacts[name] = json.dumps(
                    {
                        "stage": ctx.stage,
                        "status": "ok" if reply.status == "ok" else "failed",
                        "summary": (reply.text or "")[:1500],
                        "harness": harness,
                        "workspace": str(workspace),
                        "files_written": _count_impl_files(workspace),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                artifacts[name] = (
                    f"# {name} — stage `{ctx.stage}` (harness `{harness}`)\n\n"
                    f"{reply.text}\n\n## Log\n{log[:2000]}\n"
                )
        return artifacts, diff
