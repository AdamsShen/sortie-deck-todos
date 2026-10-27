"""上游产物摘要器：把「全文注入 prompt」换成「摘要 + 路径」。

背景（2026-10-27 实测）：单个 `eng_backend_selftest` 阶段跑了 196 轮模型请求，
单次 input 中位 28.7 万 token —— 上游产物全文既进 prompt，又被 agent 反复 Read，
两端都在放大上下文。业界的通行做法是 **context pointer / context offloading**：
把产物落盘、只给模型「摘要 + 路径」，让「取不取、取多少」的决策从阶段入口推迟到读取时
（见 https://understandingdata.com/ai-coding-dictionary/context-pointer/ 与
https://www.agentpatternscatalog.org/patterns/filesystem-as-context）。

本模块只负责生成这段 brief，不修改传入的 dict，也不负责落盘产物本身：
- 短产物（<= inline_chars）直接内联全文，避免为小文件多付一次摘要调用；
- `diff.patch` 一律走规则摘要（文件清单 + 增删行统计），大 patch 送 LLM 纯属浪费；
- 其余并发调 LLM 摘要，失败 / 超时 / 未启用 LLM 时回落规则摘要；
- 摘要按「产物路径 + 内容 sha1」缓存，同一产物在后续阶段零额外调用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 与 mission_memory 的估算口径保持一致：4 字符 ≈ 1 token
_CHARS_PER_TOKEN = 4
_CACHE_FILENAME = "artifact_summaries.json"
# 规则摘要里首段的最大字符数：够 agent 判断「要不要打开」，又不至于顶掉预算
_RULE_FIRST_PARA_CHARS = 400
_MAX_HEADINGS = 12

_SUMMARY_SYSTEM = (
    "你是产物摘要器。读者是下一位将要执行本阶段任务的工程师 Agent，"
    "他可以通过文件路径读到全文。你的摘要只负责帮他判断「这份产物里有什么、要不要读、读哪段」。"
    "要求：中文；不超过 200 字；保留可验证的关键信息（接口路径、字段、验收编号、决策结论、"
    "已知风险/未决项）；不要复述标题，不要写「本文档介绍了」这类空话。"
)


class ArtifactBrief(BaseModel):
    """一份上游产物在 prompt 里的呈现形态。"""

    name: str
    path: str
    chars: int
    mode: Literal["llm", "rule", "inline"]
    summary: str

    def render(self) -> str:
        """渲染成 prompt 中的一行（含缩进摘要），保持稳定易读。"""
        if self.mode == "inline":
            return f"- `{self.name}` — {self.chars} chars — {self.path}\n{self.summary.strip()}"
        return f"- `{self.name}` — {self.chars} chars — {self.path}\n  > {self.summary.strip()}"


def _sha12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _truncate_chars(text: str, max_tokens: int) -> str:
    limit = max(80, int(max_tokens) * _CHARS_PER_TOKEN)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _markdown_headings(text: str) -> list[str]:
    heads = re.findall(r"^#{1,3}\s+(.+)$", text, flags=re.MULTILINE)
    return [h.strip() for h in heads[:_MAX_HEADINGS] if h.strip()]


def _first_paragraph(text: str) -> str:
    """取正文第一段（跳过标题行、代码块与空行）。"""
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith("#") or line.startswith("|"):
            continue
        return line[:_RULE_FIRST_PARA_CHARS]
    return ""


def _diff_summary(text: str, max_tokens: int) -> str:
    """diff 摘要：改动文件清单 + 增删行统计（不把 patch 内容塞进上下文）。"""
    files: list[str] = []
    added = removed = 0
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:].strip())
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    head = f"{len(files)} 个文件，+{added}/-{removed} 行"
    if not files:
        return head
    # 只列 basename 与前 8 个：路径前缀高度重复，全列会让摘要吃掉整个预算
    short = [f.rsplit("/", 1)[-1] for f in files]
    listed = "、".join(short[:8]) + (f" 等 {len(short)} 个" if len(short) > 8 else "")
    return _truncate_chars(f"{head}。改动文件：{listed}", max_tokens)


def _json_summary(text: str, max_tokens: int) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if isinstance(data, dict):
        keys = list(data.keys())
        shape = f"对象，{len(keys)} 个顶层键：{'、'.join(str(k) for k in keys[:15])}"
    elif isinstance(data, list):
        shape = f"数组，{len(data)} 条"
        if data and isinstance(data[0], dict):
            shape += f"；首条键：{'、'.join(str(k) for k in list(data[0])[:10])}"
    else:
        shape = f"标量值：{str(data)[:80]}"
    return _truncate_chars(shape, max_tokens)


def rule_summary(name: str, text: str, *, max_tokens: int = 200) -> str:
    """规则摘要（零成本兜底）：按文件形态取最省 token 的结构信息。

    输入: name="tech_design.md", text=<全文>
    输出: "## 标题大纲：背景 / 接口设计 / 风险… 首段：…"
    """
    body = (text or "").strip()
    if not body:
        return "（空文件）"
    lower = name.lower()
    if lower.endswith(".patch") or lower.endswith(".diff"):
        return _diff_summary(body, max_tokens)
    if lower.endswith(".json"):
        return _json_summary(body, max_tokens) or _truncate_chars(body, max_tokens)

    parts: list[str] = []
    heads = _markdown_headings(body)
    if heads:
        parts.append("大纲：" + " / ".join(heads))
    first = _first_paragraph(body)
    if first:
        parts.append("首段：" + first)
    if not parts:
        parts.append(body[: _RULE_FIRST_PARA_CHARS])
    return _truncate_chars("。".join(parts), max_tokens)


# --- 缓存 -------------------------------------------------------------------


def _cache_path(memory_dir: Path) -> Path:
    return memory_dir / _CACHE_FILENAME


def _load_cache(memory_dir: Path | None) -> dict[str, dict[str, object]]:
    if memory_dir is None:
        return {}
    path = _cache_path(memory_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 缓存损坏只降级为「不命中」，不能影响阶段执行
        logger.warning("产物摘要缓存损坏，忽略：%s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(memory_dir: Path | None, cache: dict[str, dict[str, object]]) -> None:
    if memory_dir is None:
        return
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        _cache_path(memory_dir).write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.warning("产物摘要缓存写入失败（忽略）", exc_info=True)


def _cache_key(path: str, content: str) -> str:
    return f"{path}#{_sha12(content)}"


# --- LLM 摘要 ---------------------------------------------------------------


async def _llm_summary(
    name: str,
    text: str,
    *,
    max_tokens: int,
    timeout: float,
) -> str:
    """单份产物的 LLM 摘要；任何异常/空结果都返回空串，由调用方回落规则摘要。"""
    from sortie_deck.llm import LlmClient

    client = LlmClient(timeout=timeout)
    if not client.enabled:
        return ""
    # 只有 CLI fallback 时不要起子进程：阶段内部本就在跑 harness，
    # 再拉一个 `claude -p` 又慢又抢资源，规则摘要足够判断「要不要读」
    if client.backend == "claude_cli":
        return ""
    # 只截前 6000 字符送模型：超长产物后半段对「要不要读」的判断边际收益极低
    user = (
        f"产物文件名：{name}\n"
        f"总字符数：{len(text)}\n"
        f"--- 开头部分 ---\n{text[:6000]}"
    )
    summary = await asyncio.wait_for(
        client.chat_text(system=_SUMMARY_SYSTEM, user=user, temperature=0.1),
        timeout=timeout,
    )
    return _truncate_chars(summary or "", max_tokens)


# --- 主入口 -----------------------------------------------------------------


async def build_artifact_briefs(
    contents: dict[str, str],
    paths: dict[str, str],
    *,
    mission_id: str,
    max_tokens: int = 200,
    inline_chars: int = 1200,
    use_llm: bool = True,
    timeout: float = 12.0,
    memory_dir: Path | None = None,
) -> dict[str, ArtifactBrief]:
    """生成「摘要 + 路径」清单。

    contents: {文件名: 全文}；paths: {文件名: 绝对路径}（缺路径时 path 记为空串，调用方自行降级）
    mission_id: 仅用于日志定位；memory_dir 由调用方按任务目录传入，用于摘要缓存。
    返回: {文件名: ArtifactBrief}，顺序与 contents 一致。
    """
    if not contents:
        return {}

    cache = _load_cache(memory_dir)
    briefs: dict[str, ArtifactBrief] = {}
    dirty = False
    pending: list[tuple[str, str, str]] = []  # (name, text, cache_key)

    for name, raw in contents.items():
        text = raw or ""
        path = paths.get(name, "")
        key = _cache_key(path or name, text)
        if len(text) <= max(0, inline_chars):
            briefs[name] = ArtifactBrief(
                name=name, path=path, chars=len(text), mode="inline", summary=text.strip()
            )
            continue
        cached = cache.get(key)
        if isinstance(cached, dict) and str(cached.get("summary") or "").strip():
            briefs[name] = ArtifactBrief(
                name=name,
                path=path,
                chars=len(text),
                mode=str(cached.get("mode") or "rule"),  # type: ignore[arg-type]
                summary=str(cached["summary"]),
            )
            continue
        pending.append((name, text, key))

    if pending:

        async def _summarize_one(name: str, text: str) -> str:
            # diff 不送 LLM：patch 的增删统计用规则算更准也更省
            if not use_llm or name.lower().endswith((".patch", ".diff")):
                return ""
            return await _llm_summary(name, text, max_tokens=max_tokens, timeout=timeout)

        results = await asyncio.gather(
            *(_summarize_one(name, text) for name, text, _key in pending),
            return_exceptions=True,
        )

        for (name, text, key), result in zip(pending, results, strict=False):
            summary = result if isinstance(result, str) else ""
            mode: Literal["llm", "rule"] = "llm"
            if not summary.strip():
                summary = rule_summary(name, text, max_tokens=max_tokens)
                mode = "rule"
                if isinstance(result, BaseException) and not isinstance(
                    result, (TimeoutError, asyncio.CancelledError)
                ):
                    logger.warning(
                        "产物 LLM 摘要失败已回落规则摘要: mission=%s name=%s",
                        mission_id,
                        name,
                        exc_info=result,
                    )
            briefs[name] = ArtifactBrief(
                name=name,
                path=paths.get(name, ""),
                chars=len(text),
                mode=mode,
                summary=summary,
            )
            cache[key] = {
                "summary": summary,
                "mode": mode,
                "chars": len(text),
                "name": name,
            }
            dirty = True

    if dirty:
        _save_cache(memory_dir, cache)

    modes = [b.mode for b in briefs.values()]
    logger.info(
        "artifact briefs built: mission=%s files=%d inline=%d llm=%d rule=%d",
        mission_id,
        len(briefs),
        modes.count("inline"),
        modes.count("llm"),
        modes.count("rule"),
    )
    return briefs


def render_artifact_briefs(briefs: dict[str, ArtifactBrief]) -> str:
    """把 briefs 渲染成 prompt 段落；空集合返回空串（调用方据此跳过整段）。"""
    if not briefs:
        return ""
    lines = [brief.render() for brief in briefs.values()]
    return "\n".join(lines)
