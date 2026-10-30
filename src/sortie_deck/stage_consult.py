"""Stage-time consult: agent decides who upstream/downstream to clarify with.

Records land in the squad room (msg_type consult / consult_reply) and as
``consult_log.md`` on the stage — used as review / confirmation history.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from sortie_deck.models import new_id

PostRoomFn = Callable[..., Awaitable[Any]]
ReplyFn = Callable[[str, str], Awaitable[str]]  # (role_id, question) -> reply text


class _HasRoles(Protocol):
    roles: list[Any]


_SKIP_STAGES = frozenset({"intake", "done"})

# Neighbor hints when LLM unavailable
_DEFAULT_TARGETS: dict[str, list[str]] = {
    "product_prd": ["design", "eng"],
    "design_ui": ["product", "eng_web"],
    "eng_implement": ["product", "qa"],
    "eng_web": ["product", "qa", "design"],
    "eng_ios": ["product", "qa", "design"],
    "eng_android": ["product", "qa", "design"],
    "eng_backend": ["product", "qa"],
    "eng_agent": ["product", "qa"],
    "qa_cases": ["product", "eng"],
    "qa_verify": ["product", "eng", "eng_web"],
    "deploy_preview": ["eng", "qa"],
}


def _available_roles(ini: _HasRoles) -> set[str]:
    return {r.role.value if hasattr(r.role, "value") else str(r.role) for r in (ini.roles or [])}


def _heuristic_plan(stage: str, role: str, brief: str, available: set[str]) -> dict[str, Any]:
    targets = [t for t in _DEFAULT_TARGETS.get(stage, ["product", "qa"]) if t in available and t != role]
    if not targets:
        return {"need_consult": False, "questions": []}
    snippet = (brief or "").strip().replace("\n", " ")[:120]
    q_map = {
        "product": f"对阶段 `{stage}`：验收边界是否仍以 brief 为准？有无必须澄清的范围？（上下文：{snippet}…）",
        "qa": f"对阶段 `{stage}`：测试侧最担心的失败路径是什么？有无用例要先对齐？",
        "design": f"对阶段 `{stage}`：视觉/交互是否有硬约束（动效、触控、可访问）需要开发遵守？",
        "eng": f"对阶段 `{stage}`：实现上是否有依赖或风险需要测试/产品知情？",
        "eng_web": f"Web 交付物路径与手工验收方式是否确认？（静态打开 / 本地 server）",
    }
    questions = []
    for t in targets[:2]:
        questions.append(
            {
                "to": t,
                "question": q_map.get(t) or f"关于 `{stage}`，请确认与你职责相关的约束。",
            }
        )
    return {"need_consult": True, "questions": questions, "rationale": "heuristic neighbor consult"}


async def _llm_plan(
    *,
    stage: str,
    role: str,
    brief: str,
    upstream: dict[str, str],
    available: set[str],
) -> dict[str, Any] | None:
    from sortie_deck.llm import LlmClient

    # 必须尊重 TDT_LLM_CLI_FALLBACK / settings.llm_cli_fallback：这里曾经硬编码
    # True，导致关闭 CLI 回退后仍会 shell 调 `claude -p`，把阶段卡到 60s 超时。
    client = LlmClient()
    if not client.enabled:
        return None
    ups = "\n".join(f"- {k}: {(v or '')[:400]}" for k, v in list(upstream.items())[:6])
    user = (
        f"Your role: {role}\nStage: {stage}\n"
        f"Available roles: {sorted(available)}\n"
        f"Brief:\n{brief[:2000]}\n"
        f"Upstream artifacts:\n{ups or '(none)'}\n\n"
        "Decide whether to consult upstream/downstream before executing.\n"
        "Rules: at most 2 questions; only use available roles except yourself; "
        "need_consult=false if brief+upstream already clear."
    )
    try:
        data = await client.chat_json(
            system=(
                "You plan teammate consults for a software delivery fireteam. "
                'Reply JSON: {"need_consult": bool, "rationale": str, '
                '"questions": [{"to": "<role_id>", "question": "<one concrete question>"}]}'
            ),
            user=user,
            temperature=0.2,
        )
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    qs = []
    for item in data.get("questions") or []:
        if not isinstance(item, dict):
            continue
        to = str(item.get("to") or "").strip()
        q = str(item.get("question") or "").strip()
        if to in available and to != role and q:
            qs.append({"to": to, "question": q})
    return {
        "need_consult": bool(data.get("need_consult")) and bool(qs),
        "rationale": str(data.get("rationale") or ""),
        "questions": qs[:2],
    }


async def run_stage_consult(
    *,
    ini: _HasRoles,
    initiative_id: str,
    stage: str,
    role: str,
    brief: str,
    upstream: dict[str, str],
    role_titles: dict[str, str],
    post_room: PostRoomFn,
    reply_as_role: ReplyFn,
) -> dict[str, str]:
    """Run one consult round. Returns optional artifact map (consult_log.md)."""
    if stage in _SKIP_STAGES:
        return {}
    available = _available_roles(ini)
    if len(available) < 2:
        return {}

    plan = await _llm_plan(
        stage=stage, role=role, brief=brief, upstream=upstream, available=available
    )
    if plan is None:
        plan = _heuristic_plan(stage, role, brief, available)
    if not plan.get("need_consult") or not plan.get("questions"):
        return {}

    thread_id = new_id("csl_")
    from_title = role_titles.get(role) or role
    lines = [
        f"# Consult log · `{stage}`",
        "",
        f"- Owner: **{from_title}** (`{role}`)",
        f"- Thread: `{thread_id}`",
        f"- Rationale: {plan.get('rationale') or 'n/a'}",
        "",
    ]

    await post_room(
        initiative_id,
        text=(
            f"💬 **{from_title}** 在节点 `{stage}` 发起上下游确认"
            + (f"：{plan.get('rationale')}" if plan.get("rationale") else "。")
        ),
        actor_kind="agent",
        actor_name=from_title,
        role=role,
        msg_type="consult",
        meta={
            "stage": stage,
            "thread_id": thread_id,
            "kind": "consult_open",
            "targets": [q["to"] for q in plan["questions"]],
        },
    )

    for item in plan["questions"]:
        to = item["to"]
        question = item["question"]
        to_title = role_titles.get(to) or to
        await post_room(
            initiative_id,
            text=f"❓ @{to}（{to_title}）\n{question}",
            actor_kind="agent",
            actor_name=from_title,
            role=role,
            msg_type="consult",
            meta={
                "stage": stage,
                "thread_id": thread_id,
                "kind": "consult_ask",
                "from_role": role,
                "to_role": to,
            },
        )
        lines.append(f"## → {to_title} (`{to}`)")
        lines.append("")
        lines.append(f"**Q:** {question}")
        lines.append("")
        try:
            answer = await reply_as_role(to, question)
        except Exception as exc:  # noqa: BLE001
            answer = f"（未能自动回复：{exc}）"
        await post_room(
            initiative_id,
            text=f"↪️ {answer}",
            actor_kind="agent",
            actor_name=to_title,
            role=to,
            msg_type="consult_reply",
            meta={
                "stage": stage,
                "thread_id": thread_id,
                "kind": "consult_answer",
                "from_role": to,
                "to_role": role,
            },
        )
        lines.append(f"**A:** {answer}")
        lines.append("")

    await post_room(
        initiative_id,
        text=f"✅ 节点 `{stage}` 上下游确认结束（thread `{thread_id}`），继续执行。",
        actor_kind="system",
        actor_name="Sortie",
        msg_type="consult",
        meta={"stage": stage, "thread_id": thread_id, "kind": "consult_close"},
    )
    lines.append("---")
    lines.append("Consult closed — stage execution resumes.")
    return {"consult_log.md": "\n".join(lines)}
