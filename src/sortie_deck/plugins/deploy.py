from __future__ import annotations

import json
import os

from sortie_deck import observability as obs
from sortie_deck.models import StageContext, StageResult


class DeployShellExecutor:
    name = "deploy_shell"

    def __init__(self) -> None:
        self.cmd_template = os.environ.get(
            "TDT_DEPLOY_CMD", "echo deploy {initiative_id}"
        )

    async def run(self, ctx: StageContext) -> StageResult:
        import asyncio

        cmd = self.cmd_template.format(
            initiative_id=ctx.initiative_id,
            title=ctx.title,
            stage=ctx.stage,
        )
        with obs.span(
            "process.exec",
            **{
                "process.command": obs.truncate(cmd, 200),
                "sortie.shell": True,
            },
        ) as proc_span:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
            exit_code = proc.returncode or 0
            obs.set_attributes(proc_span, {"sortie.exit_code": exit_code})
            if exit_code != 0:
                # 部署失败仍包装成 StageResult 返回（由图的边决定回退），不会抛异常，
                # 所以必须在这里显式标 ERROR，否则 trace 上看不出部署挂了
                obs.mark_failure(proc_span, f"deploy shell exit={exit_code}")
        out = (out_b or b"").decode("utf-8", errors="replace")
        err = (err_b or b"").decode("utf-8", errors="replace")
        ok = exit_code == 0
        return StageResult(
            status="ok" if ok else "fail",
            message="deploy shell finished" if ok else "deploy shell failed",
            artifacts={
                "deploy_log.md": f"# Deploy\n\n```\n{out}\n{err}\n```\n",
            },
            proposed_next="done" if ok else "deploy_preview",
            meta={"cmd": cmd, "exit": exit_code},
        )


class LlmProductExecutor:
    """Product/PRD executor via LlmClient (HTTP API or Claude CLI fallback)."""

    name = "llm_product"

    async def run(self, ctx: StageContext) -> StageResult:
        from sortie_deck.llm import LlmClient

        llm = LlmClient(timeout=120.0)
        if not llm.enabled:
            from sortie_deck.plugins.mock import MockProductExecutor

            return await MockProductExecutor().run(ctx)

        system = (
            f"{ctx.persona}\n\n"
            "Write a PRD. Return ONLY JSON with keys prd_md (markdown string) "
            "and acceptance (object with criteria array)."
        )
        user = f"Brief:\n{ctx.brief}\n\nInstruction: {ctx.human_instruction or ''}"
        try:
            data = await llm.chat_json(system=system, user=user)
        except Exception as exc:  # noqa: BLE001
            from sortie_deck.plugins.mock import MockProductExecutor

            result = await MockProductExecutor().run(ctx)
            result.message = f"LLM product failed ({exc}); used mock"
            result.meta = {**(result.meta or {}), "llm_error": str(exc)}
            return result

        return StageResult(
            status="ok",
            message=f"LLM PRD drafted ({llm.backend})",
            artifacts={
                "prd.md": data.get("prd_md", "# PRD\n"),
                "acceptance.json": json.dumps(
                    data.get("acceptance", {"criteria": []}), ensure_ascii=False, indent=2
                ),
            },
            proposed_next="eng_implement",
            meta={"planner_backend": llm.backend},
        )
