from __future__ import annotations

from pathlib import Path

from sortie_deck.repositories.protocol import InitiativeRepository
from sortie_deck.repositories.sqlite_initiatives import SqliteInitiativeStore

SUPPORTED_BACKENDS = frozenset({"sqlite", "sql"})


def build_initiative_store(
    *,
    storage: str,
    json_path: Path,
    sqlite_path: Path,
) -> InitiativeRepository:
    """构建任务仓储。JSON 后端已移除，只会返回 sqlite 实现。

    ``json_path`` 仅为兼容既有调用方签名保留：它不再是可选后端，而是迁移源
    （见 ``Orchestrator._migrate_storage_once``）。
    """
    backend = (storage or "").strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"不支持的存储后端 {storage!r}：JSON 存储已废弃（整文件读-改-写，"
            "多任务并发会互相覆盖）。请删除 TDT_STORAGE 或设为 sqlite；"
            f"旧的 {json_path.name} 会在首次启动时自动导入。"
        )
    return SqliteInitiativeStore(sqlite_path)
