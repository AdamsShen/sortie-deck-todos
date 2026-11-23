"""Durable verification evidence for interrupted DSH builds."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FILES = ("implementation.md", "tasks.json", "checklist.md", "selftest_log.json")
_IGNORED_DIRS = {".git", ".shadow.git", ".upstream", ".archive", "node_modules", "__pycache__"}
_CHECKED_ITEM = re.compile(r"^\s*- \[[xX]\] (\w+) .*?verify:\s*`([^`]+)`", re.MULTILINE)


def _raise_walk_error(error: OSError) -> None:
    raise error


class BuildCheckpoint:
    def __init__(self, path: Path, workspace: Path, scope: str) -> None:
        self.path = path
        self.workspace = workspace
        self.scope = scope
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.saved = {}
        if isinstance(saved, dict) and saved.get("scope") == scope:
            fields = (saved.get("evidence"), saved.get("outputs"))
            if all(
                isinstance(field, dict) and all(isinstance(v, dict) for v in field.values())
                for field in fields
            ):
                self.saved = saved
        self.files = self._files()
        self.evidence = self.saved.get("evidence", {})
        self.outputs = self.saved.get("outputs", {})

    def _files(self) -> dict[str, str]:
        files = {}
        for name in _FILES:
            path = self.workspace / name
            try:
                if path.is_symlink():
                    continue
                content = path.read_text(encoding="utf-8")
                if content.strip():
                    if name.endswith(".json"):
                        json.loads(content)
                    files[name] = content
            except (OSError, ValueError):
                continue
        return files

    def _source_version(self) -> str:
        digest = hashlib.sha256()
        for root, dirs, files in os.walk(self.workspace, onerror=_raise_walk_error):
            dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS)
            for name in sorted(files):
                path = Path(root) / name
                relative = str(path.relative_to(self.workspace))
                if relative in (*_FILES, "diff.patch"):
                    continue
                stat = path.lstat()
                digest.update(
                    repr((relative, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)).encode()
                )
        return digest.hexdigest()

    @staticmethod
    def _checks(data: dict[str, str]) -> dict[str, dict[str, Any]]:
        rows = json.loads(data.get("selftest_log.json", "[]"))
        if not isinstance(rows, list):
            return {}
        return {
            row["item"]: row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("item"), str)
        }

    def capture(self) -> None:
        """Called between tools, before a later edit can change the verified source."""
        files = self._files()
        if files == self.files:
            return
        try:
            version = self._source_version()
            previous = self._checks(self.files)
            current = self._checks(files)
            evidence = {}
            for item, row in current.items():
                if type(row.get("exit_code")) is int and row["exit_code"] == 0:
                    if row != previous.get(item):
                        evidence[item] = {"row": row, "version": version}
                    elif item in self.evidence:
                        evidence[item] = self.evidence[item]
            outputs = {}
            for name, content in files.items():
                if content != self.files.get(name):
                    outputs[name] = {"text": content, "version": version}
                elif name in self.outputs:
                    outputs[name] = self.outputs[name]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"scope": self.scope, "evidence": evidence, "outputs": outputs}),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            self.files, self.evidence, self.outputs = files, evidence, outputs
        except OSError:
            logger.warning("Build checkpoint capture failed: %s", self.path, exc_info=True)

    def resume(self) -> tuple[set[str], list[str]]:
        if not self.saved:
            return set(), []
        try:
            version = self._source_version()
        except OSError:
            return set(), []
        files = self._files()
        rows = self._checks(files)
        reusable = []
        for item, command in _CHECKED_ITEM.findall(files.get("checklist.md", "")):
            evidence = self.evidence.get(item) or {}
            row = rows.get(item, {})
            if (
                evidence.get("version") == version
                and evidence.get("row") == row
                and row.get("command") == command
            ):
                reusable.append(item)
        outputs = {
            name
            for name, output in self.outputs.items()
            if name in files
            and output.get("version") == version
            and files[name] == output.get("text")
        }
        return outputs, reusable

    def finish(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Build checkpoint cleanup failed: %s", self.path, exc_info=True)
