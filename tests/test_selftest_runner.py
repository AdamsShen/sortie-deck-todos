"""自测 runner：一次起服务、跑全量用例、结构化结果、失败码透传。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from sortie_deck.harness_providers.executor import (
    HarnessStageExecutor,
    _ensure_selftest_runner,
)
from sortie_deck.models import RoleId, StageContext


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _ensure_selftest_runner(ws, "eng_web_selftest")
    assert (ws / "scripts" / "selftest_runner.sh").is_file()
    return ws


def _run(ws: Path, config: dict[str, str], timeout: str = "3") -> subprocess.CompletedProcess:
    (ws / "selftest.config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    env = {**os.environ, "SELFTEST_TIMEOUT": timeout}
    return subprocess.run(
        ["bash", "scripts/selftest_runner.sh"],
        cwd=ws,
        capture_output=True,
        text=True,
        env=env,
        timeout=40,
    )


def _result(ws: Path) -> dict:
    return json.loads((ws / "selftest_result.json").read_text(encoding="utf-8"))


def test_runner_passes_without_server(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    proc = _run(ws, {"test_command": "true"})
    assert proc.returncode == 0
    assert _result(ws)["status"] == "passed"


def test_runner_propagates_failure_exit_code(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    proc = _run(ws, {"test_command": "exit 3"})
    assert proc.returncode == 3
    data = _result(ws)
    assert data["status"] == "failed"
    assert data["exit_code"] == 3
    assert data["next"]


def test_runner_reports_start_failed(tmp_path: Path) -> None:
    """服务起不来时快速失败，而不是让模型自己 ps/lsof 排查。"""
    ws = _workspace(tmp_path)
    proc = _run(
        ws,
        {
            "start_command": "exit 1",
            "health_url": "http://127.0.0.1:1/health",
            "test_command": "true",
        },
        timeout="3",
    )
    assert proc.returncode == 2
    assert _result(ws)["status"] == "start_failed"


def test_runner_cleans_up_service_process_group(tmp_path: Path) -> None:
    """服务进程（含 fork 出的子进程）必须随 runner 退出，不能残留占端口。"""
    import socket
    import time

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    ws = _workspace(tmp_path)
    proc = _run(
        ws,
        {
            "start_command": f"python3 -m http.server {port}",
            "health_url": f"http://127.0.0.1:{port}/",
            "test_command": "true",
        },
        timeout="10",
    )
    assert proc.returncode == 0
    time.sleep(0.5)

    # 端口已无监听：connect 被拒绝说明服务确实被清掉了
    # （不用 bind 判定：TIME_WAIT 会让 bind 失败，误判成「进程还在」）
    check = socket.socket()
    check.settimeout(0.5)
    try:
        alive = check.connect_ex(("127.0.0.1", port)) == 0
    finally:
        check.close()
    assert not alive, "自测服务未被清理，端口仍在监听"


def test_runner_requires_config(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    proc = subprocess.run(
        ["bash", "scripts/selftest_runner.sh"],
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert _result(ws)["status"] == "missing_config"


def test_runner_uses_project_script_when_present(tmp_path: Path) -> None:
    """项目自带 scripts/selftest_runner.sh 时不能被模板覆盖。"""
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    custom = ws / "scripts" / "selftest_runner.sh"
    custom.write_text("#!/usr/bin/env bash\necho custom\n", encoding="utf-8")

    _ensure_selftest_runner(ws, "eng_backend_selftest")

    assert custom.read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho custom\n"


def _ctx(tmp_path: Path) -> StageContext:
    return StageContext(
        initiative_id="ini_test",
        thread_id="thr_test",
        stage="eng_web_selftest",
        role=RoleId.ENG_WEB,
        brief="贪吃蛇",
        artifact_dir=str(tmp_path / "artifacts"),
    )


def test_prompt_switches_to_runner_rules(tmp_path: Path) -> None:
    """注入 runner 后，自测 prompt 必须改成「只跑脚本」，不再逐条重跑命令。"""
    ws = _workspace(tmp_path)
    prompt = HarnessStageExecutor()._build_prompt(_ctx(tmp_path), ws, {"briefs": {}}, "")
    assert "Verification runner" in prompt
    assert "scripts/selftest_runner.sh" in prompt
    assert "pkill" in prompt


def test_prompt_falls_back_without_runner(tmp_path: Path) -> None:
    """没有 runner（模板缺失 / 老任务）时回到逐条验证，不能要求跑不存在的脚本。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "checklist.md").write_text("- [ ] C1 smoke — verify: `true`", encoding="utf-8")

    prompt = HarnessStageExecutor()._build_prompt(_ctx(tmp_path), ws, {"briefs": {}}, "")
    assert "Verification checklist" in prompt
    assert "Verification runner" not in prompt
