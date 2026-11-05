"""降级自检与 QA 门禁推荐动作。

背景：harness 异常收尾（status=fail）且自审没跑成时，旧的链路会直接把残缺产物
交给 QA，QA 再花一整轮判 fail；QA 判失败后人工点「批准」实际仍会被打回开发。
"""

from __future__ import annotations

from sortie_deck.graph import _gate_recommendation, _needs_self_check


def _result(status: str = "fail", review: dict | None = None, qa_passed: bool | None = None):
    return {
        "status": status,
        "meta": {"review": review} if review is not None else {},
        "qa_passed": qa_passed,
    }


def test_self_check_when_stage_failed_and_review_skipped():
    result = _result(review={"rounds": 3, "verdict": "not_run", "statuses": {"correctness": "skipped"}})
    assert _needs_self_check("eng_implement", result) is True


def test_self_check_when_review_missing_entirely():
    # 自审被全局关闭 / 根本没进 review 分支，也算“没跑成”
    assert _needs_self_check("eng_implement", _result()) is True


def test_no_self_check_when_review_produced_a_verdict():
    for verdict in ("passed", "findings", "blocking"):
        result = _result(review={"statuses": {"correctness": verdict}})
        assert _needs_self_check("eng_implement", result) is False


def test_no_self_check_for_healthy_deploy_or_terminal_stages():
    assert _needs_self_check("eng_implement", _result(status="ok")) is False
    assert _needs_self_check("deploy_preview", _result()) is False
    assert _needs_self_check("done", _result()) is False


def test_gate_recommends_rework_when_qa_failed():
    gate = _gate_recommendation("qa_verify", _result(qa_passed=False), "eng_implement")
    assert gate["verdict"]["passed"] is False
    assert gate["recommended_action"]["action"] == "reroute"
    assert gate["recommended_action"]["next_stage"] == "eng_implement"
    assert gate["recommended_action"]["label"] == "返工"


def test_gate_keeps_approve_when_qa_passed():
    gate = _gate_recommendation("qa_verify", _result(qa_passed=True), "eng_implement")
    assert gate["recommended_action"]["action"] == "approve"


def test_gate_has_no_recommendation_without_verdict_or_for_other_stages():
    assert _gate_recommendation("qa_verify", _result(qa_passed=None), "eng_implement") == {}
    assert _gate_recommendation("intake", _result(qa_passed=False), "eng_implement") == {}
