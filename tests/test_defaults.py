from __future__ import annotations

from sortie_deck.settings import Settings

# The collaboration capabilities ship **enabled**: each one degrades safely when
# its dependency is missing (no LLM → rule fallback, no IM channel → decision
# centre only). This pins the *field defaults* — environment variables and
# `.env` may still override them at runtime.
COLLABORATION_SWITCHES = (
    "plan_digest_check",
    "subroom_enabled",
    "loadout_auto",
    "mcp_call_enabled",
    "skill_run_enabled",
    "im_escalation",
)

# Routing is *not* switchable: agent hand-off, manager join decisions and HITL
# verdicts are all graph node jumps through one conditional-edge router.
NON_SWITCHABLE = ("agent_handoff", "manager_join_decision")


def _default(name: str):
    return Settings.model_fields[name].default


def test_collaboration_capabilities_default_to_enabled() -> None:
    for name in COLLABORATION_SWITCHES:
        assert _default(name) is True, f"`{name}` must default to enabled"


def test_collaboration_budgets_have_positive_defaults() -> None:
    assert _default("guardrail_max_tokens") > 0
    assert _default("guardrail_max_seconds") > 0
    assert _default("guardrail_max_subrooms") > 0
    assert _default("guardrail_max_handoffs") > 0


def test_routing_is_not_switchable() -> None:
    """Agent switching is a graph node jump — there is no legacy flag for it."""
    for name in NON_SWITCHABLE:
        assert name not in Settings.model_fields, (
            f"`{name}` was a legacy routing switch and must stay removed"
        )


def test_runtime_instance_respects_overrides() -> None:
    cfg = Settings(
        plan_digest_check=False,
        subroom_enabled=False,
        loadout_auto=False,
        mcp_call_enabled=False,
        skill_run_enabled=False,
        im_escalation=False,
    )
    for name in COLLABORATION_SWITCHES:
        assert getattr(cfg, name) is False
