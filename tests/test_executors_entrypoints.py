from __future__ import annotations

from sortie_deck.plugins.entrypoints import load_entry_point_executors, probe_executor
from sortie_deck.plugins.factory import build_default_registry
from sortie_deck.registry import ExecutorRegistry


def test_registry_includes_builtins():
    reg = build_default_registry()
    names = reg.names()
    assert "mock_eng" in names
    assert "claude_code" in names


def test_probe_executor():
    # probe_executor 只检查执行器是否具备 run 入口，返回 name/ok/module（无 status 字段）
    class E:
        name = "x"

        def run(self, ctx):
            return None

    probe = probe_executor(E())
    assert probe["name"] == "x"
    assert probe["ok"] is True
    assert probe["module"].endswith("test_executors_entrypoints")


def test_load_entry_points_tolerates_empty():
    reg = ExecutorRegistry()
    loaded = load_entry_point_executors(reg)
    assert isinstance(loaded, list)
