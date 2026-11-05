"""阶段边界：上游镜像、跨阶段遗留清理、intake 轻契约。

这些用例锁的是三条线上踩过的坑（任务 ini_937080976c38）：
- 编码阶段读不到 artifacts/<prev>/，退回去读 workspace 里的旧版草稿；
- 旧版 prd.md 留在 workspace 根层，一直盖住正式版；
- intake 没有契约产物规格，模型自己脑补出整份 PRD + 验收用例。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sortie_deck.contracts import REQUIRED, StageContractError, validate_stage_artifacts
from sortie_deck.harness_providers.base import HarnessReply, HarnessSession, ProbeStatus
from sortie_deck.harness_providers.executor import (
    HarnessStageExecutor,
    _contract_names,
    _sweep_stale_contracts,
)
from sortie_deck.models import RoleId, StageContext


class _WriterProvider:
    """按需把文件写进 cwd 的假 harness。"""

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
    ) -> HarnessReply:
        self.calls.append((str(cwd or ""), list(extra_dirs or []), prompt))
        root = Path(str(cwd or "."))
        root.mkdir(parents=True, exist_ok=True)
        for name, content in self.files.items():
            (root / name).write_text(content, encoding="utf-8")
        return HarnessReply(status="ok", text="stage done\n\n## Result\nok")

    async def close(self, session: HarnessSession) -> None:
        return None


class _StrayWriterProvider(_WriterProvider):
    """除了本阶段契约，还按来源写越界产物（cwd / workspace）。"""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        stray_cwd: dict[str, str] | None = None,
        stray_workspace: dict[str, str] | None = None,
    ) -> None:
        super().__init__(files)
        self.stray_cwd = stray_cwd or {}
        self.stray_workspace = stray_workspace or {}

    async def send(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        cwd: str | None = None,
        context_pack: dict[str, Any] | None = None,
        extra_dirs: list[str] | None = None,
    ) -> HarnessReply:
        reply = await super().send(
            session, prompt, cwd=cwd, context_pack=context_pack, extra_dirs=extra_dirs
        )
        root = Path(str(cwd or "."))
        for name, content in self.stray_cwd.items():
            (root / name).write_text(content, encoding="utf-8")
        for extra in extra_dirs or []:
            for name, content in self.stray_workspace.items():
                (Path(extra) / name).write_text(content, encoding="utf-8")
        return reply


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
        initiative_id="ini_bound",
        thread_id="thr_bound",
        stage=stage,
        role=RoleId(role),
        brief="帮我写一个可运行贪吃蛇小游戏，支持用户登陆注册",
        artifact_dir=str(artifact_dir) if artifact_dir else "",
        harness="writer",
        upstream_artifacts=upstream or {},
        data_dir=str(data_dir),
        worktrees_dir=str(data_dir / "missions"),
    )


def _run(monkeypatch, provider: _WriterProvider, ctx: StageContext):
    monkeypatch.setattr(
        "sortie_deck.harness_providers.base.get_harness_registry",
        lambda: _Registry(provider),
    )
    return asyncio.run(HarnessStageExecutor().run(ctx))


# --- 1. 非文档阶段的上游可读性 -------------------------------------------------


def test_coding_stage_mirrors_upstream_into_workspace(tmp_path, monkeypatch):
    """编码阶段把上游契约镜像进 workspace/.upstream，prompt 指向镜像路径。"""
    art = tmp_path / "data/missions/ini_bound/artifacts/eng_implement"
    provider = _WriterProvider({"implementation.md": "# 实现", "tasks.json": "[]"})
    ctx = _ctx(
        tmp_path,
        stage="eng_implement",
        role="eng",
        artifact_dir=art,
        upstream={
            "prd.md": "# PRD v1.1（54 条）",
            "acceptance.json": '{"total": 54}',
        },
    )

    result = _run(monkeypatch, provider, ctx)

    assert result.status == "ok"
    cwd, _extra, prompt = provider.calls[0]
    workspace = Path(cwd)
    assert workspace.name == "workspace"
    # 镜像落在 cwd 内，agent 才读得到
    assert (workspace / ".upstream/prd.md").read_text(encoding="utf-8") == "# PRD v1.1（54 条）"
    assert (workspace / ".upstream/acceptance.json").exists()
    assert str(workspace / ".upstream/prd.md") in prompt
    # 权威口径写进 prompt，避免再拿 workspace 里的旧文件当依据
    assert "权威副本" in prompt
    assert "禁止作为依据" in prompt


def test_upstream_mirror_not_counted_as_impl_files(tmp_path):
    """`.upstream` 是编排层注入的镜像，不能算成 agent 写的实现文件。"""
    from sortie_deck.plugins.coding import _count_impl_files

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/app.js").write_text("console.log(1)", encoding="utf-8")
    (workspace / ".upstream").mkdir(parents=True)
    (workspace / ".upstream/prd.md").write_text("# PRD", encoding="utf-8")

    assert _count_impl_files(workspace) == 1


def test_git_diff_excludes_upstream_mirror(tmp_path, monkeypatch):
    """无 git 仓库时的文件清单不能把 .upstream / .archive 混进 diff.patch。"""
    from sortie_deck.plugins import coding

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/app.js").write_text("console.log(1)", encoding="utf-8")
    (workspace / ".upstream").mkdir(parents=True)
    (workspace / ".upstream/prd.md").write_text("# PRD", encoding="utf-8")
    (workspace / ".archive/stage-x").mkdir(parents=True)
    (workspace / ".archive/stage-x/prd.md").write_text("# 旧 PRD", encoding="utf-8")

    monkeypatch.setattr(coding, "_which", lambda name: "/usr/bin/git")
    diff = asyncio.run(coding._git_diff(workspace, tmp_path))

    assert "src/app.js" in diff
    assert ".upstream" not in diff
    assert ".archive" not in diff


# --- 2. 跨阶段遗留清理 ---------------------------------------------------------


def test_intake_sweeps_stale_contract_files_from_workspace(tmp_path, monkeypatch):
    """intake 启动时把 workspace 里的 prd.md 等遗留契约文件归档，不再盖住正式版。"""
    art = tmp_path / "data/missions/ini_bound/artifacts/intake"
    workspace = tmp_path / "data/missions/ini_bound/workspace"
    workspace.mkdir(parents=True)
    (workspace / "prd.md").write_text("# 旧版 v1.0（43 条）", encoding="utf-8")
    (workspace / "acceptance.json").write_text('{"total": 43}', encoding="utf-8")
    (workspace / "notes.txt").write_text("非契约文件保持原样", encoding="utf-8")

    provider = _WriterProvider({"intake.md": "# 集结结论"})
    ctx = _ctx(tmp_path, stage="intake", role="product", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert result.status == "ok"
    assert not (workspace / "prd.md").exists()
    assert not (workspace / "acceptance.json").exists()
    # 非契约文件不动
    assert (workspace / "notes.txt").exists()
    archived = list((workspace / ".archive").rglob("prd.md"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "# 旧版 v1.0（43 条）"
    assert sorted(result.meta["stale_moved"]) == ["acceptance.json", "prd.md"]
    # 本阶段自己的契约不进归档
    assert not list((workspace / ".archive").rglob("intake.md"))


def test_sweep_keeps_own_contract_files(tmp_path):
    """清理只搬「别的阶段」的契约文件，本阶段产物原地保留。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "prd.md").write_text("# 旧", encoding="utf-8")
    (workspace / "implementation.md").write_text("# 本轮实现", encoding="utf-8")

    moved = _sweep_stale_contracts(workspace, "eng_implement")

    assert moved == ["prd.md"]
    assert (workspace / "implementation.md").exists()


# --- 3. intake 轻契约 -----------------------------------------------------------


def test_intake_contract_is_single_intake_md():
    assert REQUIRED["intake"] == ["intake.md"]
    assert _contract_names("intake") == ["intake.md"]
    validate_stage_artifacts("intake", {"intake.md": "# 集结"})
    with pytest.raises(StageContractError):
        validate_stage_artifacts("intake", {})


def test_intake_prompt_forbids_prd_output(tmp_path, monkeypatch):
    """intake 的提示词写死唯一产出，并明确禁止写 PRD / 验收用例。"""
    art = tmp_path / "data/missions/ini_bound/artifacts/intake"
    provider = _WriterProvider({"intake.md": "# 集结"})
    ctx = _ctx(tmp_path, stage="intake", role="product", artifact_dir=art)

    _run(monkeypatch, provider, ctx)

    cwd, _extra, prompt = provider.calls[0]
    # intake 变成文档型阶段：cwd 直接是产物目录，workspace 只作只读输入
    assert Path(cwd) == art
    assert "`intake.md`" in prompt
    assert "Never write `prd.md`" in prompt
    assert "10 tool turns" in prompt
    # 边界段列出他阶段契约名，避免越界再产出
    assert "Scope guard" in prompt
    assert "`acceptance.json`" in prompt


def test_intake_missing_artifact_fails_the_stage(tmp_path, monkeypatch):
    """intake 没写 intake.md 直接判失败：不拿回复包装冒充契约产物。"""
    art = tmp_path / "data/missions/ini_bound/artifacts/intake"
    provider = _WriterProvider()  # 什么都不写
    ctx = _ctx(tmp_path, stage="intake", role="product", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert result.meta["artifact_degraded"] == ["intake.md"]
    assert result.status == "fail"
    assert "未写出契约产物" in str(result.message)
    assert "intake.md" in str(result.message)


# --- 4. 阶段收尾的越界产物清理 ---------------------------------------------------


def test_doc_stage_archives_stray_contracts_after_run(tmp_path, monkeypatch):
    """intake 运行中越界写出的 prd.md/acceptance.json 在收尾被归档。

    线上现场（ini_7463287a3371）：intake 把 PRD 写进产物目录与 workspace 各一份，
    product_prd 启动时把 workspace 那份当历史草稿镜像进 `.upstream/`，正式阶段
    读到的是集结阶段口径。启动期清理清不到「本阶段自己刚写的」，只能收尾再扫。
    """
    art = tmp_path / "data/missions/ini_bound/artifacts/intake"
    workspace = tmp_path / "data/missions/ini_bound/workspace"
    provider = _StrayWriterProvider(
        {"intake.md": "# 集结"},
        stray_cwd={"prd.md": "# 越界 PRD（产物目录）", "acceptance.json": "{}"},
        stray_workspace={"prd.md": "# 越界 PRD（workspace）", "acceptance.json": "{}"},
    )
    ctx = _ctx(tmp_path, stage="intake", role="product", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert result.status == "ok"
    # 本阶段契约原地保留，越界产物从根层消失
    assert (art / "intake.md").exists()
    assert not (art / "prd.md").exists()
    assert not (art / "acceptance.json").exists()
    assert sorted(result.meta["stray_moved"]["artifact_dir"]) == ["acceptance.json", "prd.md"]
    # workspace 根层不留契约副本（否则下一阶段会当历史草稿镜像）
    assert not (workspace / "prd.md").exists()
    assert not (workspace / "acceptance.json").exists()
    assert sorted(result.meta["stray_moved"]["workspace"]) == ["acceptance.json", "prd.md"]
    archived = list((workspace / ".archive").rglob("prd.md"))
    assert [p.read_text(encoding="utf-8") for p in archived] == ["# 越界 PRD（workspace）"]


def test_doc_stage_keeps_own_contract_but_clears_workspace_copy(tmp_path, monkeypatch):
    """product_prd 自己的 prd.md 留在产物目录；workspace 根层的旧副本仍被清走。"""
    art = tmp_path / "data/missions/ini_bound/artifacts/product_prd"
    workspace = tmp_path / "data/missions/ini_bound/workspace"
    workspace.mkdir(parents=True)
    (workspace / "intake.md").write_text("# 上一阶段越界残留", encoding="utf-8")
    provider = _StrayWriterProvider(
        {"prd.md": "# 正式 PRD", "acceptance.json": "[]"},
        stray_workspace={"prd.md": "# 顺手写的副本"},
    )
    ctx = _ctx(tmp_path, stage="product_prd", role="product", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert (art / "prd.md").read_text(encoding="utf-8") == "# 正式 PRD"
    assert (art / "acceptance.json").exists()
    # workspace 根层契约副本（本阶段同名 + 上一阶段残留）全部归档
    assert not (workspace / "prd.md").exists()
    assert not (workspace / "intake.md").exists()
    # 上一阶段残留走启动期清理，本阶段自己写的副本走收尾清理
    assert result.meta["stale_moved"] == ["intake.md"]
    assert result.meta["stray_moved"]["workspace"] == ["prd.md"]
    assert "artifact_dir" not in result.meta["stray_moved"]


def test_coding_stage_leaves_workspace_files_alone(tmp_path, monkeypatch):
    """编码阶段 workspace 就是仓库本体，收尾不做契约清理，避免误搬仓库文件。"""
    art = tmp_path / "data/missions/ini_bound/artifacts/eng_implement"
    workspace = tmp_path / "data/missions/ini_bound/workspace"
    provider = _StrayWriterProvider(
        {"implementation.md": "# 实现", "tasks.json": "[]", "prd.md": "# 仓库自带"}
    )
    ctx = _ctx(tmp_path, stage="eng_implement", role="eng", artifact_dir=art)

    result = _run(monkeypatch, provider, ctx)

    assert (workspace / "prd.md").read_text(encoding="utf-8") == "# 仓库自带"
    assert result.meta["stray_moved"] == {}
