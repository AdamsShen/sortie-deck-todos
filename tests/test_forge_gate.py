"""建队时自主 forge：missing_slots 校验、编制合并、出击审核门禁。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck import roles as roles_module
from sortie_deck.agent_contract import AgentContract
from sortie_deck.agent_forge import ForgedAgentDraft, ForgeStore
from sortie_deck.models import (
    CreateInitiativeRequest,
    ForgeReviewRequiredError,
    PipelineNotConfirmedError,
)
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.pipeline_llm import clean_missing_slots, propose_loadout_llm
from sortie_deck.roles import RoleRegistry
from sortie_deck.settings import Settings


class _FakeLlm:
    backend = "fake"
    enabled = True

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        return dict(self._payload)


@pytest.fixture()
def registry(tmp_path: Path, monkeypatch) -> RoleRegistry:
    reg = RoleRegistry(tmp_path / "roles.json")
    monkeypatch.setattr(roles_module, "get_role_registry", lambda: reg)
    return reg


# --- missing_slots 校验 ------------------------------------------------------


def test_clean_missing_slots_drops_catalog_roles(registry: RoleRegistry) -> None:
    out = clean_missing_slots(
        [{"role_key": "qa", "title": "QA"}, {"role_key": "pet_funeral", "title": "宠物殡葬策划"}],
        set(registry.known_role_keys()),
    )
    assert [row["role_key"] for row in out] == ["pet_funeral"]
    assert out[0]["title"] == "宠物殡葬策划"


def test_clean_missing_slots_caps_and_dedupes(registry: RoleRegistry) -> None:
    out = clean_missing_slots(
        [
            {"role_key": "pet_funeral"},
            {"role_key": "pet_funeral"},
            {"role_key": "wedding_planner"},
            {"role_key": "third_role"},
        ],
        set(registry.known_role_keys()),
    )
    assert [row["role_key"] for row in out] == ["pet_funeral", "wedding_planner"]


def test_clean_missing_slots_tolerates_strings(registry: RoleRegistry) -> None:
    out = clean_missing_slots(["pet_funeral"], set(registry.known_role_keys()))
    assert out and out[0]["role_key"] == "pet_funeral"


async def test_propose_loadout_llm_returns_missing_slots(registry: RoleRegistry) -> None:
    client = _FakeLlm(
        {
            "needed_slots": ["product", "qa", "deploy"],
            "missing_slots": [{"role_key": "pet_funeral", "title": "宠物殡葬策划"}],
            "rationale": "需要行业专家",
        }
    )
    out = await propose_loadout_llm(brief="宠物殡葬服务", stages=[], client=client)  # type: ignore[arg-type]
    assert out is not None
    assert out["needed_slots"] == ["product", "qa", "deploy"]
    assert out["missing_slots"][0]["role_key"] == "pet_funeral"


async def test_propose_loadout_llm_refuses_pure_invention_without_known_slots(
    registry: RoleRegistry,
) -> None:
    # 只给了 missing_slots、没有目录内角色：仍然成立（缺角色本身就是编制结论）
    client = _FakeLlm({"needed_slots": [], "missing_slots": [{"role_key": "pet_funeral"}]})
    out = await propose_loadout_llm(brief="x", stages=[], client=client)  # type: ignore[arg-type]
    assert out is not None and out["needed_slots"] == []
    assert out["missing_slots"][0]["role_key"] == "pet_funeral"


# --- 出击审核门禁 ------------------------------------------------------------


def _seed_draft(data_dir: Path, status: str = "pending-review") -> str:
    store = ForgeStore(data_dir / "forge.json")
    draft = ForgedAgentDraft(
        role_key="pet_funeral",
        title="宠物殡葬策划",
        summary="行业角色",
        persona="You plan pet funeral services.",
        contract=AgentContract(
            role_key="pet_funeral", title="宠物殡葬策划", outputs=["pet_funeral_plan.md"]
        ),
        status=status,  # type: ignore[arg-type]
    )
    return store.save(draft).id


async def test_start_pipeline_blocks_pending_forge_draft(tmp_path: Path) -> None:
    cfg = Settings(
        data_dir=tmp_path / "data", deploy_executor="mock", pipeline_llm=False, room_llm=False
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="宠物殡葬策划",
                brief="为宠物殡葬服务做一版上线方案",
                coding_executor="mock",
                harness="mock",
                creator_name="Ada",
            )
        )
        ini.meta["pipeline_confirmed"] = True
        draft_id = _seed_draft(cfg.data_dir)
        ini.meta["forge_drafts"] = [draft_id]
        orch.store.upsert(ini)

        with pytest.raises(ForgeReviewRequiredError) as err:
            await orch.start_pipeline(ini.id)
        assert err.value.code == "forge_review_required"
        assert err.value.draft_ids == [draft_id]

        # 审核通过后门禁放行（不再抛 forge_review_required）
        store = ForgeStore(cfg.data_dir / "forge.json")
        approved = store.get(draft_id)
        approved.status = "approved"
        store.save(approved)
        ini = orch.get(ini.id)
        assert ini is not None
        assert orch._pending_forge_drafts(ini) == []
    finally:
        await orch.shutdown()


async def test_start_pipeline_still_requires_confirmation_first(tmp_path: Path) -> None:
    cfg = Settings(
        data_dir=tmp_path / "data", deploy_executor="mock", pipeline_llm=False, room_llm=False
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(title="x", brief="y", coding_executor="mock", harness="mock")
        )
        with pytest.raises(PipelineNotConfirmedError):
            await orch.start_pipeline(ini.id)
    finally:
        await orch.shutdown()
