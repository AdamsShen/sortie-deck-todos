"""阶段产物解析：直写 artifacts/<stage>/ 与 workspace 归档兜底。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sortie_deck.harness_providers.base import HarnessReply, HarnessSession, ProbeStatus
from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import RoleId, StageContext


class _WriterProvider:
    """按需把文件写进 cwd 的假 harness，用于验证产物解析顺序。"""

    name = "writer"

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or {}
        self.calls: list[tuple[str, list[str], str]] = []

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
        on_tool_result: Any = None,
    ) -> HarnessReply:
        self.calls.append((str(cwd or ""), list(extra_dirs or []), prompt))
        root = Path(str(cwd or "."))
        root.mkdir(parents=True, exist_ok=True)
        for name, content in self.files.items():
            (root / name).write_text(content, encoding="utf-8")
        return HarnessReply(status="ok", text="stage done\n\n## Result\nok")

    async def close(self, session: HarnessSession) -> None:
        return None


class _Registry:
    def __init__(self, provider: _WriterProvider) -> None:
        self.provider = provider

    def get(self, name: str) -> _WriterProvider:
        return self.provider

    def has(self, name: str) -> bool:
        return True

    def probe(self, name: str, *, refresh: bool = False) -> ProbeStatus:
        return self.provider.probe()


def _ctx(
    tmp_path: Path,
    *,
    stage: str,
    role: str,
    artifact_dir: Path | None,
    upstream: dict[str, str] | None = None,
) -> StageContext:
    data_dir = tmp_path / "data"
    return StageContext(
        initiative_id="ini_art",
        thread_id="thr_art",
        stage=stage,
        role=RoleId(role),
        brief="加一个会话接口",
        artifact_dir=str(artifact_dir) if artifact_dir else "",
        harness="writer",
        upstream_artifacts=upstream or {},
        data_dir=str(data_dir),
        worktrees_dir=str(data_dir / "missions"),
    )


def _run(monkeypatch, provider: _WriterProvider, ctx: StageContext, *, host=None):
    monkeypatch.setattr(
        "sortie_deck.harness_providers.base.get_harness_registry",
        lambda: _Registry(provider),
    )
    return asyncio.run(HarnessStageExecutor().run(ctx, host=host))


@pytest.mark.parametrize("tasks", [None, "invalid JSON", '{"status":"completed"}'])
def test_build_recovery_without_checkpoint_requires_real_artifacts(tmp_path, monkeypatch, tasks):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "review_lenses_enabled", False)
    artifact_dir = tmp_path / "data/missions/ini_art/artifacts/eng_implement"
    ctx = _ctx(tmp_path, stage="eng_implement", role="eng", artifact_dir=artifact_dir)
    ctx.harness = "dsh"
    ctx.permissions["build_recovery"] = True
    files = {"implementation.md": "# Implementation"}
    if tasks is not None:
        files["tasks.json"] = tasks

    result = _run(monkeypatch, _WriterProvider(files), ctx)

    if tasks == '{"status":"completed"}':
        assert result.status == "ok"
        assert result.artifacts["tasks.json"] == tasks
        assert result.proposed_next == "qa_verify"
    else:
        assert result.status == "fail"
        assert result.meta["build_checkpoint"] is True
        assert "tasks.json" not in result.artifacts
        assert not (artifact_dir / "tasks.json").exists()


@pytest.mark.parametrize("source_changed,manager_follow", [(False, False), (True, False), (False, True)])
def test_dsh_timeout_resumes_verified_build_checkpoint(
    tmp_path, monkeypatch, source_changed, manager_follow
):
    from sortie_deck.settings import settings

    class Manager:
        async def escalate(self, question):
            return "manager: Proceed"

    class InterruptedBuild(_WriterProvider):
        async def send(self, session, prompt, *, cwd, on_tool_result=None, **kwargs):
            root = Path(cwd)
            source = root / "repos" / "app.js"
            turn = len(self.calls)
            self.calls.append(prompt)
            if manager_follow and turn == 0:
                return HarnessReply(
                    status="ok", text='```control\n{"escalate": {"question": "Proceed?"}}\n```',
                )
            if turn == int(manager_follow):
                source.parent.mkdir(exist_ok=True)
                source.write_text("first version", encoding="utf-8")
                (root / "checklist.md").write_text(
                    "- [x] C1 first — verify: `check-first`\n"
                    "- [x] C2 second — verify: `check-second`\n",
                    encoding="utf-8",
                )
                log = root / "selftest_log.json"
                first = '{"item":"C1","command":"check-first","exit_code":0}'
                log.write_text(f"[{first}]", encoding="utf-8")
                if on_tool_result:
                    on_tool_result()
                source.write_text("second version", encoding="utf-8")
                log.write_text(
                    f'[{first},{{"item":"C2","command":"check-second","exit_code":0}}]',
                    encoding="utf-8",
                )
                (root / "implementation.md").write_text("# Completed implementation", encoding="utf-8")
                if on_tool_result:
                    on_tool_result()
                return HarnessReply(status="fail", text="DSH timeout", meta={"timeout": True})
            (root / "tasks.json").write_text('{"status":"completed"}', encoding="utf-8")
            if source_changed:
                (root / "implementation.md").write_text("# Updated implementation", encoding="utf-8")
            return HarnessReply(status="ok", text="Finished checkpoint")

    monkeypatch.setattr(settings, "review_lenses_enabled", False)
    artifact_dir = tmp_path / "data/missions/ini_art/artifacts/eng_implement"
    ctx = _ctx(tmp_path, stage="eng_implement", role="eng", artifact_dir=artifact_dir)
    ctx.harness = "dsh"
    provider = InterruptedBuild()

    failed = _run(monkeypatch, provider, ctx, host=Manager() if manager_follow else None)
    assert failed.status == "fail"
    assert failed.meta["build_checkpoint"] is True
    assert "tasks.json" not in failed.artifacts
    checkpoint = tmp_path / "data/missions/ini_art/logs/eng_implement/build-checkpoint.json"
    assert checkpoint.is_file()

    if source_changed:
        workspace = tmp_path / "data/missions/ini_art/workspace"
        (workspace / "repos/app.js").write_text("edited after timeout", encoding="utf-8")
    resumed = _run(monkeypatch, provider, ctx)
    verified = "none" if source_changed else "C2"
    assert f"Reusable verified items: {verified}." in provider.calls[-1]
    expected = "# Updated implementation" if source_changed else "# Completed implementation"
    assert resumed.artifacts["implementation.md"] == expected
    assert resumed.artifacts["tasks.json"] == '{"status":"completed"}'
    assert resumed.status == "ok"
    assert resumed.proposed_next == "qa_verify"
    assert not checkpoint.exists()


def test_doc_stage_writes_straight_into_artifact_dir(tmp_path, monkeypatch):
    """文档型阶段 cwd 就是产物目录：写盘即进档案，且提示词给出输出契约。"""
    art = tmp_path / "data/missions/ini_art/artifacts/product_prd"
    provider = _WriterProvider(
        {"prd.md": "# 真 PRD", "acceptance.json": '{"acs": [1]}'}
    )
    ctx = _ctx(
        tmp_path,
        stage="product_prd",
        role="product",
        artifact_dir=art,
        upstream={"prd.md": "# intake 草稿"},
    )

    result = _run(monkeypatch, provider, ctx)

    assert result.status == "ok"
    assert result.artifacts["prd.md"] == "# 真 PRD"
    assert result.artifacts["acceptance.json"] == '{"acs": [1]}'
    assert result.meta["artifact_sources"] == {
        "prd.md": "artifact_dir",
        "acceptance.json": "artifact_dir",
    }
    # cwd 与追加目录
    cwd, extra, prompt = provider.calls[0]
    assert Path(cwd) == art
    assert extra and Path(extra[0]).name == "workspace"
    assert "Output dir (cwd)" in prompt
    # 上游产物镜像到 cwd 内，workspace 以软链暴露
    assert (art / ".upstream/prd.md").read_text(encoding="utf-8") == "# intake 草稿"
    assert (art / "workspace").is_symlink()
    assert str(art / ".upstream/prd.md") in prompt


def test_coding_stage_archives_workspace_contract_files(tmp_path, monkeypatch):
    """编码型阶段 cwd 仍是 workspace，契约文档收尾归档进产物目录。"""
    art = tmp_path / "data/missions/ini_art/artifacts/eng_implement"
    provider = _WriterProvider(
        {"implementation.md": "# 实现说明", "tasks.json": "[]"}
    )
    ctx = _ctx(tmp_path, stage="eng_implement", role="eng", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert result.status == "ok"
    assert result.artifacts["implementation.md"] == "# 实现说明"
    assert result.meta["artifact_sources"]["implementation.md"] == "workspace"
    # workspace 里的真文件已归档到产物目录
    assert (art / "implementation.md").read_text(encoding="utf-8") == "# 实现说明"
    cwd, extra, _prompt = provider.calls[0]
    assert Path(cwd).name == "workspace"
    assert extra == [str(art)]


def test_process_files_go_to_mission_logs_dir(tmp_path):
    """过程文件（回复全文等）落在 missions/<id>/logs/<stage>/，不进产物目录。"""
    from sortie_deck.artifacts import LocalArtifactStore
    from sortie_deck.graph import _PROCESS_ARTIFACT_NAMES, _stage_logs_dir, _write_process_artifact

    store = LocalArtifactStore(tmp_path / "missions", layout="mission")
    logs_dir = _stage_logs_dir(store, "ini_log", "product_prd")

    assert logs_dir is not None
    assert logs_dir == tmp_path / "missions/ini_log/logs/product_prd"
    _write_process_artifact(logs_dir, "reply.md", "# 过程记录")
    assert (logs_dir / "reply.md").read_text(encoding="utf-8") == "# 过程记录"
    assert "reply.md" in _PROCESS_ARTIFACT_NAMES
    assert "prd.md" not in _PROCESS_ARTIFACT_NAMES


def test_persist_splits_deliverables_and_process_files(tmp_path):
    """阶段收尾落盘：交付文档进产物库，回复/沟通/审计进 logs/<stage>/。"""
    from sortie_deck.artifacts import LocalArtifactStore
    from sortie_deck.graph import _persist_stage_artifacts

    store = LocalArtifactStore(tmp_path / "missions", layout="mission")
    written = _persist_stage_artifacts(
        store,
        "ini_split",
        "product_prd",
        {
            "prd.md": "# PRD",
            "acceptance.json": "{}",
            "reply.md": "# reply",
            "consult_log.md": "# consult",
            "toolkit_meta.json": "[]",
        },
    )

    # 只有交付物进索引 / upstream
    assert written == {
        "prd.md": "ini_split/artifacts/product_prd/prd.md",
        "acceptance.json": "ini_split/artifacts/product_prd/acceptance.json",
    }
    assert store.read_text("ini_split/artifacts/product_prd/prd.md") == "# PRD"
    # 过程文件落在 logs，不出现在产物目录
    logs = tmp_path / "missions/ini_split/logs/product_prd"
    assert (logs / "reply.md").read_text(encoding="utf-8") == "# reply"
    assert (logs / "consult_log.md").read_text(encoding="utf-8") == "# consult"
    assert (logs / "toolkit_meta.json").read_text(encoding="utf-8") == "[]"
    artifact_root = tmp_path / "missions/ini_split/artifacts/product_prd"
    for name in ("reply.md", "consult_log.md", "toolkit_meta.json"):
        assert not (artifact_root / name).exists()


def test_stale_artifact_is_archived_and_not_reused(tmp_path, monkeypatch):
    """上一轮遗留的产物不算本轮结果：移入 .runs/，本轮只能回退回复包装。"""
    art = tmp_path / "data/missions/ini_art/artifacts/product_prd"
    art.mkdir(parents=True)
    (art / "prd.md").write_text("# 上一轮的 PRD", encoding="utf-8")

    provider = _WriterProvider()  # 本轮什么都不写
    ctx = _ctx(tmp_path, stage="product_prd", role="product", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert result.artifacts["prd.md"] != "# 上一轮的 PRD"
    assert "# prd.md — stage `product_prd`" in result.artifacts["prd.md"]
    assert result.meta["artifact_degraded"] == ["prd.md", "acceptance.json"]
    archived = list((art / ".runs").rglob("prd.md"))
    assert len(archived) == 1 and archived[0].read_text(encoding="utf-8") == "# 上一轮的 PRD"
