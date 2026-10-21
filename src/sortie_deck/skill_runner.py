"""skill 脚本执行沙箱（进程级隔离）。

安全模型：
- 开关 ``settings.skill_run_enabled``（默认开）；
- 仅 ``executable=True`` 的 skill 条目可执行（与 loadout.probe 语义一致）；
- 只允许执行条目 ``scripts`` 里登记过的 path（精确匹配，杜绝任意路径）；
- 脚本内容来自工具箱存储（模型只见摘要，接触不到也不需要读源码），
  执行前落盘到任务工作区 ``<workspace>/.tdt_skills/<item_id>/``；
- 独立子进程执行 + 超时 kill（``settings.skill_run_timeout_seconds``），
  即"沙箱"边界；网络/文件系统白名单属 P2 范围（见实施计划备选 3）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from sortie_deck.loadout import SKILL_OUTPUT_FLOAT_LIMIT
from sortie_deck.toolkit import SkillScript, ToolkitItem

# language → 解释器命令；不在此表的语言一律拒绝（宁可不跑，不可乱跑）
_INTERPRETERS: dict[str, list[str]] = {
    "py": [sys.executable],
    "python": [sys.executable],
    "sh": ["bash"],
    "bash": ["bash"],
    "js": ["node"],
    "javascript": ["node"],
}


def _validate(item: ToolkitItem, script_path: str) -> tuple[SkillScript | None, str]:
    """校验一次脚本执行请求，返回 (脚本, 拒绝原因)；通过时原因为空串。"""
    if item.kind != "skill" or not item.enabled:
        return None, f"`{item.id}` is not an enabled skill"
    if not item.executable:
        return None, "skill is not marked executable — prompt text only"
    # 精确匹配登记路径（统一斜杠与前导 ./），禁止目录穿越与任意命令
    want = str(script_path or "").replace("\\", "/").lstrip("./")
    for script in item.scripts:
        if script.path == want:
            interpreter = _INTERPRETERS.get((script.language or "").lower())
            if interpreter is None:
                return None, f"unsupported script language `{script.language}`"
            return script, ""
    return None, f"script `{script_path}` is not registered on this skill"


async def run_skill_script(
    item: ToolkitItem,
    script_path: str,
    args: list[Any] | None = None,
    *,
    workspace_dir: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """执行一个 skill 条目登记过的脚本，返回结构化结果（永不抛异常）。

    输入: item（skill 条目）、script_path（item.scripts 里登记的 path）、
          args（传给脚本的命令行参数，逐个 str()）。
    输出: {"ok": bool, "exit_code": int|None, "timed_out": bool,
           "stdout": str(≤8000 字符), "stderr": str(≤8000 字符),
           "duration_ms": int, "reason": str(仅失败校验时有)}
    """
    from sortie_deck.settings import settings as _settings

    if not bool(getattr(_settings, "skill_run_enabled", False)):
        return {"ok": False, "reason": "skill script execution is disabled (skill_run_enabled)"}
    script, reject_reason = _validate(item, script_path)
    if script is None:
        return {"ok": False, "reason": reject_reason}

    timeout = float(timeout or getattr(_settings, "skill_run_timeout_seconds", 30.0))
    base = Path(workspace_dir or ".") / ".tdt_skills" / item.id
    # 落盘该 skill 的全部脚本（脚本之间可能互相 import），只执行被点名的那个
    for entry in item.scripts:
        target = base / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.content or "", encoding="utf-8")

    cmd = _INTERPRETERS[(script.language or "").lower()]
    started = time.monotonic()
    env = {**os.environ, **(item.env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            str(base / script.path),
            *[str(a) for a in (args or [])],
            cwd=str(base),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {
            "ok": False,
            "exit_code": None,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "reason": f"failed to spawn interpreter: {exc}",
        }

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)

    def _decode(raw: bytes | None) -> str:
        text = (raw or b"").decode("utf-8", errors="replace")
        if len(text) > SKILL_OUTPUT_FLOAT_LIMIT:
            text = text[:SKILL_OUTPUT_FLOAT_LIMIT] + "\n… [output truncated]"
        return text

    return {
        "ok": (not timed_out) and proc.returncode == 0,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "stdout": _decode(stdout_b),
        "stderr": _decode(stderr_b),
        "duration_ms": duration_ms,
    }
