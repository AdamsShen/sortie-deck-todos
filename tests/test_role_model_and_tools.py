from __future__ import annotations

import asyncio

from sortie_deck.agent_contract import AgentContract
from sortie_deck.agents import AgentCatalog, CreateAgentRequest
from sortie_deck.models import (
    DEFAULT_ROLE_MODEL,
    CreateInitiativeRequest,
    RoleAgent,
    RoleId,
    StageContext,
    filter_toolkit_by_contract,
)
from sortie_deck.orchestrator import Orchestrator, bind_catalogs
from sortie_deck.settings import Settings
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitStore

# --- 默认值与字段缺失时的兼容 ------------------------------------------------


def test_default_role_model_constant() -> None:
    assert DEFAULT_ROLE_MODEL == "deepseek/deepseek-v4-flash-20260731"


def test_role_agent_defaults_to_default_model() -> None:
    role = RoleAgent(id="r1", role=RoleId.ENG, title="Eng", persona="p", executor="mock_eng")
    assert role.model == DEFAULT_ROLE_MODEL


def test_stage_context_defaults_have_no_allowlist() -> None:
    ctx = StageContext(
        initiative_id="i",
        thread_id="t",
        stage="eng_implement",
        role=RoleId.ENG,
        brief="b",
        artifact_dir="/tmp/artifacts",
    )
    # 空值表示沿用 provider / 旧行为，保证既有任务零回归
    assert ctx.model == ""
    assert ctx.tools_allowed == []


# --- 契约白名单过滤（纯函数） ------------------------------------------------


def test_filter_toolkit_empty_allowlist_is_noop() -> None:
    assert filter_toolkit_by_contract(["a", "b"], []) == ["a", "b"]


def test_filter_toolkit_keeps_only_allowed() -> None:
    assert filter_toolkit_by_contract(["a", "b", "c"], ["b", " c "]) == ["b", "c"]


def test_filter_toolkit_drops_everything_outside_allowlist() -> None:
    assert filter_toolkit_by_contract(["a", "b"], ["z"]) == []


# --- 已发布角色的模型字段 ----------------------------------------------------


def test_published_agent_defaults_model(tmp_path) -> None:
    catalog = AgentCatalog(tmp_path / "agents.json")
    agent = catalog.create(
        CreateAgentRequest(title="Eng", slot="eng", persona="You are eng."),
        author_id="u1",
        author_name="Ada",
    )
    assert agent.model == DEFAULT_ROLE_MODEL


def test_published_agent_accepts_explicit_model(tmp_path) -> None:
    catalog = AgentCatalog(tmp_path / "agents.json")
    agent = catalog.create(
        CreateAgentRequest(title="QA", slot="qa", persona="You are qa.", model="x/y"),
        author_id="u1",
        author_name="Ada",
    )
    assert agent.model == "x/y"


def test_published_agent_persists_tools_allowed(tmp_path) -> None:
    catalog = AgentCatalog(tmp_path / "agents.json")
    agent = catalog.create(
        CreateAgentRequest(
            title="QA",
            slot="qa",
            persona="You are qa.",
            tools_allowed=["t1", " t2 ", ""],
        ),
        author_id="u1",
        author_name="Ada",
    )
    assert agent.tools_allowed == ["t1", "t2"]
    # 重新读取（走 JSON 反序列化）仍保留
    assert catalog.get(agent.id).tools_allowed == ["t1", "t2"]


# --- 编排层接线：白名单收敛 + 模型进 loadout ---------------------------------


def test_role_contract_filters_toolkits_and_exposes_model(tmp_path, monkeypatch) -> None:
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    toolkit = ToolkitStore(tmp_path / "toolkit.json")
    t1 = toolkit.create(
        CreateToolkitRequest(kind="tool", name="gh-cli", summary="GitHub CLI", endpoint="gh"),
        author_id="u1",
        author_name="Ada",
    )
    t2 = toolkit.create(
        CreateToolkitRequest(kind="skill", name="review-skill", summary="Review", body="# R"),
        author_id="u1",
        author_name="Ada",
    )
    catalog = AgentCatalog(tmp_path / "agents.json")
    bind_catalogs(agents=catalog, toolkit=toolkit)

    # eng 的契约只允许 t1；其余角色保持空白名单（不限制）
    def _fake_contract(role: str) -> AgentContract:
        if role == "eng":
            return AgentContract(role_key="eng", tools_allowed=[t1.id])
        return AgentContract(role_key=role)

    monkeypatch.setattr("sortie_deck.orchestrator.load_agent_contract", _fake_contract)

    orch = Orchestrator(cfg)

    async def _run() -> None:
        await orch.startup()
        try:
            ini = await orch.create_initiative(
                CreateInitiativeRequest(
                    title="Allowlist",
                    brief="only t1 may load",
                    role_toolkits={"eng": [t1.id, t2.id]},
                    creator_name="Ada",
                )
            )
            eng = next(r for r in ini.roles if r.role.value == "eng")
            assert eng.toolkit_ids == [t1.id]
            assert eng.model == DEFAULT_ROLE_MODEL

            loadout = orch._pipeline_loadout(ini)
            assert loadout["role_toolkits"]["eng"] == [t1.id]
            assert loadout["role_models"]["eng"] == DEFAULT_ROLE_MODEL
        finally:
            await orch.shutdown()

    asyncio.run(_run())


def test_role_level_tools_allowed_overrides_contract(tmp_path, monkeypatch) -> None:
    """工坊配置的白名单优先于 persona front-matter 契约。"""
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    toolkit = ToolkitStore(tmp_path / "toolkit.json")
    t1 = toolkit.create(
        CreateToolkitRequest(kind="tool", name="gh-cli", summary="GitHub CLI", endpoint="gh"),
        author_id="u1",
        author_name="Ada",
    )
    t2 = toolkit.create(
        CreateToolkitRequest(kind="skill", name="review-skill", summary="Review", body="# R"),
        author_id="u1",
        author_name="Ada",
    )
    catalog = AgentCatalog(tmp_path / "agents.json")
    agent = catalog.create(
        CreateAgentRequest(
            title="Eng",
            slot="eng",
            persona="You are eng.",
            toolkit_ids=[t1.id, t2.id],
            tools_allowed=[t1.id],
        ),
        author_id="u1",
        author_name="Ada",
    )
    bind_catalogs(agents=catalog, toolkit=toolkit)

    # 契约只允许 t2；角色级配置（t1）应胜出
    def _fake_contract(role: str) -> AgentContract:
        return AgentContract(role_key=role, tools_allowed=[t2.id])

    monkeypatch.setattr("sortie_deck.orchestrator.load_agent_contract", _fake_contract)

    orch = Orchestrator(cfg)

    async def _run() -> None:
        await orch.startup()
        try:
            ini = await orch.create_initiative(
                CreateInitiativeRequest(
                    title="Role allowlist wins",
                    brief="role config beats contract",
                    role_agents={"eng": agent.id},
                    creator_name="Ada",
                )
            )
            eng = next(r for r in ini.roles if r.role.value == "eng")
            assert eng.tools_allowed == [t1.id]
            assert eng.toolkit_ids == [t1.id]
            loadout = orch._pipeline_loadout(ini)
            assert loadout["role_tools_allowed"]["eng"] == [t1.id]
        finally:
            await orch.shutdown()

    asyncio.run(_run())
