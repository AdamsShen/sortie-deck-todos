from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck.loadout import (
    CapabilityNeed,
    build_loadout,
    manifest_for,
    probe_item,
    recall,
)
from sortie_deck.mcp_runtime import McpToolInvoker, tool_looks_like_write
from sortie_deck.skill_runtime import SkillRuntime, detect_write
from sortie_deck.toolkit import (
    CreateToolkitRequest,
    SkillScript,
    ToolkitItem,
    ToolkitStore,
    UpdateToolkitRequest,
)


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


def _add(store: ToolkitStore, **kwargs: Any) -> ToolkitItem:
    return store.create(
        CreateToolkitRequest(**kwargs),
        author_id="tester",
        author_name="Tester",
    )


class _FakeLlm:
    enabled = True
    backend = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        return dict(self._payload)


# --- recall ---------------------------------------------------------------


def test_recall_ranks_capability_labels_first(store: ToolkitStore) -> None:
    _add(
        store,
        kind="tool",
        name="health-probe",
        summary="probe preview health",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="git --version",
    )
    _add(
        store,
        kind="tool",
        name="unrelated",
        summary="nothing to do with it",
        runtime="cli",
        endpoint="git --version",
    )
    hits = recall(store, "deploy health")
    assert hits
    assert hits[0][0].name == "health-probe"
    assert hits[0][1] > 0


def test_recall_ignores_disabled_items(store: ToolkitStore) -> None:
    item = _add(
        store,
        kind="tool",
        name="off",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="git --version",
    )
    _add(
        store,
        kind="tool",
        name="on",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="git --version",
    )
    store.update(item.id, UpdateToolkitRequest(enabled=False))
    names = [i.name for i, _ in recall(store, "deploy health")]
    assert "off" not in names
    assert "on" in names


def test_recall_without_store_or_tokens_is_empty(store: ToolkitStore) -> None:
    assert recall(None, "anything") == []
    assert recall(store, "   ") == []


# --- probe ----------------------------------------------------------------


async def test_probe_cli_tool_reports_missing_binary() -> None:
    item = ToolkitItem(kind="tool", name="ghost", runtime="cli", endpoint="definitely-not-a-real-bin-xyz")
    status, detail = await probe_item(item)
    assert status == "unavailable"
    assert "not found" in detail


async def test_probe_cli_tool_finds_python() -> None:
    import shutil

    binary = "python3" if shutil.which("python3") else "git"
    item = ToolkitItem(kind="tool", name="py", runtime="cli", endpoint=f"{binary} --version")
    status, _detail = await probe_item(item)
    assert status == "ok"


async def test_probe_skill_without_executable_flag_is_degraded() -> None:
    item = ToolkitItem(
        kind="skill",
        name="writer",
        scripts=[SkillScript(path="a.py", content="print(1)")],
        executable=False,
    )
    status, detail = await probe_item(item)
    assert status == "degraded"
    assert "not marked executable" in detail


async def test_probe_disabled_item_is_unavailable() -> None:
    item = ToolkitItem(kind="tool", name="x", runtime="cli", endpoint="git", enabled=False)
    status, _ = await probe_item(item)
    assert status == "unavailable"


# --- build_loadout --------------------------------------------------------


async def test_build_loadout_without_needs_is_empty(store: ToolkitStore) -> None:
    plan = await build_loadout(store=store, needs=[])
    assert plan.selected == []
    assert plan.manifest == ""


async def test_build_loadout_without_library_notes_the_gap() -> None:
    plan = await build_loadout(store=None, needs=[CapabilityNeed(capability="deploy health")])
    assert plan.selected == []
    assert plan.notes


async def test_build_loadout_picks_and_probes_a_candidate(store: ToolkitStore) -> None:
    _add(
        store,
        kind="tool",
        name="health-probe",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="git --version",
        executable=True,
    )
    plan = await build_loadout(
        store=store,
        needs=[CapabilityNeed(capability="deploy health")],
        stage="qa_verify",
        role="qa",
        use_llm=False,
    )
    assert plan.planner == "heuristic"
    assert [c.name for c in plan.selected] == ["health-probe"]
    assert plan.selected[0].loaded is True
    assert "health-probe" in plan.manifest


async def test_build_loadout_degrades_an_unavailable_candidate(store: ToolkitStore) -> None:
    _add(
        store,
        kind="tool",
        name="ghost",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="definitely-not-a-real-bin-xyz",
        executable=True,
    )
    plan = await build_loadout(
        store=store,
        needs=[CapabilityNeed(capability="deploy health")],
        use_llm=False,
    )
    assert plan.selected[0].loaded is False
    assert plan.rejected
    assert "ghost" not in plan.manifest


async def test_build_loadout_llm_can_only_choose_recalled_candidates(store: ToolkitStore) -> None:
    good = _add(
        store,
        kind="tool",
        name="health-probe",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="git --version",
    )
    llm = _FakeLlm({"selections": [{"capability": "deploy health", "item_id": "tk_hallucinated"}]})
    plan = await build_loadout(
        store=store,
        needs=[CapabilityNeed(capability="deploy health")],
        use_llm=True,
        client=llm,
    )
    assert plan.planner == "heuristic", "hallucinated ids must be ignored"
    assert [c.item_id for c in plan.selected] == [good.id]


async def test_build_loadout_llm_selection_must_still_probe(store: ToolkitStore) -> None:
    item = _add(
        store,
        kind="tool",
        name="ghost",
        capabilities=["deploy health"],
        runtime="cli",
        endpoint="definitely-not-a-real-bin-xyz",
    )
    llm = _FakeLlm({"selections": [{"capability": "deploy health", "item_id": item.id}]})
    plan = await build_loadout(
        store=store,
        needs=[CapabilityNeed(capability="deploy health")],
        use_llm=True,
        client=llm,
    )
    assert plan.planner == "llm"
    assert plan.selected[0].loaded is False
    assert any(r["item_id"] == item.id for r in plan.rejected)


def test_manifest_lists_loaded_and_degraded() -> None:
    from sortie_deck.loadout import LoadoutChoice

    text = manifest_for(
        [
            LoadoutChoice(item_id="a", name="loadable", kind="tool", loaded=True),
            LoadoutChoice(
                item_id="b", name="broken", kind="mcp", loaded=False, detail="`x` not found on PATH"
            ),
        ]
    )
    assert "loadable" in text
    assert "Unavailable" in text
    assert "not found on PATH" in text


# --- skill runtime --------------------------------------------------------


def test_detect_write_flags_mutating_scripts() -> None:
    assert detect_write("open('a.txt','w')")
    assert detect_write("rm -rf build")
    assert detect_write("git commit -m x")
    assert detect_write("curl -X POST https://x") is True
    assert detect_write("print('hello')") is False


def _skill_item(**kwargs: Any) -> ToolkitItem:
    return ToolkitItem(
        kind="skill",
        name="demo",
        scripts=[SkillScript(path="run.py", content=kwargs.pop("content", "print('hi')"), language="py")],
        **kwargs,
    )


async def test_skill_runtime_disabled_blocks_execution() -> None:
    result = await SkillRuntime(enabled=False).run(_skill_item(executable=True), _skill_item(executable=True).scripts[0])
    assert result.blocked is True
    assert "disabled" in result.reason


async def test_skill_runtime_blocks_a_non_executable_skill() -> None:
    item = _skill_item(executable=False)
    result = await SkillRuntime(enabled=True).run(item, item.scripts[0])
    assert result.blocked is True
    assert "not marked executable" in result.reason


async def test_skill_runtime_blocks_a_write_without_confirmation() -> None:
    item = _skill_item(executable=True, content="open('out.txt','w').write('x')")
    result = await SkillRuntime(enabled=True).run(item, item.scripts[0])
    assert result.blocked is True
    assert result.write_detected is True
    assert "confirmation required" in result.reason


async def test_skill_runtime_runs_a_confirmed_write_script(tmp_path: Path) -> None:
    import sys

    item = _skill_item(
        executable=True,
        content=f"open({str(tmp_path / 'out.txt')!r},'w').write('done')",
    )
    result = await SkillRuntime(enabled=True, workdir=tmp_path).run(
        item, item.scripts[0], confirmed=True
    )
    if sys.platform == "win32":  # pragma: no cover - repo targets posix CI
        pytest.skip("posix only")
    assert result.blocked is False
    assert result.ok is True
    assert (tmp_path / "out.txt").read_text() == "done"


async def test_skill_runtime_runs_a_read_only_script(tmp_path: Path) -> None:
    item = _skill_item(executable=True, content="print('hello world')")
    result = await SkillRuntime(enabled=True, workdir=tmp_path).run(item, item.scripts[0])
    assert result.ok is True
    assert "hello world" in result.stdout


async def test_skill_runtime_blocks_an_unsupported_language(tmp_path: Path) -> None:
    script = SkillScript(path="run.rb", content="puts 1", language="ruby")
    item = ToolkitItem(kind="skill", name="rb", scripts=[script], executable=True)
    result = await SkillRuntime(enabled=True, workdir=tmp_path).run(item, script)
    assert result.blocked is True
    assert "not allowed" in result.reason


async def test_skill_runtime_times_out(tmp_path: Path) -> None:
    item = _skill_item(executable=True, content="import time\ntime.sleep(2)")
    result = await SkillRuntime(enabled=True, timeout_seconds=0.2, workdir=tmp_path).run(
        item, item.scripts[0]
    )
    assert result.ok is False
    assert "timed out" in result.reason


async def test_skill_runtime_run_path_reports_a_missing_script(tmp_path: Path) -> None:
    item = _skill_item(executable=True)
    result = await SkillRuntime(enabled=True, workdir=tmp_path).run_path(item, "nope.py")
    assert result.blocked is True
    assert "not found" in result.reason


def test_skill_runtime_availability(tmp_path: Path) -> None:
    runtime = SkillRuntime(enabled=True)
    ok, _ = runtime.availability(_skill_item(executable=True))
    assert ok is True
    bad, reason = runtime.availability(_skill_item(executable=False))
    assert bad is False
    assert reason


# --- MCP invoker ---------------------------------------------------------


def test_tool_looks_like_write() -> None:
    assert tool_looks_like_write("write_file") is True
    assert tool_looks_like_write("read_file") is False


async def test_invoker_disabled_blocks_calls() -> None:
    invoker = McpToolInvoker(enabled=False)
    outcome = await invoker.call("read_file", {})
    assert outcome["blocked"] is True


async def test_invoker_blocks_tools_outside_the_whitelist() -> None:
    invoker = McpToolInvoker(enabled=True)
    invoker._allowed = {"read_file": {"filesystem"}}
    outcome = await invoker.call("delete_everything", {})
    assert outcome["blocked"] is True
    assert "whitelist" in outcome["reason"]


async def test_invoker_requires_confirmation_for_write_tools() -> None:
    invoker = McpToolInvoker(enabled=True)
    invoker._allowed = {"write_file": {"filesystem"}}
    outcome = await invoker.call("write_file", {"path": "a"})
    assert outcome["blocked"] is True
    assert "confirmation" in outcome["reason"]


async def test_invoker_calls_an_allowed_read_tool() -> None:
    class _Item:
        name = "filesystem"

    class _Session:
        item = _Item()

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return {"content": "ok", "tool": name}

    invoker = McpToolInvoker(enabled=True)
    invoker._manager.sessions = [_Session()]  # type: ignore[list-item]
    invoker._allowed = {"read_file": {"filesystem"}}
    outcome = await invoker.call("read_file", {"path": "a"})
    assert outcome["ok"] is True
    assert outcome["result"]["tool"] == "read_file"


async def test_invoker_swallows_tool_errors() -> None:
    class _Item:
        name = "filesystem"

    class _Session:
        item = _Item()

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

    invoker = McpToolInvoker(enabled=True)
    invoker._manager.sessions = [_Session()]  # type: ignore[list-item]
    invoker._allowed = {"read_file": {"filesystem"}}
    outcome = await invoker.call("read_file", {})
    assert outcome["ok"] is False
    assert "boom" in outcome["error"]
