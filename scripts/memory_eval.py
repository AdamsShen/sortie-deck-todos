"""记忆检索质量评估（阶段二 #8，LoCoMo/LongMemEval 思路的轻量抽检）。

构造「query → 应命中条目」用例集，在隔离的临时 MemoryStore 上跑检索，
统计 top-k 命中率，并可对比 local（关键词打分+新近度）与 weknora（混合检索
+rerank，需 TDT_MEMORY_RETRIEVAL=weknora 且服务可用）两条路径。

用法：
    python scripts/memory_eval.py                      # 内置用例，local 路径
    python scripts/memory_eval.py --cases cases.json   # 自定义用例
    python scripts/memory_eval.py --out report.md      # 结果落 markdown

用例格式（JSON 数组）：
    [{"query": "部署前要做什么",
      "expect": ["lint"],        # 命中判定：任一关键词出现在 top-k 条目内容里
      "k": 3}]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from sortie_deck.memory import CreateMemoryRequest, MemoryStore
from sortie_deck.settings import settings

# 内置种子记忆（模拟真实记忆库的内容分布：全局约定 / 个人偏好 / 角色 lesson）
_SEED_ENTRIES: list[dict] = [
    {"scope": "workspace", "kind": "lesson", "content": "部署前先跑 lint 与单测，双绿再发车。", "tags": ["process"]},
    {"scope": "workspace", "kind": "preference", "content": "产物优先短、可验收；避免空话 PRD。", "tags": ["style"]},
    {"scope": "workspace", "kind": "fact", "content": "数据库连接统一走 PolarDB，测试库与生产库分开配置。", "tags": ["infra"]},
    {"scope": "workspace", "kind": "fact", "content": "接口变更必须同步更新 thrift IDL 与文档。", "tags": ["api"]},
    {"scope": "workspace", "kind": "episode", "content": "上次发布回滚是因为缓存未预热，之后每次发版先预热。", "tags": ["release"]},
    {"scope": "user", "scope_id": "usr_eval", "kind": "preference", "content": "代码注释与日志统一用中文。", "tags": ["style"]},
    {"scope": "agent", "scope_id": "eng", "kind": "lesson", "content": "缓存穿透必须做空值兜底，否则打穿数据库。", "tags": ["procedural"]},
    {"scope": "agent", "scope_id": "eng", "kind": "lesson", "content": "异步回调失败要幂等重试，防止重复扣款。", "tags": ["procedural"]},
    {"scope": "agent", "scope_id": "product", "kind": "lesson", "content": "写 PRD 前先对齐验收标准，避免返工。", "tags": ["procedural"]},
    {"scope": "agent", "scope_id": "ops", "kind": "lesson", "content": "批量任务要支持断点续跑，失败可从检查点恢复。", "tags": ["procedural"]},
]

# 内置评估用例：query → 期望命中的关键词
_DEFAULT_CASES: list[dict] = [
    {"query": "发布前需要检查什么", "expect": ["lint", "单测"], "k": 3},
    {"query": "数据库用哪个", "expect": ["PolarDB"], "k": 3},
    {"query": "缓存穿透怎么处理", "expect": ["空值兜底"], "k": 3},
    {"query": "异步失败重试要注意什么", "expect": ["幂等"], "k": 3},
    {"query": "PRD 怎么写", "expect": ["验收标准"], "k": 3},
    {"query": "注释用什么语言", "expect": ["中文"], "k": 3},
    {"query": "批量任务失败怎么办", "expect": ["断点续跑"], "k": 3},
    {"query": "接口改了要同步什么", "expect": ["thrift"], "k": 3},
]


def _seed_store(tmp: Path) -> MemoryStore:
    store = MemoryStore(tmp / "memory.json")
    for spec in _SEED_ENTRIES:
        store.create(
            CreateMemoryRequest(
                scope=spec["scope"],
                scope_id=spec.get("scope_id", "global"),
                kind=spec["kind"],
                content=spec["content"],
                tags=spec.get("tags", []),
                source="eval",
            ),
            author_id="usr_eval",
            author_name="Eval",
        )
    return store


def _hit(entry_content: str, expect: list[str]) -> bool:
    lowered = entry_content.lower()
    return any(kw.lower() in lowered for kw in expect)


def run_eval(cases: list[dict], store: MemoryStore) -> dict:
    """逐用例跑 context_candidates，统计 top-k 命中率与逐例明细。"""
    rows: list[dict] = []
    hits = 0
    for case in cases:
        query = case["query"]
        expect = case.get("expect", [])
        k = int(case.get("k", 3))
        picked = store.context_candidates(query=query, user_id="usr_eval", limit=k)
        contents = [e.content for e in picked]
        ok = any(_hit(c, expect) for c in contents)
        hits += int(ok)
        rows.append(
            {
                "query": query,
                "expect": expect,
                "k": k,
                "hit": ok,
                "top": contents,
            }
        )
    total = len(cases) or 1
    return {"hit_rate": hits / total, "hits": hits, "total": len(cases), "rows": rows}


def render_report(result: dict, path_label: str) -> str:
    lines = [
        f"# 记忆检索质量评估（{path_label}）",
        "",
        f"- 用例数：{result['total']}",
        f"- top-k 命中率：**{result['hit_rate']:.0%}**（{result['hits']}/{result['total']}）",
        "",
        "| query | 期望关键词 | 命中 | top 内容（截断） |",
        "|---|---|---|---|",
    ]
    for r in result["rows"]:
        mark = "✅" if r["hit"] else "❌"
        tops = " <br> ".join(c[:40] for c in r["top"])
        lines.append(f"| {r['query']} | {','.join(r['expect'])} | {mark} | {tops} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆检索质量评估")
    parser.add_argument("--cases", type=str, default="", help="自定义用例 JSON 文件")
    parser.add_argument("--out", type=str, default="", help="报告输出 markdown 路径")
    args = parser.parse_args()

    cases = _DEFAULT_CASES
    if args.cases:
        cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))

    retrieval = (settings.memory_retrieval or "local").strip().lower()
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(Path(tmp))
        result = run_eval(cases, store)

    label = f"路径: {retrieval}"
    print(render_report(result, label))  # noqa: T201

    if args.out:
        Path(args.out).write_text(render_report(result, label), encoding="utf-8")
        print(f"报告已写入 {args.out}")  # noqa: T201

    # weknora 路径提示（同一批用例对比需真实服务，此处仅提示当前模式）
    if retrieval != "weknora":
        print(  # noqa: T201
            "提示：当前为 local 检索路径；对比 weknora 混合检索请设置 "
            "TDT_MEMORY_RETRIEVAL=weknora 后重跑。"
        )
    return 0 if result["hit_rate"] >= 0.7 else 2  # 低于 70% 视为需要调优


if __name__ == "__main__":
    sys.exit(main())
