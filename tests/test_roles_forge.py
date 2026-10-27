from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck import roles as roles_module
from sortie_deck.agent_forge import AgentForge, ForgeRequest, ForgeStore
from sortie_deck.agents import AgentCatalog
from sortie_deck.models import RoleId, role_key
from sortie_deck.roles import RoleRegistry, RoleSpec, normalize_role_key


class _FakeLlm:
    backend = "fake"

    def __init__(self, payload: dict[str, Any] | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._payload = payload or {}

    async def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        return dict(self._payload)


@pytest.fixture()
def registry(tmp_path: Path) -> RoleRegistry:
    return RoleRegistry(tmp_path / "roles.json")


@pytest.fixture()
def catalog(tmp_path: Path) -> AgentCatalog:
    return AgentCatalog(tmp_path / "agents.json")


# --- role key normalisation ----------------------------------------------


def test_normalize_role_key_slugifies_ascii() -> None:
    assert normalize_role_key("Stocks Research") == "stocks_research"
    assert normalize_role_key("eng-ios") == "eng_ios"
    assert normalize_role_key("  ") == ""


def test_normalize_role_key_is_stable_for_non_ascii() -> None:
    first = normalize_role_key("育儿")
    assert first.startswith("role_")
    assert first == normalize_role_key("育儿")


# --- open role set --------------------------------------------------------


def test_role_id_materialises_unknown_keys() -> None:
    custom = RoleId("parenting")
    assert custom.value == "parenting"
    assert role_key(custom) == "parenting"
    # built-ins are untouched
    assert RoleId("qa") is RoleId.QA
    with pytest.raises(ValueError):
        RoleId("")


def test_registry_lists_builtin_and_persona_roles(registry: RoleRegistry) -> None:
    keys = registry.keys()
    for expected in ("product", "design", "eng_web", "qa", "deploy"):
        assert expected in keys, expected
    spec = registry.get("qa")
    assert spec.coding is False
    assert registry.get("eng_web").coding is True


def test_registry_unknown_role_is_not_known(registry: RoleRegistry) -> None:
    assert registry.is_known("parenting") is False
    assert registry.maybe("parenting") is None


def test_registry_register_and_get_custom_role(registry: RoleRegistry) -> None:
    spec = registry.register(
        RoleSpec(role_key="parenting", title="育儿顾问", tags=["family"], source="custom")
    )
    assert spec.role_key == "parenting"
    assert registry.is_known("parenting") is True
    assert registry.get("parenting").title == "育儿顾问"
    # cache must be invalidated by the write
    assert registry.get("parenting").tags == ["family"]


def test_registry_register_normalises_role_key(registry: RoleRegistry) -> None:
    spec = registry.register(RoleSpec(role_key="Stocks Research", title="Stock Analyst"))
    assert spec.role_key == "stocks_research"


def test_registry_refuses_to_shadow_builtin_without_overwrite(registry: RoleRegistry) -> None:
    with pytest.raises(ValueError):
        registry.register(RoleSpec(role_key="qa", title="Shadow QA"))
    shadow = registry.register(RoleSpec(role_key="qa", title="Shadow QA"), overwrite=True)
    assert shadow.title == "Shadow QA"


def test_registry_delete_custom_role(registry: RoleRegistry) -> None:
    registry.register(RoleSpec(role_key="parenting", title="育儿顾问"))
    registry.delete("parenting")
    assert registry.is_known("parenting") is False
    with pytest.raises(KeyError):
        registry.delete("parenting")


def test_registry_executor_hint_and_coding(registry: RoleRegistry) -> None:
    registry.register(RoleSpec(role_key="stocks", title="Stock Analyst", executor="mock_intake"))
    assert registry.executor_hint("stocks") == "mock_intake"
    assert registry.is_coding("stocks") is False
    assert registry.is_coding("eng_backend") is True


def test_registry_find_similar_scores_exact_key_highest(registry: RoleRegistry) -> None:
    hits = registry.find_similar(title="qa engineer")
    assert hits, "expected at least one similar role"
    assert hits[0][0].role_key == "qa"
    assert hits[0][1] == 1.0


def test_registry_find_similar_flags_near_duplicates(registry: RoleRegistry) -> None:
    registry.register(RoleSpec(role_key="parenting", title="育儿顾问", summary="婴幼儿养育建议"))
    hits = registry.find_similar(title="育儿顾问", summary="婴幼儿养育建议")
    keys = [spec.role_key for spec, _ in hits]
    assert "parenting" in keys


def test_registry_handoff_targets_come_from_persona_contract(registry: RoleRegistry) -> None:
    assert "eng" in registry.handoff_targets("qa")
    assert registry.handoff_targets("deploy") == []


# --- forge ---------------------------------------------------------------


def _forge(registry: RoleRegistry, catalog: AgentCatalog, llm: Any | None = None) -> AgentForge:
    return AgentForge(
        store=ForgeStore(registry.path.parent / "forge.json"),
        roles=registry,
        agents=catalog,
        llm=llm,
    )


async def test_forge_propose_without_llm_uses_template(registry: RoleRegistry, catalog: AgentCatalog) -> None:
    forge = _forge(registry, catalog, _FakeLlm(enabled=False))
    draft = await forge.propose(ForgeRequest(capability="育儿建议", title="育儿顾问"))
    assert draft.status == "pending-review"
    assert draft.generator == "template"
    assert draft.role_key
    assert draft.contract.outputs
    assert draft.issues == []


async def test_forge_propose_with_llm_uses_model_payload(registry: RoleRegistry, catalog: AgentCatalog) -> None:
    payload = {
        "role_key": "stocks",
        "title": "Stock Analyst",
        "summary": "Tracks a watchlist and writes a daily note",
        "persona": "You are a stock analyst.",
        "inputs": ["watchlist.json"],
        "outputs": ["daily_note.md"],
        "handoff_targets": ["eng"],
        "tags": ["finance"],
    }
    forge = _forge(registry, catalog, _FakeLlm(payload))
    draft = await forge.propose(ForgeRequest(capability="股票分析"))
    assert draft.generator == "llm"
    assert draft.role_key == "stocks"
    assert draft.contract.outputs == ["daily_note.md"]
    assert draft.contract.handoff_targets == ["eng"]


async def test_forge_flags_unknown_handoff_targets(registry: RoleRegistry, catalog: AgentCatalog) -> None:
    payload = {"title": "Ghost", "outputs": ["x.md"], "handoff_targets": ["nonexistent_role"]}
    forge = _forge(registry, catalog, _FakeLlm(payload))
    draft = await forge.propose(ForgeRequest(capability="ghost"))
    assert any("not in role catalog" in issue for issue in draft.issues)


async def test_forge_assigns_unique_role_key_on_collision(
    registry: RoleRegistry, catalog: AgentCatalog
) -> None:
    registry.register(RoleSpec(role_key="stocks", title="Existing"))
    payload = {"role_key": "stocks", "title": "Stocks 2", "outputs": ["x.md"]}
    forge = _forge(registry, catalog, _FakeLlm(payload))
    draft = await forge.propose(ForgeRequest(capability="stocks"))
    assert draft.role_key == "stocks_2"


async def test_forge_update_then_approve_registers_role(
    registry: RoleRegistry, catalog: AgentCatalog
) -> None:
    forge = _forge(registry, catalog, _FakeLlm(enabled=False))
    draft = await forge.propose(ForgeRequest(capability="育儿建议", title="育儿顾问"))
    edited = forge.update(draft.id, {"summary": "edited by human"})
    assert edited.summary == "edited by human"
    approved, role_spec, published = forge.approve(draft.id, reviewer="ops")
    assert approved.status == "approved"
    assert role_spec is not None
    assert registry.is_known(approved.role_key) is True
    assert published is None


async def test_forge_approve_can_publish_agent(registry: RoleRegistry, catalog: AgentCatalog) -> None:
    forge = _forge(registry, catalog, _FakeLlm(enabled=False))
    draft = await forge.propose(ForgeRequest(capability="育儿建议", title="育儿顾问"))
    _, _, published = forge.approve(draft.id, reviewer="ops", publish_agent=True)
    assert published is not None
    assert published.published is True
    assert catalog.list(slot=draft.role_key)


async def test_forge_reject_blocks_approval(registry: RoleRegistry, catalog: AgentCatalog) -> None:
    forge = _forge(registry, catalog, _FakeLlm(enabled=False))
    draft = await forge.propose(ForgeRequest(capability="育儿建议"))
    forge.reject(draft.id, note="duplicate of qa", reviewer="ops")
    with pytest.raises(ValueError):
        forge.approve(draft.id)


async def test_forge_store_lists_pending_review_only(
    registry: RoleRegistry, catalog: AgentCatalog
) -> None:
    forge = _forge(registry, catalog, _FakeLlm(enabled=False))
    first = await forge.propose(ForgeRequest(capability="育儿建议", title="育儿顾问"))
    await forge.propose(ForgeRequest(capability="股票分析", title="股票分析师"))
    forge.reject(first.id)
    pending = forge.list(status="pending-review")
    assert len(pending) == 1
    assert pending[0].title == "股票分析师"


# --- 结构化人设（六段式） ------------------------------------------------


_STRUCTURED_PERSONA = {
    "role": "你是拥有 10 年经验的资深股票分析师，擅长把行情数据转成可执行观察笔记。",
    "background": "- 服务内部投研小队，上游是自选股清单，下游是投资决策群。\n- 每日一篇观察笔记，必须给依据。",
    "skills": ["行情解读：识别量价异动。", "风险识别：标注回撤与流动性风险。", "组合复盘：对照目标仓位。"],
    "workflow": ["读自选股清单与昨日笔记。", "生成当日观察结论。", "标注风险与待确认项。"],
    "examples": [
        {"situation": "自选股无重大变化", "response": "输出一句话结论并标注「无操作建议」。"}
    ],
    "constraints": ["不给出直接投资建议。", "数据缺失时标注缺失而非补零。", "结论必须附数据依据。"],
}


async def test_forge_renders_structured_persona_spec(
    registry: RoleRegistry, catalog: AgentCatalog
) -> None:
    payload = {
        "role_key": "stocks",
        "title": "Stock Analyst",
        "persona": _STRUCTURED_PERSONA,
        "outputs": ["daily_note.md"],
        "handoff_targets": ["eng"],
    }
    forge = _forge(registry, catalog, _FakeLlm(payload))
    draft = await forge.propose(ForgeRequest(capability="股票分析"))
    assert draft.generator == "llm"
    assert draft.persona_spec is not None
    for heading in ("## 角色", "## 背景", "## 技能", "## 工作流", "## 示例", "## 约束"):
        assert heading in draft.persona
    assert draft.issues == []


async def test_forge_flags_thin_persona_quality(
    registry: RoleRegistry, catalog: AgentCatalog
) -> None:
    payload = {
        "role_key": "stocks",
        "title": "Stock Analyst",
        "persona": {"role": "股票分析师", "skills": ["看盘"]},
        "outputs": ["daily_note.md"],
    }
    forge = _forge(registry, catalog, _FakeLlm(payload))
    draft = await forge.propose(ForgeRequest(capability="股票分析"))
    assert any("10 年经验" in issue for issue in draft.issues)
    assert any("示例" in issue for issue in draft.issues)
    assert any("约束" in issue for issue in draft.issues)


async def test_forge_update_persona_spec_recompiles_text(
    registry: RoleRegistry, catalog: AgentCatalog
) -> None:
    forge = _forge(registry, catalog, _FakeLlm(enabled=False))
    draft = await forge.propose(ForgeRequest(capability="育儿建议", title="育儿顾问"))
    edited = forge.update(draft.id, {"persona_spec": _STRUCTURED_PERSONA})
    assert edited.persona.startswith("## 角色")
    assert edited.persona_spec is not None
    assert edited.issues == []


def test_catalog_create_compiles_persona_spec(catalog: AgentCatalog) -> None:
    from sortie_deck.agents import CreateAgentRequest
    from sortie_deck.persona import PersonaSpec

    spec = PersonaSpec.model_validate(_STRUCTURED_PERSONA)
    agent = catalog.create(
        CreateAgentRequest(title="Stock Analyst", slot="product", persona_spec=spec),
        author_id="u1",
        author_name="Tester",
    )
    assert agent.persona.startswith("## 角色")
    assert agent.persona_spec is not None
    # 旧接口：只传自由文本仍然可用，spec 留空
    raw = catalog.create(
        CreateAgentRequest(title="Legacy", slot="product", persona="You are a legacy agent."),
        author_id="u1",
        author_name="Tester",
    )
    assert raw.persona == "You are a legacy agent."
    assert raw.persona_spec is None


# --- loadout / collaboration --------------------------------------------


def test_valid_loadout_slots_comes_from_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from sortie_deck import pipeline_llm

    temp = RoleRegistry(tmp_path / "roles.json")
    temp.register(RoleSpec(role_key="parenting", title="育儿顾问"))
    monkeypatch.setattr(roles_module, "get_role_registry", lambda: temp)
    allowed = pipeline_llm.valid_loadout_slots()
    assert "parenting" in allowed
    assert "qa" in allowed


async def test_assess_collaboration_falls_back_to_multi_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sortie_deck import pipeline_llm

    temp = RoleRegistry(tmp_path / "roles.json")
    monkeypatch.setattr(roles_module, "get_role_registry", lambda: temp)
    result = await pipeline_llm.assess_collaboration(
        title="加个健康检查", brief="给门户加一个预发健康检查", use_llm=False
    )
    assert result["multi_agent"] is True
    assert "product" in result["roles"]
    assert result["planner"] == "heuristic"


async def test_assess_collaboration_honours_single_agent_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sortie_deck import pipeline_llm

    temp = RoleRegistry(tmp_path / "roles.json")
    monkeypatch.setattr(roles_module, "get_role_registry", lambda: temp)
    llm = _FakeLlm({"multi_agent": False, "roles": ["eng"], "rationale": "one small fix"})
    result = await pipeline_llm.assess_collaboration(title="typo", brief="fix a typo", client=llm)
    assert result["multi_agent"] is False
    assert result["roles"] == ["eng"]


async def test_assess_collaboration_drops_unknown_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sortie_deck import pipeline_llm

    temp = RoleRegistry(tmp_path / "roles.json")
    monkeypatch.setattr(roles_module, "get_role_registry", lambda: temp)
    llm = _FakeLlm({"multi_agent": True, "roles": ["not_a_role"], "rationale": "x"})
    result = await pipeline_llm.assess_collaboration(title="t", brief="b", client=llm)
    assert "not_a_role" not in result["roles"]
