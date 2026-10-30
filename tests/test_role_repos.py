from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from sortie_deck.agents import AgentCatalog
from sortie_deck.mission_briefing import MissionBriefingAgent
from sortie_deck.models import CreateInitiativeRequest, Initiative, RepoConfig
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.plugins.coding import _git_diff, _repo_dirname, clone_repos
from sortie_deck.settings import Settings


def test_create_initiative_role_repos(tmp_path):
    """Mission role_repos land on RoleAgent.repos, ini.meta and pipeline loadout."""
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    agents = AgentCatalog(tmp_path / "agents.json")
    orch = Orchestrator(cfg)

    async def _run():
        await orch.startup()
        try:
            ini = await orch.create_initiative(
                CreateInitiativeRequest(
                    title="Multi-repo dev",
                    brief="Server + web repos for dev agents",
                    role_repos={
                        "eng_backend": [
                            {"url": "https://github.com/acme/server.git", "branch": "main"},
                        ],
                        "eng_web": [
                            {"url": "https://github.com/acme/web.git"},
                            {"url": "https://github.com/acme/admin.git", "branch": "dev"},
                        ],
                    },
                    creator_name="Ada",
                )
            )
            backend = next(r for r in ini.roles if r.role.value == "eng_backend")
            web = next(r for r in ini.roles if r.role.value == "eng_web")
            assert [r.url for r in backend.repos] == ["https://github.com/acme/server.git"]
            assert backend.repos[0].branch == "main"
            assert len(web.repos) == 2 and web.repos[1].branch == "dev"

            meta_repos = ini.meta.get("role_repos") or {}
            assert meta_repos["eng_backend"][0]["branch"] == "main"

            loadout = orch._pipeline_loadout(ini)
            assert loadout["role_repos"]["eng_web"][1]["branch"] == "dev"
            # Roles without repos stay empty
            product = next(r for r in ini.roles if r.role.value == "product")
            assert product.repos == []
            assert "product" not in loadout["role_repos"]
        finally:
            await orch.shutdown()

    asyncio.run(_run())


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_clone_repos_reads_code_before_iteration(tmp_path):
    """clone_repos checks out each configured repo under workspace/repos."""
    src = tmp_path / "src_repo"
    src.mkdir()
    _git(src, "init", "-b", "main")
    (src / "README.md").write_text("# server repo\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    repos = [
        RepoConfig(url=str(src), branch="main"),
        RepoConfig(url=str(src), name="aliased"),
    ]

    async def _run():
        section, log = await clone_repos(repos, workspace)

    asyncio.run(_run())
    assert (workspace / "repos" / "src_repo" / ".git").exists()
    assert (workspace / "repos" / "aliased" / ".git").exists()
    assert (workspace / "repos" / "src_repo" / "README.md").is_file()

    # dir naming helpers
    assert _repo_dirname(RepoConfig(url="https://github.com/acme/web.git")) == "web"
    assert _repo_dirname(RepoConfig(url="https://github.com/acme/web")) == "web"
    assert _repo_dirname(RepoConfig(url="https://github.com/acme/web.git", name="x")) == "x"

    # edits inside the cloned repo show up in _git_diff
    (workspace / "repos" / "src_repo" / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    _git(workspace / "repos" / "src_repo", "add", ".")

    async def _run2():
        diff = await _git_diff(workspace, workspace, repos)
        assert "feature.py" in diff

    asyncio.run(_run2())


def test_briefing_role_repos_pass_to_create(tmp_path: Path):
    """set_role_repos on the briefing draft flows into CreateInitiativeRequest."""
    cfg = Settings(
        data_dir=tmp_path / "data",
        pipeline_llm=False,
        llm_cli_fallback=False,
        default_coding_executor="mock",
    )
    created: list[Initiative] = []
    captured: list[CreateInitiativeRequest] = []

    async def create(req: CreateInitiativeRequest, *, initiative_id: str | None = None) -> Initiative:
        captured.append(req)
        ini = Initiative(
            id=initiative_id or "ini_fallback", title=req.title, brief=req.brief, template="default"
        )
        created.append(ini)
        return ini

    agents = AgentCatalog(cfg.agents_db)
    agents._write([])  # type: ignore[attr-defined]

    agent = MissionBriefingAgent(
        settings=cfg,
        agents=agents,
        create_initiative=create,
        list_initiatives=lambda: created,
        save_initiative=lambda ini: created.__setitem__(0, ini) if created else None,
    )

    async def _run():
        sess = agent.start()
        sess = await agent.handle(sess.id, "服务端与前端联调开发", user_name="Ada")
        assert sess.status == "awaiting_confirm"
        sess = agent.set_role_repos(
            sess.id,
            {
                "eng_backend": [{"url": "https://github.com/acme/server.git", "branch": "main"}],
                "eng_web": [{"url": "", "branch": "x"}],  # empty url dropped
            },
        )
        assert sess.draft["role_repos"]["eng_backend"][0]["branch"] == "main"
        assert "eng_web" not in sess.draft["role_repos"]
        sess = await agent.handle(sess.id, "确认", user_name="Ada")
        assert sess.status == "done"
        assert captured
        rr = captured[0].role_repos
        assert rr["eng_backend"][0].url == "https://github.com/acme/server.git"
        assert rr["eng_backend"][0].branch == "main"

    asyncio.run(_run())
