from __future__ import annotations

import pytest

from sortie_deck.agent_contract import (
    AgentContract,
    ContractError,
    contract_from_mapping,
    handoff_allowed,
    split_front_matter,
    validate_handoff,
)
from sortie_deck.contracts import validate_stage_handoff
from sortie_deck.templates import (
    list_persona_roles,
    load_agent_contract,
    load_persona,
    load_persona_document,
)

# --- front-matter parsing -------------------------------------------------


def test_split_front_matter_without_block_returns_document() -> None:
    front, body = split_front_matter("You are qa.")
    assert front == {}
    assert body == "You are qa."


def test_split_front_matter_parses_block_and_strips_body() -> None:
    text = "---\nrole_key: qa\ntitle: QA\n---\nYou are qa.\n"
    front, body = split_front_matter(text)
    assert front == {"role_key": "qa", "title": "QA"}
    assert body == "You are qa."


def test_split_front_matter_unterminated_block_is_body() -> None:
    text = "---\nrole_key: qa\nYou are qa.\n"
    front, body = split_front_matter(text)
    assert front == {}
    assert body == text


def test_split_front_matter_broken_yaml_is_tolerated() -> None:
    text = "---\nrole_key: [unclosed\n---\nBody\n"
    front, body = split_front_matter(text)
    assert front == {}
    assert body == "Body"


def test_split_front_matter_empty_text() -> None:
    assert split_front_matter("") == ({}, "")


# --- contract construction ------------------------------------------------


def test_contract_from_mapping_ignores_unknown_keys() -> None:
    contract = contract_from_mapping(
        {"role_key": "qa", "outputs": ["test_cases.md"], "random_key": 1},
        role_key="qa",
    )
    assert contract is not None
    assert contract.outputs == ["test_cases.md"]


def test_contract_from_mapping_accepts_nested_contract_block() -> None:
    contract = contract_from_mapping(
        {"contract": {"role_key": "qa", "handoff_targets": ["eng"]}},
        role_key="qa",
    )
    assert contract is not None
    assert contract.handoff_targets == ["eng"]


def test_contract_from_mapping_none_when_no_contract_shape() -> None:
    assert contract_from_mapping({"some": "meta"}, role_key="qa") is None
    assert contract_from_mapping(None, role_key="qa") is None


def test_contract_defaults_role_key_from_persona_stem() -> None:
    contract = contract_from_mapping({"outputs": ["x.md"]}, role_key="parenting")
    assert contract is not None
    assert contract.role_key == "parenting"
    assert contract.on_fail == "escalate"


# --- handoff whitelist ----------------------------------------------------


def test_validate_handoff_allows_whitelisted_target() -> None:
    src = AgentContract(role_key="qa", handoff_targets=["eng", "deploy"])
    validate_handoff(src, "eng")  # no raise


def test_validate_handoff_rejects_unknown_target() -> None:
    src = AgentContract(role_key="qa", handoff_targets=["eng"])
    with pytest.raises(ContractError) as exc:
        validate_handoff(src, "design")
    assert "may not hand off to `design`" in str(exc.value)


def test_validate_handoff_rejects_missing_contract() -> None:
    with pytest.raises(ContractError):
        validate_handoff(None, "eng")


def test_validate_handoff_empty_targets_denies_all() -> None:
    src = AgentContract(role_key="deploy", handoff_targets=[])
    assert handoff_allowed(src, "eng") is False
    with pytest.raises(ContractError):
        validate_handoff(src, "eng")


def test_handoff_allowed_blank_target_is_false() -> None:
    src = AgentContract(role_key="qa", handoff_targets=["eng"])
    assert handoff_allowed(src, "  ") is False


def test_validate_stage_handoff_wraps_stage_context() -> None:
    src = AgentContract(role_key="qa", handoff_targets=["eng"])
    with pytest.raises(ContractError) as exc:
        validate_stage_handoff(src, "qa_cases", "design")
    message = str(exc.value)
    assert "stage `qa_cases`" in message
    assert "role `qa`" in message


# --- self check -----------------------------------------------------------


def test_contract_problems_flags_self_handoff_and_empty_outputs() -> None:
    contract = AgentContract(
        role_key="qa",
        outputs=[],
        handoff_targets=["qa", "eng", "eng"],
    )
    issues = contract.problems()
    assert any("outputs is empty" in i for i in issues)
    assert any("must not contain the role itself" in i for i in issues)
    assert any("duplicate handoff_targets" in i for i in issues)


def test_contract_problems_clean_for_valid_contract() -> None:
    contract = AgentContract(
        role_key="qa",
        outputs=["test_cases.md"],
        handoff_targets=["eng", "deploy"],
    )
    assert contract.problems() == []


def test_contract_brief_lists_targets() -> None:
    contract = AgentContract(role_key="qa", outputs=["x.md"], handoff_targets=["eng"])
    brief = contract.brief()
    assert "`qa`" in brief
    assert "eng" in brief


# --- shipped personas -----------------------------------------------------


def test_every_shipped_persona_declares_a_valid_contract() -> None:
    roles = list_persona_roles()
    assert roles, "personas directory should not be empty"
    for role in roles:
        contract = load_agent_contract(role)
        assert contract is not None, f"`{role}` persona is missing a contract front-matter block"
        assert contract.role_key == role
        assert contract.problems() == [], f"`{role}` contract problems: {contract.problems()}"


def test_load_persona_strips_front_matter() -> None:
    body = load_persona("qa")
    assert body
    assert "role_key" not in body
    assert "---" not in body.splitlines()[0]


def test_load_persona_document_returns_contract_and_body() -> None:
    contract, body = load_persona_document("deploy")
    assert contract is not None
    assert contract.handoff_targets == []
    assert body.startswith("You are a release engineer")


def test_load_persona_falls_back_for_unknown_role() -> None:
    contract, body = load_persona_document("no_such_role")
    assert contract is None
    assert body == ""
    assert load_persona("no_such_role") == "You are the no_such_role role agent."
