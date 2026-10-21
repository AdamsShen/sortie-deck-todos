"""Mission-bound memory — per-initiative files that survive sessions.

Layout (``TDT_DATA_DIR/missions/<initiative_id>/``)::

    memory/memory.md            rolling summary (auto-compacted, LLM-written)
    memory/sessions/<sid>.jsonl one file per harness session (full turns)
    memory/context/<name>.md    requirement-bound files (prd.md, plan.md, ...)

A new session bootstraps from ``bootstrap_context()``: the rolling summary +
every context file + the artifact index, trimmed to
``TDT_MISSION_CONTEXT_TOKEN_LIMIT``. When the pack exceeds the budget, the
rolling summary is LLM-summarised and written back (``maybe_summarize``).
Everything is plain files — restart-safe, harness-agnostic, and shared by all
providers (mem0/RAG layer on top, see ``memory.py`` / ``knowledge_gateway``).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sortie_deck.models import Initiative, new_id, utc_now

logger = logging.getLogger(__name__)

# 协调/规划类角色值：滚动总结走 LLM 提炼要点；其余执行类角色降级为增量流水
_COORDINATOR_ROLES = frozenset({"product", "ops"})

# Rough token estimate: ~4 chars per token for mixed CJK/latin text.
_CHARS_PER_TOKEN = 4

_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
# Context filenames keep dots (``prd.md``); session ids never do.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


class ContextPack(BaseModel):
    """What a *new* harness session gets injected on bootstrap."""

    initiative_id: str
    memory_md: str = ""
    context_files: dict[str, str] = Field(default_factory=dict)
    artifact_index: list[str] = Field(default_factory=list)
    session_log: list[dict[str, Any]] = Field(default_factory=list)
    token_estimate: int = 0
    summarized: bool = False

    def render(self, *, budget: int | None = None) -> str:
        """One markdown blob ready to prepend to a harness prompt."""
        sections: list[str] = ["# Mission context pack (loaded from bound files)"]
        if self.memory_md.strip():
            sections.append(f"## Rolling memory\n\n{self.memory_md.strip()}")
        for name, content in self.context_files.items():
            sections.append(f"## {name}\n\n{content.strip()}")
        if self.artifact_index:
            sections.append(
                "## Artifact index\n\n"
                + "\n".join(f"- `{p}`" for p in self.artifact_index)
            )
        blob = "\n\n".join(sections)
        limit = budget or 0
        if limit > 0 and estimate_tokens(blob) > limit:
            blob = blob[: limit * _CHARS_PER_TOKEN - 3].rstrip() + "..."
        return blob


class MissionMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths -------------------------------------------------------------

    def mission_dir(self, initiative_id: str) -> Path:
        return self.root / initiative_id

    def memory_dir(self, initiative_id: str) -> Path:
        return self.mission_dir(initiative_id) / "memory"

    def sessions_dir(self, initiative_id: str) -> Path:
        return self.memory_dir(initiative_id) / "sessions"

    def context_dir(self, initiative_id: str) -> Path:
        return self.memory_dir(initiative_id) / "context"

    def memory_file(self, initiative_id: str) -> Path:
        return self.memory_dir(initiative_id) / "memory.md"

    def entries_dir(self, initiative_id: str) -> Path:
        """单条记忆全文目录：Agent 按 MEMORY_INDEX.md 中的 id 按需读取。"""
        return self.memory_dir(initiative_id) / "entries"

    def usage_log(self, initiative_id: str) -> Path:
        """记忆使用记录（best-effort）：Agent 取用全文后追加一行 id。"""
        return self.memory_dir(initiative_id) / "usage.log"

    # --- rolling summary ----------------------------------------------------

    def load_memory_md(self, initiative_id: str) -> str:
        path = self.memory_file(initiative_id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def append_memory(self, initiative_id: str, text: str) -> None:
        """Append one dated entry to the rolling summary."""
        text = (text or "").strip()
        if not text:
            return
        path = self.memory_file(initiative_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {utc_now().isoformat(timespec='seconds')}\n{text}\n")

    def write_memory_md(self, initiative_id: str, content: str) -> None:
        path = self.memory_file(initiative_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # --- context files -------------------------------------------------------

    def bind_context_file(self, initiative_id: str, name: str, content: str) -> Path:
        """Persist a requirement-bound file (``prd.md``, ``plan.md``, ...)."""
        safe = _SAFE_NAME_RE.sub("", name).strip(".") or "note.md"
        if not safe.endswith(".md"):
            safe = f"{safe}.md"
        path = self.context_dir(initiative_id) / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content or "", encoding="utf-8")
        return path

    def list_context_files(self, initiative_id: str) -> dict[str, str]:
        cdir = self.context_dir(initiative_id)
        if not cdir.exists():
            return {}
        return {
            p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(cdir.glob("*.md"))
        }

    # --- session transcripts ---------------------------------------------------

    def record_turn(
        self,
        initiative_id: str,
        session_id: str,
        *,
        stage: str,
        role: str,
        prompt: str,
        reply: str,
        provider: str,
    ) -> Path:
        safe = _SESSION_ID_RE.sub("_", session_id) or new_id("sess_")
        path = self.sessions_dir(initiative_id) / f"{safe}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": utc_now().isoformat(),
            "stage": stage,
            "role": role,
            "provider": provider,
            "prompt_chars": len(prompt or ""),
            "reply_chars": len(reply or ""),
            "prompt_head": (prompt or "")[:2000],
            "reply_head": (reply or "")[:2000],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def load_session_log(
        self, initiative_id: str, *, limit_turns: int = 20
    ) -> list[dict[str, Any]]:
        sdir = self.sessions_dir(initiative_id)
        if not sdir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(sdir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows[-limit_turns:]

    # --- bootstrap / budget ------------------------------------------------------

    def token_usage(self, initiative_id: str) -> int:
        total = len(self.load_memory_md(initiative_id))
        for content in self.list_context_files(initiative_id).values():
            total += len(content)
        return estimate_tokens(total)

    async def bootstrap_context(
        self,
        initiative: Initiative,
        *,
        artifact_paths: list[str] | None = None,
        budget: int | None = None,
    ) -> ContextPack:
        """Everything a fresh session needs: summary + bound files + artifacts."""
        from sortie_deck.settings import settings

        limit = int(budget or settings.mission_context_token_limit)
        pack = ContextPack(
            initiative_id=initiative.id,
            memory_md=self.load_memory_md(initiative.id),
            context_files=self.list_context_files(initiative.id),
            artifact_index=list(artifact_paths or []),
            session_log=self.load_session_log(initiative.id, limit_turns=10),
        )
        pack.token_estimate = estimate_tokens(pack.render())
        # Over budget → compact the rolling summary once, then rebuild.
        if pack.token_estimate > limit and self._summary_beyond_files(pack, limit):
            summarized = await self.summarize(initiative)
            pack = ContextPack(
                initiative_id=initiative.id,
                memory_md=self.load_memory_md(initiative.id),
                context_files=self.list_context_files(initiative.id),
                artifact_index=list(artifact_paths or []),
                token_estimate=0,
                summarized=bool(summarized),
            )
            pack.token_estimate = estimate_tokens(pack.render())
        return pack

    def _summary_beyond_files(self, pack: ContextPack, limit: int) -> bool:
        files_tokens = estimate_tokens("\n".join(pack.context_files.values()))
        return estimate_tokens(pack.memory_md) > max(200, limit - files_tokens)

    async def maybe_summarize(self, initiative_id: str) -> str | None:
        """Compact the rolling summary when it alone exceeds the token budget."""
        from sortie_deck.settings import settings

        limit = int(settings.mission_context_token_limit)
        memory_md = self.load_memory_md(initiative_id)
        if estimate_tokens(memory_md) <= limit:
            return None
        return await self.summarize_from_text(initiative_id, memory_md)

    async def summarize(self, initiative: Initiative) -> str | None:
        return await self.summarize_from_text(initiative.id, self.load_memory_md(initiative.id))

    async def summarize_from_text(self, initiative_id: str, memory_md: str) -> str | None:
        """LLM-summarise ``memory_md`` and write the summary back. Degrades to trim."""
        from sortie_deck.llm import LlmClient
        from sortie_deck.settings import settings

        summary = ""
        try:
            client = LlmClient(cli_fallback=bool(settings.llm_cli_fallback))
            summary = await client.chat_text(
                system=(
                    "你是任务记忆压缩器。把给定滚动记忆压缩为要点式 markdown 摘要，"
                    "保留：目标、已做决策、关键结论、未完成事项、风险。不新增内容。"
                ),
                user=memory_md,
                temperature=0.1,
            )
        except Exception:  # noqa: BLE001 — summarization is best-effort
            summary = ""
        if not summary.strip():
            # Degraded compaction: keep the head plus the most recent entries.
            entries = memory_md.split("\n## ")
            head = entries[0].strip()
            keep = entries[-4:] if len(entries) > 5 else entries[1:]
            summary = "\n## ".join([head, *keep])[
                : settings.mission_context_token_limit * _CHARS_PER_TOKEN
            ]
        self.write_memory_md(
            initiative_id,
            f"# Rolling memory (auto-summarized {utc_now().isoformat(timespec='seconds')})\n\n{summary.strip()}\n",
        )
        return summary.strip() or None


_STORE: MissionMemoryStore | None = None


def get_mission_memory() -> MissionMemoryStore:
    global _STORE
    if _STORE is None:
        from sortie_deck.settings import settings

        _STORE = MissionMemoryStore(settings.missions_dir)
    return _STORE


def reset_mission_memory() -> None:
    global _STORE
    _STORE = None


_SCOPE_LABEL = {"workspace": "全局约定", "user": "我的", "mission": "任务", "agent": "Agent"}


def write_memory_pack(
    mem_store: Any,
    *,
    initiative_id: str,
    user_id: str | None = None,
    agent_ids: list[str] | None = None,
) -> None:
    """把共享记忆「索引 + 单条全文」落进任务目录（Claude Code MEMORY.md 模式）。

    memory/MEMORY_INDEX.md   简介索引，Agent 读它决定加载哪条
    memory/entries/<id>.md   单条全文，Agent 按需读取
    memory/usage.log         取用全文后追加 id（best-effort 使用记录）

    任何失败静默降级：记忆包只是增强，不能阻塞任务启动。
    """
    try:
        entries = mem_store.context_candidates(
            user_id=user_id,
            mission_id=initiative_id,
            agent_ids=agent_ids,
            limit=40,
        )
    except Exception:  # noqa: BLE001 — memory must never break a stage run
        return
    try:
        mdir = get_mission_memory().memory_dir(initiative_id)
        edir = get_mission_memory().entries_dir(initiative_id)
        edir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Memory Index",
            "",
            "需要某条记忆的完整内容时，读取 memory/entries/<id>.md；",
            "取用后执行 `echo <id> >> memory/usage.log` 记录使用（best-effort）。",
            "",
        ]
        keep: set[str] = set()
        for e in entries:
            keep.add(e.id)
            label = _SCOPE_LABEL.get(e.scope, e.scope)
            lines.append(f"- {e.id} | {label} / {e.kind} | {e.display_summary()}")
            try:
                (edir / f"{e.id}.md").write_text(e.content, encoding="utf-8")
            except OSError:
                pass
        for f in edir.glob("*.md"):
            if f.stem not in keep:
                f.unlink(missing_ok=True)  # 已删除记忆的条目文件一并清理
        (mdir / "MEMORY_INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — memory must never break a stage run
        return


async def build_stage_summary(*, stage: str, role: str, reply_text: str, harness: str) -> str:
    """角色分层 stage 摘要：协调角色用 LLM 提炼要点，执行角色降级为增量流水。

    业界做法（多智能体记忆综述）：协调器写高层决策/结论/风险，执行者只写增量，
    避免 append_memory 无条件流水账把滚动总结冲成噪音。

    输入：stage 阶段名 / role 角色值（如 "eng"）/ reply_text harness 回复全文 / harness 名。
    输出：一段追加进 memory.md 的文本（时间戳头由 append_memory 负责）。
    任何失败降级为旧的截断流水，绝不抛出（记忆是增强不是依赖）。
    """
    from sortie_deck.settings import settings

    text = (reply_text or "").strip()
    # 降级形态：与旧行为一致的截断流水，保证无 LLM / 执行角色场景零回归
    fallback = f"Stage `{stage}` ({role}) via `{harness}`: {text[:600] or 'no output'}"
    if not settings.memory_stage_summary_llm or not text or role not in _COORDINATOR_ROLES:
        return fallback
    try:
        from sortie_deck.llm import LlmClient

        client = LlmClient(timeout=15.0)
        if not client.enabled:
            return fallback
        summary = await client.chat_text(
            system=(
                "你是任务记忆摘要器。从给定 stage 回复中提炼要点式 markdown，"
                "只保留：做出的决策、关键结论、遗留风险。每条一行，最多 5 行，不新增内容。"
            ),
            user=f"stage={stage}\nrole={role}\n回复：\n{text[:4000]}",
            temperature=0.1,
        )
        if summary.strip():
            return f"Stage `{stage}` ({role}) via `{harness}` 要点：\n{summary.strip()}"
    except Exception:  # noqa: BLE001 — 摘要是增强项，失败回退截断流水
        logger.warning("stage 分层摘要生成失败，降级流水: stage=%s role=%s", stage, role)
    return fallback
