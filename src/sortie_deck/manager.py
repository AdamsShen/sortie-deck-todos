"""Mission manager — the *narrow* decision layer.

Collaboration model (see the design review this implements):

* **serial step** → the finishing agent hands off on its own; the manager only
  *marks the step complete* afterwards (record-keeping, never blocking);
* **parallel join** → two agents finished at once and neither may arbitrate for
  the other, so the **manager decides** which agent runs next;
* **conflict / undecidable** → the manager decides, then escalates to a human.

Nothing else routes through this module. Keeping the trigger set this small is
deliberate: a manager that approves every step would stall the chain.

``plan_digest`` freezes the plan so a resume can never silently run against an
edited pipeline.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.models import new_id, role_key

ManagerTrigger = Literal["join", "conflict", "escalate"]
ManagerAction = Literal["continue", "reroute", "request_human", "pause"]

_JOIN_SYSTEM = """You are the mission manager for a multi-agent delivery mission.
Several sibling agents just finished the same parallel wave. Pick exactly one
next node from the allowed candidates. Return JSON:
  target_stage: one id from the allowed candidates
  action: "continue" | "reroute"
  rationale: one short line
  confidence: number between 0 and 1
Prefer "continue" to the plan's declared next node when every branch succeeded.
Reroute back to a branch only when that branch's output blocks the join.
Never invent a stage id.
Return ONLY JSON.
"""

_ADVISE_SYSTEM = """You are the mission manager for a multi-agent delivery mission.
An executing agent hit a decision it cannot settle on its own. Answer only with
what the mission brief and the agent's question actually support:
  resolution: a concrete, directly actionable decision (one or two sentences)
  confidence: number between 0 and 1
If the question needs human authority (scope change, budget, compliance, an
unstated business fact), return an empty resolution and confidence 0 — the
mission will then escalate to a human.
Never invent requirements, numbers, deadlines or approvals.
Return ONLY JSON.
"""


def plan_digest(stages: list[dict[str, Any]]) -> str:
    """Stable hash over the frozen plan (node ids, roles, parallel grouping).

    Resume compares this against the stored digest: a mismatch means the plan
    was edited after the run started, and the run must be re-confirmed rather
    than resumed against a graph it was never checked against.
    """
    payload = [
        {
            "name": s.get("name") or s.get("id"),
            "role": str(s.get("role") or ""),
            "parallel_group": str(s.get("parallel_group") or ""),
            "branch": str(s.get("branch") or ""),
            "hitl_after": bool(s.get("hitl_after")),
        }
        for s in stages or []
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def plan_digest_matches(expected: str | None, stages: list[dict[str, Any]]) -> bool:
    """True when ``expected`` is empty (legacy runs) or equals the current plan."""
    if not expected:
        return True
    return expected == plan_digest(stages)


class ManagerDecision(BaseModel):
    """The manager's routing output — the only thing it may hand to the graph."""

    decision_id: str = Field(default_factory=lambda: new_id("mgr_"))
    trigger: ManagerTrigger
    stage: str
    action: ManagerAction = "continue"
    target_stage: str | None = None
    rationale: str = ""
    # 升级裁决的正文（仅 trigger=conflict 且 Manager 能自行裁决时非空）；
    # 空串表示 Manager 也判断不了，调用方应继续升级真人。
    resolution: str = ""
    confidence: float = 0.5
    plan_digest: str = ""

    def resolved_target(self) -> str | None:
        return self.target_stage


class BranchStatus(BaseModel):
    """Compact per-branch outcome fed into a join decision."""

    stage: str
    role: str = ""
    status: str = "ok"
    message: str = ""


class MissionManager:
    """Join arbitration + escalation for one mission."""

    def __init__(
        self,
        stages: list[dict[str, Any]],
        *,
        client: Any | None = None,
        use_llm: bool = False,
    ) -> None:
        self.stages = list(stages or [])
        self.digest = plan_digest(self.stages)
        self._client = client
        self.use_llm = use_llm
        self._node_ids = [str(s.get("name") or s.get("id")) for s in self.stages]
        self._role_by_stage = {
            str(s.get("name") or s.get("id")): str(s.get("role") or "") for s in self.stages
        }

    # --- plan facts ----------------------------------------------------

    def node_ids(self) -> list[str]:
        return list(self._node_ids)

    def role_of(self, stage: str) -> str:
        return self._role_by_stage.get(stage, "")

    def is_plan_node(self, stage: str) -> bool:
        return stage in self._role_by_stage

    def static_next(self, after_stage: str) -> str:
        """Plan-declared successor of ``after_stage`` (last node → ``done``)."""
        idx = next((i for i, n in enumerate(self._node_ids) if n == after_stage), None)
        if idx is None or idx + 1 >= len(self._node_ids):
            return "done"
        nxt = self._node_ids[idx + 1]
        # parallel members collapse into their wave node, resolved by the caller
        return nxt

    def join_candidates(self, last_member: str, *, failed: list[str] | None = None) -> list[str]:
        """Plan-legal next nodes after a parallel wave, in preference order."""
        candidates: list[str] = []
        nxt = self.static_next(last_member)
        if nxt:
            candidates.append(nxt)
        for stage in failed or []:
            if stage not in candidates:
                candidates.append(stage)
        if "done" not in candidates:
            candidates.append("done")
        return candidates

    # --- decisions -----------------------------------------------------

    async def decide_join(
        self,
        *,
        group: str,
        last_member: str,
        branches: list[BranchStatus],
        brief: str = "",
        upstream_keys: list[str] | None = None,
    ) -> ManagerDecision:
        """Decide the next node after a parallel wave.

        Rule path is O(1) and covers the common case: every branch succeeded →
        continue to the plan's next node, no model call, confidence 1.0.
        """
        failed = [b.stage for b in branches if b.status not in {"ok", "success"}]
        candidates = self.join_candidates(last_member, failed=failed)
        plan_next = self.static_next(last_member)

        if not failed:
            return ManagerDecision(
                trigger="join",
                stage=last_member,
                action="continue",
                target_stage=plan_next,
                rationale=f"all {len(branches)} branches succeeded — continue to `{plan_next}`",
                confidence=1.0,
                plan_digest=self.digest,
            )
        if len(candidates) <= 1:
            return ManagerDecision(
                trigger="join",
                stage=last_member,
                action="reroute" if failed else "continue",
                target_stage=candidates[0] if candidates else plan_next,
                rationale=f"branch failure(s) {failed} — only one candidate on the plan",
                confidence=0.7,
                plan_digest=self.digest,
            )

        llm_target = await self._llm_join(
            group=group,
            last_member=last_member,
            branches=branches,
            candidates=candidates,
            brief=brief,
            upstream_keys=upstream_keys or [],
        )
        if llm_target in candidates:
            return ManagerDecision(
                trigger="join",
                stage=last_member,
                action="reroute" if llm_target in failed else "continue",
                target_stage=llm_target,
                rationale="manager (llm) join decision",
                confidence=0.75,
                plan_digest=self.digest,
            )
        # Deterministic fallback: first failed branch, else the plan's next node
        target = failed[0] if failed else plan_next
        return ManagerDecision(
            trigger="join",
            stage=last_member,
            action="reroute" if failed else "continue",
            target_stage=target,
            rationale=f"rule fallback after branch failure(s) {failed}",
            confidence=0.6,
            plan_digest=self.digest,
        )

    async def decide_conflict(
        self,
        *,
        stage: str,
        candidates: list[str],
        context: str = "",
    ) -> ManagerDecision:
        """Conflict resolution among plan nodes; falls back to the first candidate."""
        target = next((c for c in candidates if self.is_plan_node(c)), None)
        return ManagerDecision(
            trigger="conflict",
            stage=stage,
            action="reroute" if target else "pause",
            target_stage=target,
            rationale=context or "conflicting signals — picked the first plan-legal candidate",
            confidence=0.5,
            plan_digest=self.digest,
        )

    def escalate(self, *, stage: str, question: str) -> ManagerDecision:
        """Hand an undecidable question to a human (IM bridge consumes this)."""
        return ManagerDecision(
            trigger="escalate",
            stage=stage,
            action="request_human",
            target_stage=None,
            rationale=question,
            confidence=0.0,
            plan_digest=self.digest,
        )

    async def advise(
        self,
        *,
        stage: str,
        question: str,
        brief: str = "",
        options: list[str] | None = None,
    ) -> ManagerDecision | None:
        """升级链第一级：Manager 先尝试把 agent 的问题裁决掉。

        返回 ``None`` 表示 Manager 也判断不了（无 LLM / 无把握 / 问题需要人类授权），
        调用方据此继续升级真人。任何异常都按"裁决不了"处理，绝不阻塞任务。
        """
        if not self.use_llm:
            return None
        client = self._client
        if client is None:
            try:
                from sortie_deck.llm import LlmClient

                client = LlmClient()
            except Exception:  # noqa: BLE001 — 无客户端等价于裁决不了
                return None
        if not getattr(client, "enabled", False):
            return None
        user = json.dumps(
            {
                "stage": stage,
                "role": self.role_of(stage),
                "question": question,
                "options": options or [],
                "brief": (brief or "")[:2000],
            },
            ensure_ascii=False,
        )
        try:
            data = await client.chat_json(system=_ADVISE_SYSTEM, user=user)
        except Exception:  # noqa: BLE001 — 裁决失败就交给真人
            return None
        if not isinstance(data, dict):
            return None
        resolution = str(data.get("resolution") or "").strip()
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        # 低置信度不放行：宁可升真人，也不要 Manager 编一个答案
        if not resolution or confidence < 0.6:
            return None
        return ManagerDecision(
            trigger="conflict",
            stage=stage,
            action="continue",
            target_stage=None,
            rationale=resolution,
            resolution=resolution,
            confidence=confidence,
            plan_digest=self.digest,
        )

    def completion_note(self, *, stage: str, status: str, role: str = "") -> str:
        """Manager-side 'step finished' annotation (async record, never blocking)."""
        who = role_key(role) if role else self.role_of(stage)
        suffix = "" if status in {"ok", "success"} else f" ({status})"
        return f"manager: step `{stage}`{f' [{who}]' if who else ''} marked complete{suffix}"

    # --- llm -----------------------------------------------------------

    async def _llm_join(
        self,
        *,
        group: str,
        last_member: str,
        branches: list[BranchStatus],
        candidates: list[str],
        brief: str,
        upstream_keys: list[str],
    ) -> str | None:
        if not self.use_llm:
            return None
        client = self._client
        if client is None:
            try:
                from sortie_deck.llm import LlmClient

                client = LlmClient()
            except Exception:  # noqa: BLE001 — no client is a valid state
                return None
        if not getattr(client, "enabled", False):
            return None
        user = json.dumps(
            {
                "parallel_group": group,
                "last_member": last_member,
                "branches": [b.model_dump(mode="json") for b in branches],
                "allowed_candidates": candidates,
                "brief": (brief or "")[:1200],
                "upstream_artifact_keys": upstream_keys[:20],
            },
            ensure_ascii=False,
        )
        try:
            data = await client.chat_json(system=_JOIN_SYSTEM, user=user)
        except Exception:  # noqa: BLE001 — fall back to the deterministic rule
            return None
        if not isinstance(data, dict):
            return None
        target = str(data.get("target_stage") or "").strip()
        return target or None
