"""全局子代理池：派生规则、能力映射、只读契约与 claude 侧渲染。"""

from __future__ import annotations

from typing import Any

from sortie_deck.models import RoleId, StageContext
from sortie_deck.subagents import resolve_subagents, tool_names


def _ctx(tmp_path: Any, *, stage: str = "eng_backend", role: str = "eng_backend", **kw: Any) -> StageContext:
    return StageContext(
        initiative_id="ini_sub",
        thread_id="thr_sub",
        stage=stage,
        role=RoleId(role),
        brief="给会话页加已读状态",
        artifact_dir="",
        harness=kw.pop("harness", "claude_code"),
        data_dir=str(tmp_path / "data"),
        worktrees_dir=str(tmp_path / "data/missions"),
        **kw,
    )


def test_default_pool_is_scout_only(tmp_path, monkeypatch):
    """开关打开后默认只供给只读 scout，不出现任何业务角色。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)
    monkeypatch.setattr(settings, "subagent_max_turns", 9)

    plan = resolve_subagents(_ctx(tmp_path))

    assert [spec.id for spec in plan.agents] == ["scout"]
    scout = plan.agents[0]
    assert scout.mode == "readonly"
    assert scout.max_turns == 9
    assert set(scout.capabilities) <= {"read_files", "search_code", "web"}
    assert plan.parallel is True
    assert "scout" in plan.guidance
    assert "委派判据" in plan.guidance


def test_disabled_switch_is_zero_behavior(tmp_path, monkeypatch):
    """总开关关闭：空池 + 空指引（prompt 与 provider 都不会看到任何子代理内容）。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", False)

    plan = resolve_subagents(_ctx(tmp_path))

    assert plan.agents == []
    assert plan.guidance == ""
    assert plan.support == "disabled"
    assert plan.enabled is False


def test_scout_capabilities_follow_stage_tool_policy(tmp_path, monkeypatch):
    """阶段禁掉 Read / Web 后，scout 不能还能"读文件"或"查网页"。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)

    no_read = resolve_subagents(_ctx(tmp_path, disallowed_tools=["Read"]))
    assert "read_files" not in no_read.agents[0].capabilities
    assert "search_code" in no_read.agents[0].capabilities

    no_web = resolve_subagents(
        _ctx(tmp_path, disallowed_tools=["WebSearch", "WebFetch"])
    )
    assert "web" not in no_web.agents[0].capabilities

    no_read_no_search = resolve_subagents(
        _ctx(tmp_path, disallowed_tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"])
    )
    assert no_read_no_search.agents == []
    assert no_read_no_search.guidance == ""


def test_declared_write_agent_downgraded_by_default(tmp_path, monkeypatch):
    """模板声明 write 但没放行时降级为只读，并剔除 shell / 写文件能力。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)
    monkeypatch.setattr(settings, "subagent_allow_write", False)

    plan = resolve_subagents(
        _ctx(tmp_path),
        declared=[
            {
                "id": "test-runner",
                "description": "跑测试并汇总失败项",
                "mode": "write",
                "capabilities": ["shell", "write_files", "read_files"],
            }
        ],
    )

    runner = next(spec for spec in plan.agents if spec.id == "test-runner")
    assert runner.mode == "readonly"
    assert runner.capabilities == ["read_files"]
    assert any("降级为 readonly" in note for note in plan.notes)
    # 池里只要有一个写型，就不允许并行
    assert plan.parallel is True  # 已降级，全只读


def test_declared_agent_merges_and_overrides_builtin(tmp_path, monkeypatch):
    """同名声明覆盖内置 scout，顺序按声明序；缺 description 的条目被丢弃。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)

    plan = resolve_subagents(
        _ctx(tmp_path),
        declared=[
            {"id": "scout", "description": "本任务专用的侦察描述"},
            {"id": "web-researcher", "description": "检索并交叉验证来源", "capabilities": ["web"]},
            {"id": "broken"},
        ],
    )

    assert [spec.id for spec in plan.agents] == ["scout", "web-researcher"]
    assert plan.agents[0].description == "本任务专用的侦察描述"
    assert plan.agents[0].source == "template"
    assert any("缺 id 或 description" in note for note in plan.notes)


def test_description_budget_trims_tail(tmp_path, monkeypatch):
    """description 预算超出时按声明序截断，避免撞 harness 的提示词预算。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)
    # 预算只够内置 scout + 第一个声明（scout 描述约 90 字符，每条声明约 190）
    monkeypatch.setattr(settings, "subagent_description_budget", 300)

    plan = resolve_subagents(
        _ctx(tmp_path),
        declared=[
            {"id": "a", "description": "x" * 180},
            {"id": "b", "description": "y" * 180},
        ],
    )

    assert [spec.id for spec in plan.agents] == ["scout", "a"]
    assert any("超预算" in note for note in plan.notes)


def test_tool_names_mapping_is_harness_specific():
    """能力标签只在支持工具裁剪的 harness 上翻译；不支持时返回空表。"""
    assert tool_names("claude_code", ["read_files", "search_code"]) == ["Read", "Grep", "Glob"]
    assert tool_names("codex", ["read_files"]) == []
    assert tool_names("dsh", ["read_files"]) == []
    assert tool_names("unknown", ["read_files"]) == []


def test_claude_agent_definitions_render_and_drop_banned_tools():
    """claude 侧渲染：能力→工具名、剔除阶段禁用项、透传 maxTurns。"""
    from sortie_deck.harness_providers.claude_code import _agent_definitions

    defs = _agent_definitions(
        {
            "agents": [
                {
                    "id": "scout",
                    "description": "只读侦察",
                    "prompt": "你是侦察子代理",
                    "capabilities": ["read_files", "search_code"],
                    "max_turns": 5,
                }
            ]
        },
        ["Read"],
    )

    assert list(defs) == ["scout"]
    assert defs["scout"].tools == ["Grep", "Glob"]
    assert defs["scout"].maxTurns == 5
    assert defs["scout"].description == "只读侦察"


def test_claude_agent_definitions_empty_without_pool():
    from sortie_deck.harness_providers.claude_code import _agent_definitions

    assert _agent_definitions({}, []) == {}
    assert _agent_definitions({"agents": []}, []) == {}


def test_unsupported_harness_gets_empty_pool(tmp_path, monkeypatch):
    """cursor 没有委派能力：给指引只会让模型假装委派，必须空池。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)

    plan = resolve_subagents(_ctx(tmp_path, harness="cursor"))

    assert plan.agents == []
    assert plan.guidance == ""
    assert plan.support == "unsupported"


def test_prompt_only_harness_keeps_pool(tmp_path, monkeypatch):
    """dsh 有 tool-subagent 但自定义定义未验证：保留池，标记 prompt_only。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subagent_enabled", True)

    plan = resolve_subagents(_ctx(tmp_path, harness="dsh"))

    assert plan.support == "prompt_only"
    assert [spec.id for spec in plan.agents] == ["scout"]
