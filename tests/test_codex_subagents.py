"""codex 侧子代理：项目级 TOML 渲染、命名约束与协作事件观测。"""

from __future__ import annotations

import json

from sortie_deck.harness_providers.codex import (
    _codex_agent_name,
    _collab_stats,
    ensure_subagent_files,
)


def _spec(**kw):
    base = {
        "id": "web-researcher",
        "description": "检索并交叉验证",
        "prompt": "你是检索子代理",
        "mode": "readonly",
        "capabilities": ["web", "read_files"],
    }
    base.update(kw)
    return base


def test_codex_agent_name_only_lowercase_digits_underscore():
    """实测：codex 0.162.1 的 agent_name 带连字符会直接报错。"""
    assert _codex_agent_name("web-researcher") == "web_researcher"
    assert _codex_agent_name("Scout") == "scout"
    assert _codex_agent_name("a b/c") == "a_b_c"
    assert _codex_agent_name("") == "subagent"


def test_ensure_subagent_files_writes_project_level_toml(tmp_path):
    support, notes = ensure_subagent_files({"agents": [_spec()]}, tmp_path)

    assert support == "native"
    assert notes == []
    path = tmp_path / ".codex" / "agents" / "web_researcher.toml"
    text = path.read_text(encoding="utf-8")
    assert 'name = "web_researcher"' in text
    assert 'description = "检索并交叉验证"' in text
    assert "只读约束" in text
    # agent 自己的 git 操作看不到这些编排层文件
    assert (tmp_path / ".codex" / ".gitignore").read_text(encoding="utf-8") == "agents/\n"


def test_ensure_subagent_files_is_idempotent(tmp_path):
    """内容未变不重写：否则每轮刷新 mtime 会被误当成产出改动。"""
    ensure_subagent_files({"agents": [_spec()]}, tmp_path)
    path = tmp_path / ".codex" / "agents" / "web_researcher.toml"
    before = path.stat().st_mtime_ns

    ensure_subagent_files({"agents": [_spec()]}, tmp_path)

    assert path.stat().st_mtime_ns == before


def test_ensure_subagent_files_escapes_quotes_and_newlines(tmp_path):
    spec = _spec(description='带"引号"与\n换行')
    ensure_subagent_files({"agents": [spec]}, tmp_path)

    text = (tmp_path / ".codex" / "agents" / "web_researcher.toml").read_text(encoding="utf-8")
    assert '\\"引号\\"' in text
    assert "\\n换行" in text
    # 转义后仍是单行 TOML 字符串
    assert text.count("\n") == 3


def test_ensure_subagent_files_empty_pool(tmp_path):
    support, notes = ensure_subagent_files({}, tmp_path)
    assert support == "unsupported"
    assert notes == []
    assert not (tmp_path / ".codex").exists()


def test_collab_stats_counts_delegation_activity():
    """实测：spawn_agent 不出现在 JSON 流里，只能靠 collab_tool_call 计数。"""
    lines = [
        {"type": "item.started", "item": {"id": "i1", "type": "collab_tool_call", "tool": "wait"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "collab_tool_call", "tool": "wait"}},
        {
            "type": "item.completed",
            "item": {"id": "i2", "type": "collab_tool_call", "tool": "send_message"},
        },
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "done"}},
    ]
    stdout = "\n".join(json.dumps(line) for line in lines)

    stats = _collab_stats(stdout)

    assert stats["calls"] == 2
    assert stats["tools"] == ["wait", "send_message"]
    assert stats["source"] == "collab_tool_call"
    assert _collab_stats("{}") == {}
