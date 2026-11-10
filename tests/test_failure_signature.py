"""失败签名 `_failure_signature` 的字段依赖与「连续同一失败」计数语义。

签名只依赖四个字段，测试据此构造最小输入：
- 显式入参 ``stage``
- ``result["status"]``
- ``meta["failure_kind"]``，缺失时回退 ``result["failure_kind"]``
- ``meta["failure_reason"]``，缺失时回退 ``result["message"]``，截断 120 字符
"""

from __future__ import annotations

from typing import Any

from sortie_deck import graph


def _state() -> dict[str, Any]:
    return {"initiative_id": "ini_test", "events": [], "failure_signatures": {}}


def test_signature_prefers_meta_fields() -> None:
    result = {
        "status": "fail",
        "message": "外层 message 不参与签名",
        "meta": {"failure_kind": "environment_blocked", "failure_reason": "docker 未启动"},
    }
    sig = graph._failure_signature("eng_implement", result)
    assert sig == "eng_implement|fail|environment_blocked|docker 未启动"


def test_signature_falls_back_to_top_level_fields() -> None:
    result = {"status": "fail", "failure_kind": "auth_blocked", "message": "token 过期"}
    assert graph._failure_signature("qa_verify", result) == "qa_verify|fail|auth_blocked|token 过期"


def test_signature_tolerates_missing_fields() -> None:
    # 既无 meta 也无 message：不得 KeyError，拼出稳定的空值签名
    assert graph._failure_signature("eng_implement", {"status": "fail"}) == "eng_implement|fail||"


def test_signature_truncates_reason_to_120_chars() -> None:
    long_reason = "错" * 300
    sig = graph._failure_signature("eng_implement", {"status": "fail", "message": long_reason})
    assert sig.endswith("|" + "错" * 120)


def test_signature_differs_by_stage_and_reason() -> None:
    base = {"status": "fail", "message": "测试失败"}
    assert graph._failure_signature("eng_implement", base) != graph._failure_signature("qa_verify", base)
    other = {"status": "fail", "message": "编译失败"}
    assert graph._failure_signature("eng_implement", base) != graph._failure_signature("eng_implement", other)


def test_consecutive_count_resets_when_signature_changes(monkeypatch: Any) -> None:
    """计数语义是「连续」：换一种失败即重新计 1，不沿用旧失败的次数。"""
    monkeypatch.setattr("sortie_deck.settings.settings.env_block_pause_enabled", False)
    monkeypatch.setattr("sortie_deck.settings.settings.max_consecutive_same_failure", 3)
    st = _state()
    first = {"status": "fail", "message": "测试失败"}
    assert graph._apply_environment_block_guard(st, "eng_implement", first) is None
    assert graph._apply_environment_block_guard(st, "eng_implement", first) is None
    assert list(st["failure_signatures"].values()) == [2]

    switched = {"status": "fail", "message": "编译失败"}
    assert graph._apply_environment_block_guard(st, "eng_implement", switched) is None
    assert list(st["failure_signatures"].values()) == [1]