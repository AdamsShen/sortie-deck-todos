"""多镜头自审：视角选择、协议解析、日志落盘与执行器接入。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sortie_deck.harness_providers.base import HarnessReply, HarnessSession, ProbeStatus
from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import RoleId, StageContext
from sortie_deck.review_lens import (
    LENSES,
    LensStatus,
    ReviewScope,
    build_scope,
    build_static_review_section,
    compute_verdict,
    parse_diff_stats,
    parse_review_reply,
    record_static_review,
    render_review_markdown,
    run_review_lenses,
    select_lenses,
)


def _ctx(tmp_path: Path, *, stage: str = "eng_web", role: str = "eng_web") -> StageContext:
    data_dir = tmp_path / "data"
    return StageContext(
        initiative_id="ini_rev",
        thread_id="thr_rev",
        stage=stage,
        role=RoleId(role),
        brief="给会话页加已读状态",
        artifact_dir="",
        harness="writer",
        data_dir=str(data_dir),
        worktrees_dir=str(data_dir / "missions"),
    )


def _reply(text: str, status: str = "ok") -> HarnessReply:
    return HarnessReply(status=status, text=text)


def _review_text(
    status: str = "passed",
    findings: list[dict[str, str]] | None = None,
    reviewed: list[str] | None = None,
) -> str:
    payload = {
        "status": status,
        "reviewed": reviewed or ["src/session.ts:42"],
        "findings": findings or [],
        "summary": f"review {status}",
    }
    return (
        "已按本轮视角检查完毕。\n\n```review\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```\n"
    )


# --- 视角与解析 ---------------------------------------------------------------


def test_select_lenses_light_and_full():
    assert [lens.name for lens in select_lenses(0)] == []
    assert [lens.name for lens in select_lenses(1)] == ["correctness"]
    assert [lens.name for lens in select_lenses(3)] == ["correctness", "boundaries", "test_gaps"]
    # 4 轮按轻量模式处理，不半吊子插入并发
    assert [lens.name for lens in select_lenses(4)] == [
        "correctness",
        "boundaries",
        "test_gaps",
    ]
    assert [lens.name for lens in select_lenses(5)] == [lens.name for lens in LENSES]
    assert select_lenses(5)[-1].name == "test_gaps"  # 只记录缺口的视角必须最后


def test_parse_review_reply_ok_and_missing_block():
    status, findings, reviewed, summary = parse_review_reply(
        _review_text(
            "blocking",
            [{"severity": "blocking", "where": "a.py:9", "issue": "空指针", "fix": "加判空"}],
        )
    )
    assert status == LensStatus.BLOCKING
    assert findings[0].severity.value == "blocking"
    assert reviewed == ["src/session.ts:42"]
    assert summary == "review blocking"

    unparsed, empty, _, _ = parse_review_reply("只写了自然语言，没有协议块")
    assert unparsed == LensStatus.UNPARSED
    assert empty == []


def test_compute_verdict_priority():
    from sortie_deck.review_lens import LensRun

    def run(status: LensStatus) -> LensRun:
        return LensRun(lens="x", title="x", round=1, status=status)

    assert compute_verdict([]) == "not_run"
    assert compute_verdict([run(LensStatus.PASSED)] * 3) == "passed"
    assert compute_verdict([run(LensStatus.PASSED), run(LensStatus.FINDINGS)]) == "findings"
    assert compute_verdict([run(LensStatus.FINDINGS), run(LensStatus.UNPARSED)]) == "degraded"
    assert compute_verdict([run(LensStatus.UNPARSED), run(LensStatus.BLOCKING)]) == "blocking"


def test_build_scope_from_diff_and_checklist(tmp_path):
    diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,3 @@
-old
+new
+more
"""
    ctx = _ctx(tmp_path)
    scope = build_scope(ctx, diff, "- [ ] C1 改 a.py\n- [x] C2 加测试\n- 普通说明")
    assert scope.changed_files == ["src/a.py"]
    assert scope.diff_added == 2
    assert scope.diff_removed == 1
    assert scope.contract_files == ["implementation.md", "tasks.json"]
    assert scope.checklist_items == ["- [ ] C1 改 a.py", "- [x] C2 加测试"]


def test_parse_diff_stats_handles_non_git_worktree_listing():
    files, added, removed = parse_diff_stats("# worktree files (no git)\n+ src/a.py\n+ src/b.py\n")
    assert files == ["src/a.py", "src/b.py"]
    assert (added, removed) == (0, 0)


# --- 轮次执行与日志 -----------------------------------------------------------


async def test_run_review_lenses_records_log(tmp_path):
    ctx = _ctx(tmp_path)
    scope = ReviewScope(stage="eng_web", role="eng_web", changed_files=["src/a.py"])
    prompts: list[str] = []

    async def send(prompt: str) -> HarnessReply:
        prompts.append(prompt)
        return _reply(_review_text("passed"))

    log = await run_review_lenses(
        ctx,
        scope,
        send,
        rounds=3,
        timeout=5,
        harness="writer",
        session_id="hs_test",
        data_dir=tmp_path / "data",
    )

    assert log.verdict == "passed"
    assert [run.status for run in log.lenses] == [LensStatus.PASSED] * 3
    assert len(prompts) == 3
    assert "正确性与不变量" in prompts[0]
    assert "测试缺口" in prompts[2]
    # 范围清单进了提示词 = 「审查了什么」有据可查
    assert "src/a.py" in prompts[0]

    log_dir = tmp_path / "data/missions/ini_rev/logs/eng_web"
    saved = json.loads((log_dir / "review.json").read_text(encoding="utf-8"))
    assert saved["verdict"] == "passed"
    assert len(saved["lenses"]) == 3
    lines = (log_dir / "review.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["lens"] == "correctness"


async def test_run_review_lenses_degrades_on_missing_block(tmp_path):
    ctx = _ctx(tmp_path)
    scope = ReviewScope(stage="eng_web", role="eng_web")

    async def send(_prompt: str) -> HarnessReply:
        return _reply("没有协议块的自由文本")

    log = await run_review_lenses(
        ctx, scope, send, rounds=3, timeout=5, data_dir=tmp_path / "data"
    )
    assert log.verdict == "degraded"
    assert all(run.status == LensStatus.UNPARSED for run in log.lenses)


async def test_run_review_lenses_survives_timeout(tmp_path):
    ctx = _ctx(tmp_path)
    scope = ReviewScope(stage="eng_web", role="eng_web")

    async def slow(_prompt: str) -> HarnessReply:
        await asyncio.sleep(0.3)
        return _reply(_review_text())

    log = await run_review_lenses(
        ctx, scope, slow, rounds=1, timeout=0.01, data_dir=tmp_path / "data"
    )
    assert log.lenses[0].status == LensStatus.FAILED
    assert "timeout" in log.lenses[0].error


def test_static_review_is_marked_prompted_not_passed(tmp_path):
    ctx = _ctx(tmp_path)
    scope = ReviewScope(stage="eng_web", role="eng_web")
    log = record_static_review(
        ctx, scope, harness="cursor_cli", rounds=3, data_dir=tmp_path / "data"
    )
    assert log.mode == "static"
    assert log.verdict == "not_run"
    assert all(run.status == LensStatus.PROMPTED for run in log.lenses)
    assert "Self-review" in build_static_review_section(3)
    markdown = render_review_markdown(log)
    assert "多镜头自审报告" in markdown
    assert "not_run" in markdown


# --- 执行器接入 ---------------------------------------------------------------


class _ReviewProvider:
    """首轮实现 + 后续自审轮，用 calls 计数区分。"""

    name = "writer"

    def __init__(self, review_text: str | None = None) -> None:
        self.review_text = review_text or _review_text()
        self.calls: list[str] = []

    def probe(self) -> ProbeStatus:
        return ProbeStatus(status="ok", detail="writer")

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(provider=self.name, meta={"stage": stage})

    async def send(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        cwd: str | None = None,
        context_pack: dict[str, Any] | None = None,
        extra_dirs: list[str] | None = None,
    ) -> HarnessReply:
        self.calls.append(prompt)
        if len(self.calls) == 1:
            root = Path(str(cwd or "."))
            root.mkdir(parents=True, exist_ok=True)
            (root / "implementation.md").write_text("# 实现说明\n", encoding="utf-8")
            (root / "tasks.json").write_text("[]", encoding="utf-8")
            return _reply("实现完成\n\n## Result\nok")
        return _reply(self.review_text)

    async def close(self, session: HarnessSession) -> None:
        return None


class _Registry:
    def __init__(self, provider: _ReviewProvider) -> None:
        self.provider = provider

    def get(self, name: str) -> _ReviewProvider:
        return self.provider

    def has(self, name: str) -> bool:
        return True

    def probe(self, name: str, *, refresh: bool = False) -> ProbeStatus:
        return self.provider.probe()


def _run(monkeypatch, provider: _ReviewProvider, ctx: StageContext):
    monkeypatch.setattr(
        "sortie_deck.harness_providers.base.get_harness_registry",
        lambda: _Registry(provider),
    )
    return asyncio.run(HarnessStageExecutor().run(ctx))


def test_executor_runs_review_and_attaches_report(tmp_path, monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    provider = _ReviewProvider()
    result = _run(monkeypatch, provider, _ctx(tmp_path))

    assert result.status == "ok"
    assert len(provider.calls) == 4  # 1 次实现 + 3 轮自审
    assert result.meta["review"]["verdict"] == "passed"
    assert result.meta["review"]["rounds"] == 3
    assert "多镜头自审报告" in result.artifacts["review.md"]
    assert Path(result.meta["review"]["log_path"]).is_file()


def test_executor_escalates_when_review_has_blocking(tmp_path, monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    blocking = _review_text(
        "blocking",
        [{"severity": "blocking", "where": "src/a.py:9", "issue": "并发写覆盖", "fix": "加锁"}],
    )
    result = _run(monkeypatch, _ReviewProvider(blocking), _ctx(tmp_path))

    assert result.status == "needs_hitl"
    assert result.meta["review_blocking"] is True
    assert result.meta["review"]["blocking"] == 3  # 三轮各报一条
    assert "blocking" in result.message


def test_executor_skips_review_for_mock_harness(tmp_path, monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    ctx = _ctx(tmp_path)
    ctx = ctx.model_copy(update={"harness": "mock"})
    result = asyncio.run(HarnessStageExecutor().run(ctx))

    assert result.meta["review"]["verdict"] == "not_run"
    assert result.meta["review"]["statuses"]["correctness"] == "skipped"
    # 没真跑的阶段不产出 review.md：日志可以说明「没审」，产物不能假装有报告
    assert "review.md" not in result.artifacts
