"""规划会话并发安全：一任务一目录后，多用户同时建任务不能再互相覆盖。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from sortie_deck import mission_input
from sortie_deck.mission_briefing import BriefingMessage, PlannerSessionStore
from sortie_deck.models import ExternalMaterial
from sortie_deck.settings import Settings


def _cfg(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        pipeline_llm=False,
        llm_cli_fallback=False,
        default_coding_executor="mock",
    )


def _run_threads(target, args_list: list[tuple]) -> list[BaseException]:
    """并发执行并把线程内异常带回主线程断言（避免静默失败）。"""
    errors: list[BaseException] = []

    def _wrap(args: tuple) -> None:
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 — 线程异常必须带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=_wrap, args=(args,)) for args in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_sessions_are_isolated(tmp_path: Path):
    store = PlannerSessionStore(_cfg(tmp_path))
    created: list[str] = []
    lock = threading.Lock()

    def _start(idx: int) -> None:
        sess = store.create(owner_id=f"usr_{idx}")
        with lock:
            created.append(sess.id)

    errors = _run_threads(_start, [(i,) for i in range(20)])

    assert not errors, errors
    assert len(set(created)) == 20
    for mission_id in created:
        state = json.loads(
            (tmp_path / "data" / "missions" / mission_id / "chat" / "planner.state.json").read_text(
                encoding="utf-8"
            )
        )
        assert state["mission_id"] == mission_id
        assert state["id"] == mission_id
        assert state["owner_id"].startswith("usr_")
    # 旧全局文件不得被创建
    assert not (tmp_path / "data" / "briefings.json").exists()


def test_same_session_concurrent_messages_are_all_persisted(tmp_path: Path):
    store = PlannerSessionStore(_cfg(tmp_path))
    sess = store.create(owner_id="usr_a")

    def _append(idx: int) -> None:
        current = store.get(sess.id)
        current.messages.append(BriefingMessage(role="user", text=f"m{idx}"))
        store.save(current)

    errors = _run_threads(_append, [(i,) for i in range(10)])

    assert not errors, errors
    path = tmp_path / "data" / "missions" / sess.id / "chat" / "planner.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 10
    assert {row["text"] for row in rows} == {f"m{i}" for i in range(10)}


def test_materials_do_not_leak_across_missions(tmp_path: Path):
    store = PlannerSessionStore(_cfg(tmp_path))
    missions = [store.create(owner_id=f"usr_{i}") for i in range(5)]

    for idx, sess in enumerate(missions):
        # 资料清单由 input/manifest.json 承载（store 不存副本）
        mission_input.upsert_materials(
            tmp_path / "data",
            sess.id,
            [ExternalMaterial(id=f"mat_{idx}", name=f"prd_{idx}.md", kind="prd", pinned=True)],
        )

    for idx, sess in enumerate(missions):
        manifest = json.loads(
            (
                tmp_path / "data" / "missions" / sess.id / "input" / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert [row["name"] for row in manifest["files"]] == [f"prd_{idx}.md"]


def test_sessions_of_different_users_are_owned_separately(tmp_path: Path):
    """owner 落在各自 state 里：并发创建不会串号（API 层据此做 403 校验）。"""
    store = PlannerSessionStore(_cfg(tmp_path))
    owners: dict[str, str] = {}
    lock = threading.Lock()

    def _start(idx: int) -> None:
        sess = store.create(owner_id=f"usr_{idx}")
        with lock:
            owners[sess.id] = sess.owner_id

    errors = _run_threads(_start, [(i,) for i in range(8)])

    assert not errors, errors
    assert sorted(owners.values()) == [f"usr_{i}" for i in range(8)]
    for mission_id, owner in owners.items():
        assert store.get(mission_id).owner_id == owner
