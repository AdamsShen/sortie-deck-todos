"""QA 浏览器复验 runner：注入、幂等与 prompt 硬约束。"""

from __future__ import annotations

from pathlib import Path

from sortie_deck.harness_providers.executor import HarnessStageExecutor, _ensure_qa_runner
from sortie_deck.models import RoleId, StageContext

RUNNER = Path("scripts") / "qa_browser_runner.mjs"


def _ctx(stage: str = "qa_verify") -> StageContext:
    return StageContext(
        initiative_id="ini_test",
        thread_id="thr_test",
        stage=stage,
        role=RoleId.QA,
        brief="贪吃蛇小游戏，带登录注册",
        artifact_dir="/tmp/artifacts",
    )


def test_runner_injected_for_qa_stage(tmp_path: Path):
    _ensure_qa_runner(tmp_path, "qa_verify")
    injected = tmp_path / RUNNER
    assert injected.is_file()
    assert injected.stat().st_mode & 0o111
    assert "qa_result.json" in injected.read_text(encoding="utf-8")


def test_runner_skipped_for_non_qa_stage(tmp_path: Path):
    _ensure_qa_runner(tmp_path, "eng_implement")
    assert not (tmp_path / RUNNER).exists()


def test_runner_never_overwrites_project_script(tmp_path: Path):
    target = tmp_path / RUNNER
    target.parent.mkdir(parents=True)
    target.write_text("// project owned", encoding="utf-8")
    _ensure_qa_runner(tmp_path, "qa_verify")
    assert target.read_text(encoding="utf-8") == "// project owned"


def test_prompt_requires_runner_once_injected(tmp_path: Path):
    _ensure_qa_runner(tmp_path, "qa_verify")
    prompt = HarnessStageExecutor()._build_prompt(_ctx(), tmp_path, {"briefs": {}}, "")
    assert "qa_browser_runner.mjs" in prompt
    assert "do NOT hand-write CDP" in prompt


def test_prompt_omits_runner_rules_when_not_injected(tmp_path: Path):
    # 注入失败时不能要求模型去跑一个不存在的脚本
    prompt = HarnessStageExecutor()._build_prompt(_ctx(), tmp_path, {"briefs": {}}, "")
    assert "qa_browser_runner.mjs" not in prompt
