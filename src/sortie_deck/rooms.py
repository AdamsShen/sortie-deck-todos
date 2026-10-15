from __future__ import annotations

import json
import re
from pathlib import Path

from sortie_deck.models import RoomMessage

MAIN_ROOM = "main"

_UNSAFE_ROOM_CHARS = re.compile(r"[^A-Za-z0-9_.:-]+")


def room_file_key(room_key: str) -> str:
    """Filesystem-safe suffix for a room key (``main`` keeps the legacy name)."""
    key = (room_key or MAIN_ROOM).strip() or MAIN_ROOM
    return _UNSAFE_ROOM_CHARS.sub("_", key)[:120]


class RoomStore:
    """Per-initiative discussion rooms (Matrix-room inspired, local files).

    The big room keeps its historic file (``<initiative>.jsonl``). Sub-rooms get
    their own file (``<initiative>__<room_key>.jsonl``) so their transcript never
    leaks into the main timeline — only the conclusion travels back.

    layout="mission"（任务目录收敛）时 root 传 missions 根，文件落在
    ``<root>/<initiative_id>/chat/`` 下；layout="flat" 保持历史布局 ``<root>/``。
    """

    def __init__(self, root: Path, *, layout: str = "flat") -> None:
        self.root = root
        self.layout = "mission" if layout == "mission" else "flat"
        self.root.mkdir(parents=True, exist_ok=True)

    def _chat_dir(self, initiative_id: str) -> Path:
        base = self.root / initiative_id
        if self.layout == "mission":
            base = base / "chat"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _path(self, initiative_id: str, room_key: str = MAIN_ROOM) -> Path:
        suffix = room_file_key(room_key)
        if suffix == MAIN_ROOM:
            return self._chat_dir(initiative_id) / f"{initiative_id}.jsonl"
        return self._chat_dir(initiative_id) / f"{initiative_id}__{suffix}.jsonl"

    def append(self, message: RoomMessage) -> RoomMessage:
        path = self._path(message.initiative_id, message.room_key)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(message.model_dump_json() + "\n")
        return message

    def list(
        self,
        initiative_id: str,
        limit: int = 500,
        *,
        room_key: str = MAIN_ROOM,
        all_rooms: bool = False,
    ) -> list[RoomMessage]:
        if all_rooms:
            msgs: list[RoomMessage] = []
            for key in self.list_rooms(initiative_id):
                msgs.extend(self.list(initiative_id, limit=limit, room_key=key))
            return sorted(msgs, key=lambda m: m.at)
        path = self._path(initiative_id, room_key)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        msgs = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            msgs.append(RoomMessage.model_validate(json.loads(line)))
        return msgs

    def list_rooms(self, initiative_id: str) -> list[str]:
        """Room keys that hold at least one message (``main`` first)."""
        keys: list[str] = []
        if self._path(initiative_id).exists():
            keys.append(MAIN_ROOM)
        prefix = f"{initiative_id}__"
        for path in sorted(self._chat_dir(initiative_id).glob(f"{prefix}*.jsonl")):
            keys.append(path.stem[len(prefix) :])
        return keys

    def clear(self, initiative_id: str, room_key: str = MAIN_ROOM) -> None:
        path = self._path(initiative_id, room_key)
        if path.exists():
            path.unlink()
