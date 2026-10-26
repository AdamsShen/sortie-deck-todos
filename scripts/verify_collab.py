#!/usr/bin/env python3
"""多人协作 + Agent Harness 验收脚本。

两种模式：
- ``--harness mock``：把 mock harness 换成"剧本化 provider"，按阶段返回预设回复，
  让 handoff / 小群 / 元工具 / 升级真人 / 并行汇合 等控制平面链路在无模型时也能跑通。
  这是 CI 回归模式，断言的是**编排层兑现能力**，不是模型能力。
- ``--harness claude_code|codex|opencode|cursor``：真实 agent 环境跑精简航线
  （express：集结 → 开发 → 测试 → 发布），验证任务能在真实 harness 里完成。

产物写入 ``data/verify/<run_id>/``：
  report.json / report.md   — 每个检查项的 pass/fail/skip + 证据
  initiative_<scenario>.json — 任务快照（状态、timeline、pending_hitl）

用法：
  python scripts/verify_collab.py --harness mock
  python scripts/verify_collab.py --harness claude_code --scenario basic
  python scripts/verify_collab.py --harness codex --scenario handoff --timeout 2400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
try:  # 兼容未 pip install -e 的临时环境
    import sortie_deck  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT / "src"))

from sortie_deck.harness_providers.base import (  # noqa: E402
    HarnessRegistry,
    HarnessReply,
    HarnessSession,
    ProbeStatus,
    build_harness_registry,
)
from sortie_deck.models import (  # noqa: E402
    CreateInitiativeRequest,
    ForgeReviewRequiredError,
    HitlAction,
    HitlDecision,
    Initiative,
    InitiativeStatus,
    PipelineStageSpec,
    UpdatePipelineRequest,
)
from sortie_deck.orchestrator import Orchestrator, bind_catalogs  # noqa: E402
from sortie_deck.settings import Settings  # noqa: E402
from sortie_deck.toolkit import (  # noqa: E402
    CreateToolkitRequest,
    SkillScript,
    ToolkitStore,
)

logger = logging.getLogger("verify_collab")

SCRIPTED = "mock"  # 剧本化 provider 复用 mock 名字，任务才能以 harness=mock 启动

# 真机模式下只跑能在 3-4 个阶段内观察完的场景，其余明确 skip
_REAL_SCENARIOS = ("basic", "handoff", "subroom", "escalate")

# 真机模式下写进 brief 的验收指令（模型需要被明确要求输出控制块）
_DIRECTIVES: dict[str, str] = {
    "basic": (
        "【验收要求】这是控制平面验收任务，不是功能开发：请勿修改或新增代码、不要运行构建/测试；"
        "每个阶段只写一个不超过 40 行的 markdown 说明（文件名用 `<stage>.md`），"
        "并以 `## Result` 小节收尾，全程控制在 2 分钟以内。"
    ),
    "handoff": (
        "【验收要求】开发（build）阶段完成实现后，必须在回复末尾额外输出一个 ```control 块，"
        '内容为 {"handoff": {"role": "qa", "reason": "实现完成，交给测试"}}；'
        "不要交给其他角色，也不要省略该块。"
    ),
    "subroom": (
        "【验收要求】开发（build）阶段先和 QA 拉一次小群确认验收口径，在回复中输出一个 "
        "```subroom 块："
        '{"goal": "确认验收口径", "questions": ["上线前是否必须回归登录？"], "participants": ["qa"]}；'
        "拿到结论后再给出本阶段产出。"
    ),
    "escalate": (
        "【验收要求】开发（build）阶段必须把下面这个问题升级给真人拍板，在回复中输出一个 "
        "```control 块："
        '{"escalate": {"question": "两个验收口径冲突：选 A（全量回归）还是 B（只测改动）？", '
        '"options": ["A", "B"]}}；不要自行决定。'
    ),
}


# --------------------------------------------------------------------------- #
# 报告结构
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    """一条可观测验收结论（pass / fail / skip）。"""

    id: str
    name: str
    status: str = "fail"
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class Report:
    run_id: str
    harness: str
    mode: str
    started_at: str
    checks: list[Check] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        log = (
            logger.info
            if check.ok
            else (logger.warning if check.status == "skip" else logger.error)
        )
        log("[%s] %s — %s", check.status.upper(), check.id, check.name)
        return check

    def summary(self) -> dict[str, int]:
        out = {"pass": 0, "fail": 0, "skip": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def markdown(self) -> str:
        s = self.summary()
        lines = [
            f"# 多人协作 + Harness 验收报告 · {self.mode}",
            "",
            f"- run_id：`{self.run_id}`",
            f"- harness：`{self.harness}`",
            f"- 开始时间：{self.started_at}",
            f"- 结论：**{s['pass']} passed / {s['fail']} failed / {s['skip']} skipped**",
            "",
            "## Harness 探测",
            "",
            "| Provider | 状态 | 说明 |",
            "|----------|------|------|",
        ]
        for row in self.probes:
            lines.append(f"| `{row['name']}` | {row['status']} | {row['detail']} |")
        lines += [
            "",
            "## 检查项",
            "",
            "| # | 能力 | 结果 | 说明 | 证据 |",
            "|---|------|------|------|------|",
        ]
        for c in self.checks:
            ev = "<br>".join(c.evidence[:4])
            lines.append(f"| {c.id} | {c.name} | {c.status} | {c.detail} | {ev} |")
        lines += [
            "",
            "## 说明",
            "",
            "- `pass` 表示该能力的**可观测证据**成立；`skip` 表示当前环境无法验证（原因写在说明列）。",
        ]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 剧本化 harness provider（mock 模式）
# --------------------------------------------------------------------------- #


class ScriptedProvider:
    """按阶段返回预设文本，用来在无模型时驱动完整编排链路。"""

    name = SCRIPTED

    def __init__(self, reply_fn: Callable[[str, str], str]) -> None:
        self._reply_fn = reply_fn
        self.calls: list[tuple[str, str]] = []

    def probe(self) -> ProbeStatus:
        return ProbeStatus(status="ok", detail="scripted verify provider (mock harness)")

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_vf_{stage}_{len(self.calls)}",
            provider=self.name,
            meta={"initiative_id": initiative_id, "stage": stage, **(meta or {})},
        )

    async def send(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        cwd: str | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> HarnessReply:
        stage = str(session.meta.get("stage") or "")
        text = self._reply_fn(stage, prompt)
        self.calls.append((stage, prompt))
        return HarnessReply(
            status="ok",
            text=text,
            session_id=session.id,
            native_id=f"{session.id}:{len(self.calls)}",
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _install_scripted_registry(provider: ScriptedProvider) -> Callable[[], None]:
    """把剧本化 provider 注入执行器解析路径；返回恢复函数。"""
    import sortie_deck.harness_providers.base as base

    original = base.get_harness_registry
    registry = HarnessRegistry(probe_ttl=1.0)
    registry.register(provider)
    base.get_harness_registry = lambda: registry  # type: ignore[assignment]
    return lambda: setattr(base, "get_harness_registry", original)


class _NullGateway:
    """空知识网关：剧本化回归不依赖外部 WeKnora，避免网络耗时影响可复现性。"""

    def retrieve(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def index_mission_artifact(self, *args: Any, **kwargs: Any) -> None:
        return None


def _disable_rag() -> Callable[[], None]:
    import sortie_deck.knowledge_gateway as gateway_module

    original = gateway_module.gateway_from_settings
    gateway_module.gateway_from_settings = lambda *a, **k: _NullGateway()  # type: ignore[assignment]
    return lambda: setattr(gateway_module, "gateway_from_settings", original)


def _disable_llm() -> Callable[[], None]:
    """关闭进程内 LLM：让 Manager/规划/讨论都走规则回退，脚本断言才可复现。"""
    import sortie_deck.llm as llm_module

    original = llm_module.LlmClient

    class _DisabledLlm:
        backend = "disabled"
        enabled = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def chat_json(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("llm disabled by verify_collab")

        async def chat_text(self, **kwargs: Any) -> str:
            raise RuntimeError("llm disabled by verify_collab")

    llm_module.LlmClient = _DisabledLlm  # type: ignore[misc,assignment]
    return lambda: setattr(llm_module, "LlmClient", original)


# --------------------------------------------------------------------------- #
# 环境与任务驱动
# --------------------------------------------------------------------------- #


@dataclass
class Env:
    args: argparse.Namespace
    cfg: Settings
    orch: Orchestrator
    harness: str
    provider: Any
    toolkit: ToolkitStore
    data_dir: Path
    timeout: float
    restores: list[Callable[[], None]] = field(default_factory=list)

    def close_patches(self) -> None:
        for restore in reversed(self.restores):
            try:
                restore()
            except Exception:  # noqa: BLE001 — 恢复失败不影响报告
                logger.warning("恢复 patch 失败", exc_info=True)


def _plain_reply(stage: str, _prompt: str) -> str:
    """默认剧本回复：QA 阶段带结论块（通过），其余阶段只交付正文。"""
    text = f"# {stage}\n\n本阶段完成。\n\n## Result\n- {stage} done"
    if stage == "qa_verify":
        text += '\n\n```qa\n{"passed": true, "summary": "验收用例全部通过"}\n```'
    return text


async def _boot(args: argparse.Namespace, reply_fn: Callable[[str, str], str]) -> Env:
    data_dir = Path(tempfile.mkdtemp(prefix="sortie-verify-"))
    logger.info("场景数据目录：%s（--keep-data 可保留排查）", data_dir)
    cfg = Settings(
        data_dir=data_dir,
        deploy_executor="mock",
        # 验收要可复现：关闭规划/讨论 LLM，关闭 harness 降级（失败要暴露而不是静默顶替）
        pipeline_llm=False,
        room_llm=False,
        memory_backend="local",
        kb_backend="local",
        # 剧本化回归不需要把产物镜像进外部知识库（会带来网络依赖与耗时）
        kb_mission_index=args.harness != SCRIPTED,
        default_harness=args.harness if args.harness != SCRIPTED else SCRIPTED,
        harness_fallback=False,
        loadout_stage_recall=False,
        # 剧本化模式不需要模型；真机模式保持默认开关，由真实 provider 执行
        subroom_enabled=True,
        im_escalation=True,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    toolkit = ToolkitStore(cfg.data_dir / "toolkit.json")
    bind_catalogs(toolkit=toolkit)

    restores: list[Callable[[], None]] = []
    if args.harness == SCRIPTED:
        provider = ScriptedProvider(reply_fn)
        restores.append(_install_scripted_registry(provider))
        restores.append(_disable_llm())
        restores.append(_disable_rag())
    else:
        from sortie_deck.harness_providers.base import get_harness_registry

        provider = get_harness_registry().get(args.harness)
    return Env(
        args=args,
        cfg=cfg,
        orch=orch,
        harness=args.harness,
        provider=provider,
        toolkit=toolkit,
        data_dir=data_dir,
        timeout=float(args.timeout),
        restores=restores,
    )


async def _shutdown(env: Env) -> None:
    env.close_patches()
    try:
        await env.orch.shutdown()
    except Exception:  # noqa: BLE001 — 关闭失败不掩盖验收结论
        logger.warning("orchestrator shutdown 失败", exc_info=True)
    if not env.args.keep_data:
        shutil.rmtree(env.data_dir, ignore_errors=True)


async def _create_mission(
    env: Env,
    brief: str,
    *,
    title: str,
    track: str | None = None,
    stages: list[dict[str, Any]] | None = None,
    scenario: str = "",
) -> Initiative:
    """建任务；给了 stages 就改写为自定义航线（免 HITL，便于确定性断言）。

    真机模式下把场景验收指令拼进 brief：模型必须被明确要求才会输出控制块。
    """
    if env.harness != SCRIPTED and scenario in _DIRECTIVES:
        brief = f"{brief}\n\n{_DIRECTIVES[scenario]}"
    ini = await env.orch.create_initiative(
        CreateInitiativeRequest(
            title=title,
            brief=brief,
            pipeline_track=track,  # type: ignore[arg-type]
            coding_executor="mock",
            harness=env.harness,
            creator_name="verify",
            auto_confirm_pipeline=stages is None,
        )
    )
    if stages is not None:
        ini = await env.orch.update_pipeline(
            ini.id,
            UpdatePipelineRequest(
                stages=[PipelineStageSpec(**spec) for spec in stages], confirm=True
            ),
            author_name="verify",
        )
    return ini


async def _drive(
    env: Env,
    ini_id: str,
    *,
    on_hitl: Callable[[Initiative], Any] | None = None,
    timeout: float | None = None,
) -> tuple[Initiative | None, list[str]]:
    """推进任务直到 done / failed / stopped / 超时；HITL 由回调处理。"""
    notes: list[str] = []
    deadline = time.monotonic() + (timeout or env.timeout)
    ini = env.orch.get(ini_id)
    while ini is not None and time.monotonic() < deadline:
        status = ini.status
        if status == InitiativeStatus.DONE:
            return ini, notes
        if status in {InitiativeStatus.FAILED, InitiativeStatus.STOPPED}:
            return ini, notes
        if status == InitiativeStatus.WAITING_HITL:
            prompt = (ini.pending_hitl.prompt if ini.pending_hitl else "")[:160]
            notes.append(f"HITL@{ini.current_stage}: {prompt}")
            if on_hitl is not None:
                outcome = on_hitl(ini)
                if asyncio.iscoroutine(outcome):
                    await outcome
            else:
                await env.orch.submit_hitl(
                    ini_id,
                    HitlDecision(action=HitlAction.APPROVE),
                    author_name="verify",
                )
        await asyncio.sleep(0.15)
        ini = env.orch.get(ini_id)
    return ini, notes


def _events(ini: Initiative | None, kind: str | None = None) -> list[Any]:
    if ini is None:
        return []
    rows = list(ini.timeline or [])
    return [e for e in rows if kind is None or e.kind == kind]


def _artifact_text(ini: Initiative | None, stage: str, name: str, root: Path) -> str:
    """按阶段 + 文件名读产物正文。

    ``ArtifactRef.path`` 是相对产物根（missions 目录）的路径，不是工作目录相对路径，
    必须用 cfg.missions_dir 拼接后再读。
    """
    if ini is None:
        return ""
    for ref in ini.artifacts:
        if ref.stage != stage or Path(ref.path).name != name:
            continue
        path = Path(ref.path)
        if not path.is_absolute():
            path = root / path
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _stage_events(ini: Initiative | None) -> list[Any]:
    return [e for e in _events(ini, "info") if (e.data or {}).get("executor") == "harness_stage"]


def _stage_status(event: Any) -> str:
    """阶段事件的执行状态存在 data.status（TimelineEvent 本身没有 status 字段）。"""
    return str((event.data or {}).get("status") or "")


def _check_stage_artifacts(ini: Initiative | None, root: Path) -> tuple[bool, list[str]]:
    """C1 证据：每个执行过的阶段都落了 reply.md。"""
    rows = _stage_events(ini)
    missing = [e.stage for e in rows if not _artifact_text(ini, e.stage, "reply.md", root)]
    evidence = [f"{e.stage}:{_stage_status(e)}" for e in rows]
    return (bool(rows) and not missing), evidence + (
        [f"missing reply.md: {missing}"] if missing else []
    )


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #


async def scenario_basic(env: Env) -> list[Check]:
    """C1：真实/剧本化 harness 完成一条完整任务，每阶段产出 reply.md + 契约产物。"""
    ini = await _create_mission(
        env,
        "验收基础链路：把一个内部小需求的交付跑完。",
        title="验收 · 基础链路",
        track="express",
        scenario="basic",
    )
    await env.orch.start_pipeline(ini.id, author_name="verify")
    ini, hitls = await _drive(env, ini.id)
    ok, evidence = _check_stage_artifacts(ini, env.cfg.missions_dir)
    checks = [
        Check(
            "C1",
            "harness 内完成任务",
            "pass" if (ini and ini.status == InitiativeStatus.DONE and ok) else "fail",
            f"final={getattr(ini, 'status', None)} · stages={len(_stage_events(ini))} · hitl={len(hitls)}",
            evidence,
        )
    ]
    # harness 标识随阶段 meta 落库，证明走的是 harness 执行器而不是旧 mock 执行器
    harness_meta = [
        str((e.data.get("meta") or {}).get("harness") or "") for e in _stage_events(ini)
    ]
    checks.append(
        Check(
            "C1b",
            "阶段由所选 harness 执行",
            "pass" if harness_meta and all(h == env.harness for h in harness_meta) else "fail",
            f"harness={env.harness}",
            sorted(set(harness_meta)),
        )
    )
    return checks


async def scenario_handoff(env: Env) -> list[Check]:
    """C2：agent 自主决定下一个沟通对象 → 条件边跳节点，跳过计划中的中间节点。"""
    stages = [
        {"id": "intake", "label": "集结", "role": "product"},
        {"id": "build", "label": "开发", "role": "eng"},
        {"id": "eng_selftest", "label": "自测", "role": "eng"},
        {"id": "qa_verify", "label": "测试", "role": "qa"},
        {"id": "done", "label": "完成", "role": "product"},
    ]

    def reply(stage: str, _prompt: str) -> str:
        body = f"# {stage}\n\n本阶段完成。\n\n## Result\n- {stage} done"
        if stage == "build":
            body += (
                '\n\n```control\n{"handoff": {"role": "qa", "reason": "实现完成，交给测试"}}\n```'
            )
        return body

    env.restores.append(_swap_reply(env, reply))
    ini = await _create_mission(
        env,
        "验收自主交接：开发完成后直接交给 QA，不需要自测节点。",
        title="验收 · 自主交接",
        stages=stages,
        scenario="handoff",
    )
    await env.orch.start_pipeline(ini.id, author_name="verify")
    ini, _ = await _drive(env, ini.id)

    transitions = [
        e for e in _events(ini, "transition") if (e.data or {}).get("handoff") == "accepted"
    ]
    executed = [(e.stage, _stage_status(e)) for e in _stage_events(ini)]
    skipped_selftest = "eng_selftest" not in [s for s, _ in executed]
    accepted_to_qa = any((e.data or {}).get("target_stage") == "qa_verify" for e in transitions)
    return [
        Check(
            "C2",
            "agent 自主选择沟通对象（handoff）",
            "pass"
            if (accepted_to_qa and skipped_selftest and ini and ini.status == InitiativeStatus.DONE)
            else "fail",
            f"accepted={len(transitions)} · skipped_selftest={skipped_selftest} · final={getattr(ini, 'status', None)}",
            [f"{s}:{st}" for s, st in executed] + [e.message for e in transitions[:2]],
        )
    ]


async def scenario_subroom(env: Env) -> list[Check]:
    """C3：agent 需要时拉小群，结论回注本阶段，大群不承载讨论记录。"""
    stages = [
        {"id": "intake", "label": "集结", "role": "product"},
        {"id": "build", "label": "开发", "role": "eng"},
        {"id": "qa_verify", "label": "测试", "role": "qa"},
        {"id": "done", "label": "完成", "role": "product"},
    ]
    build_calls = {"n": 0}

    def reply(stage: str, _prompt: str) -> str:
        if stage == "build":
            build_calls["n"] += 1
            if build_calls["n"] == 1:
                return (
                    "# build\n\n需要先确认验收口径。\n\n```subroom\n"
                    '{"goal": "确认验收口径", "questions": ["上线前是否必须回归登录？"], '
                    '"participants": ["qa"]}\n```'
                )
            return "# build\n\n已按小群结论完成实现。\n\n## Result\n- build done"
        return f"# {stage}\n\n本阶段完成。\n\n## Result\n- {stage} done"

    env.restores.append(_swap_reply(env, reply))
    ini = await _create_mission(
        env,
        "验收小群讨论：开发阶段先和 QA 确认验收口径。",
        title="验收 · 小群讨论",
        stages=stages,
        scenario="subroom",
    )
    await env.orch.start_pipeline(ini.id, author_name="verify")
    ini, _ = await _drive(env, ini.id)

    from sortie_deck.subroom import get_subroom_service

    service = get_subroom_service()
    rooms = service.store.list(initiative_id=ini.id) if service else []
    transcript_len = len(service.transcript(rooms[0].id)) if (service and rooms) else 0
    reply_md = _artifact_text(ini, "build", "reply.md", env.cfg.missions_dir)
    conclusion_folded = "小组讨论结论" in reply_md
    return [
        Check(
            "C3",
            "按需拉小群并回注结论",
            "pass"
            if (rooms and transcript_len > 0 and conclusion_folded and build_calls["n"] >= 2)
            else "fail",
            f"subrooms={len(rooms)} · transcript={transcript_len} · second_turn={build_calls['n'] >= 2}",
            [
                f"subroom={rooms[0].id} goal={rooms[0].goal[:40]}" if rooms else "no subroom",
                f"conclusion_in_reply={conclusion_folded}",
            ],
        )
    ]


async def scenario_loadout(env: Env) -> list[Check]:
    """C5：合适的时机加载 skill / mcp / tool → 元工具检索、上浮、真实执行脚本。"""
    item = env.toolkit.create(
        CreateToolkitRequest(
            kind="skill",
            name="发布检查清单",
            summary="release checklist：上线前逐项核对",
            tags=["release", "checklist", "shipping"],
            capabilities=["release checklist", "上线检查"],
            executable=True,
            read_only=True,
            scripts=[
                SkillScript(
                    path="scripts/run.py",
                    content="print('release checklist ok')\n",
                    language="py",
                    description="打印检查结果",
                )
            ],
        ),
        author_id="verify",
        author_name="Verify",
    )

    def reply(stage: str, _prompt: str) -> str:
        body = f"# {stage}\n\n本阶段完成。\n\n## Result\n- {stage} done"
        if stage == "build":
            body += (
                "\n\n```toolkit_intent\n"
                + json.dumps(
                    [
                        {"toolkit_search": {"query": "release checklist"}},
                        {"toolkit_pick": {"item_id": item.id}},
                        {
                            "skill_run": {
                                "item_id": item.id,
                                "script": "scripts/run.py",
                                "args": [],
                            }
                        },
                    ],
                    ensure_ascii=False,
                )
                + "\n```"
            )
        return body

    env.restores.append(_swap_reply(env, reply))
    ini = await _create_mission(
        env,
        "验收按需加载：开发阶段自行检索并运行发布检查技能。",
        title="验收 · 按需加载",
        stages=[
            {"id": "intake", "label": "集结", "role": "product"},
            {"id": "build", "label": "开发", "role": "eng"},
            {"id": "done", "label": "完成", "role": "product"},
        ],
    )
    await env.orch.start_pipeline(ini.id, author_name="verify")
    ini, _ = await _drive(env, ini.id)

    meta_text = _artifact_text(ini, "build", "toolkit_meta.json", env.cfg.missions_dir)
    runs_text = _artifact_text(ini, "build", "skill_runs.json", env.cfg.missions_dir)
    ops: list[str] = []
    try:
        ops = [str(r.get("op")) for r in json.loads(meta_text or "[]") if isinstance(r, dict)]
    except ValueError:
        ops = []
    return [
        Check(
            "C5",
            "按需加载 skill / tool",
            "pass" if ({"search", "pick", "skill_run"} <= set(ops) and runs_text) else "fail",
            f"ops={sorted(set(ops))} · skill_runs={bool(runs_text)}",
            [
                f"item={item.id}",
                f"toolkit_meta={bool(meta_text)}",
                f"skill_runs={bool(runs_text)}",
            ],
        )
    ]


async def scenario_escalate(env: Env) -> list[Check]:
    """C6：agent 判断需要真人 → 决策中心 + 通知渠道 → 任务暂停 → 答复后恢复。"""
    stages = [
        {"id": "intake", "label": "集结", "role": "product"},
        {"id": "build", "label": "开发", "role": "eng"},
        {"id": "done", "label": "完成", "role": "product"},
    ]

    def reply(stage: str, _prompt: str) -> str:
        body = f"# {stage}\n\n本阶段完成。\n\n## Result\n- {stage} done"
        if stage == "build":
            body += (
                "\n\n```control\n"
                '{"escalate": {"question": "两个验收口径冲突：选 A（全量回归）还是 B（只测改动）？", '
                '"options": ["A", "B"]}}\n```'
            )
        return body

    env.restores.append(_swap_reply(env, reply))
    ini = await _create_mission(
        env,
        "验收升级真人：开发阶段遇到口径冲突必须由人拍板。",
        title="验收 · 升级真人",
        stages=stages,
        scenario="escalate",
    )
    await env.orch.start_pipeline(ini.id, author_name="verify")

    state: dict[str, Any] = {"decision_id": "", "paused_status": ""}

    async def on_hitl(current: Initiative) -> None:
        from sortie_deck.decisions import get_decision_service

        service = get_decision_service()
        pending = service.list(initiative_id=current.id, status="pending") if service else []
        if pending and not state["decision_id"]:
            state["decision_id"] = pending[0].decision_id
            state["paused_status"] = str(getattr(current.status, "value", current.status))
            # 用决策中心回复（等价钉钉回调），驱动任务恢复
            outcome = await env.orch.resolve_decision(
                pending[0].decision_id, answer="选 A（全量回归）", by="verify"
            )
            state["accepted"] = bool(getattr(outcome, "accepted", False))

    ini, notes = await _drive(env, ini.id, on_hitl=on_hitl)
    hitl_events = [e for e in _events(ini, "hitl") if "escalation" in (e.message or "")]
    return [
        Check(
            "C6",
            "需要真人时走决策中心并恢复",
            "pass"
            if (
                state["decision_id"]
                and state.get("accepted")
                and state["paused_status"] == "waiting_hitl"
                and ini
                and ini.status == InitiativeStatus.DONE
            )
            else "fail",
            (
                f"decision={state['decision_id'] or '-'} · accepted={state.get('accepted')} · "
                f"paused={state['paused_status'] or '-'} · final={getattr(ini, 'status', None)}"
            ),
            notes[:2] + [e.message for e in hitl_events[:2]],
        )
    ]


async def scenario_forge(env: Env) -> list[Check]:
    """C7：角色目录不足 → 自主创建带审核角色 → 未审核拒绝出击 / 审核后放行。"""
    from sortie_deck.agent_contract import AgentContract
    from sortie_deck.agent_forge import ForgedAgentDraft, ForgeStore

    ini = await _create_mission(
        env,
        "验收自主建角色：需要宠物殡葬行业策划角色。",
        title="验收 · 自主建角色",
        stages=[
            {"id": "intake", "label": "集结", "role": "product"},
            {"id": "build", "label": "开发", "role": "eng"},
            {"id": "done", "label": "完成", "role": "product"},
        ],
    )
    store = ForgeStore(env.cfg.data_dir / "forge.json")
    draft = store.save(
        ForgedAgentDraft(
            role_key="pet_funeral",
            title="宠物殡葬策划",
            summary="宠物殡葬服务行业策划",
            persona="You plan pet funeral services.",
            contract=AgentContract(
                role_key="pet_funeral", title="宠物殡葬策划", outputs=["pet_funeral_plan.md"]
            ),
            tags=["forge", "pending-review"],
        )
    )
    ini.meta["forge_drafts"] = [draft.id]
    env.orch.store.upsert(ini)

    blocked = False
    blocked_ids: list[str] = []
    try:
        await env.orch.start_pipeline(ini.id, author_name="verify")
    except ForgeReviewRequiredError as exc:
        blocked = True
        blocked_ids = exc.draft_ids

    approved = store.get(draft.id)
    approved.status = "approved"
    store.save(approved)
    ini = env.orch.get(ini.id) or ini
    pending_after = env.orch._pending_forge_drafts(ini)
    await env.orch.start_pipeline(ini.id, author_name="verify")
    ini, _ = await _drive(env, ini.id)
    return [
        Check(
            "C7",
            "角色不足自主创建 + 必须审核",
            "pass"
            if (
                blocked
                and blocked_ids == [draft.id]
                and not pending_after
                and ini
                and ini.status == InitiativeStatus.DONE
            )
            else "fail",
            f"blocked={blocked} · draft={draft.id} · final={getattr(ini, 'status', None)}",
            [f"blocked_ids={blocked_ids}", f"pending_after_approve={pending_after}"],
        )
    ]


async def scenario_manager(env: Env) -> list[Check]:
    """C4：并行波汇合由 Manager 决策下一节点（分支都成功 → 计划下一节点）。"""
    stages = [
        {"id": "intake", "label": "集结", "role": "product"},
        {"id": "eng_web", "label": "Web", "role": "eng_web", "parallel_group": "delivery"},
        {
            "id": "eng_backend",
            "label": "Backend",
            "role": "eng_backend",
            "parallel_group": "delivery",
        },
        {"id": "eng_integrate", "label": "联调", "role": "eng"},
        {"id": "done", "label": "完成", "role": "product"},
    ]

    def reply(stage: str, _prompt: str) -> str:
        return f"# {stage}\n\n本阶段完成。\n\n## Result\n- {stage} done"

    env.restores.append(_swap_reply(env, reply))
    ini = await _create_mission(
        env,
        "验收 Manager 汇合：Web 与后端并行开发后由 Manager 决定进入联调。",
        title="验收 · Manager 汇合",
        stages=stages,
    )
    await env.orch.start_pipeline(ini.id, author_name="verify")
    ini, _ = await _drive(env, ini.id)

    transitions = [
        e for e in _events(ini, "transition") if "manager join decision" in (e.message or "")
    ]
    executed = [e.stage for e in _stage_events(ini)]
    both_branches = {"eng_web", "eng_backend"} <= set(executed)
    to_integrate = any("eng_integrate" in (e.message or "") for e in transitions)
    return [
        Check(
            "C4",
            "Manager 推进流程（并行汇合）",
            "pass"
            if (
                transitions
                and both_branches
                and to_integrate
                and ini
                and ini.status == InitiativeStatus.DONE
            )
            else "fail",
            f"join_decisions={len(transitions)} · branches={both_branches}",
            [e.message for e in transitions[:2]] + executed,
        )
    ]


def _swap_reply(env: Env, reply: Callable[[str, str], str]) -> Callable[[], None]:
    """替换剧本化 provider 的回复函数；返回恢复函数。"""
    provider = env.provider
    original = getattr(provider, "_reply_fn", None)
    if original is None:
        return lambda: None
    provider._reply_fn = reply  # type: ignore[attr-defined]
    return lambda: setattr(provider, "_reply_fn", original)


SCENARIOS: dict[str, Callable[[Env], Any]] = {
    "basic": scenario_basic,
    "handoff": scenario_handoff,
    "subroom": scenario_subroom,
    "loadout": scenario_loadout,
    "escalate": scenario_escalate,
    "forge": scenario_forge,
    "manager": scenario_manager,
}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _probe_rows() -> list[dict[str, Any]]:
    """探测所有 harness provider（含真实 CLI / SDK 凭据状态）。"""
    registry = build_harness_registry()
    rows = []
    for name in registry.names():
        status = registry.probe(name, refresh=True)
        rows.append({"name": name, "status": status.status, "detail": status.detail})
    return rows


def _skip_checks(harness: str, names: list[str]) -> list[Check]:
    return [
        Check(
            name.upper(),
            f"场景 `{name}`",
            "skip",
            f"真机模式（{harness}）只跑 {', '.join(_REAL_SCENARIOS)}；该场景为剧本化回归项",
            [],
        )
        for name in names
    ]


async def _run_scenario(args: argparse.Namespace, name: str, report: Report, out_dir: Path) -> None:
    fn = SCENARIOS[name]
    if args.harness != SCRIPTED and name not in _REAL_SCENARIOS:
        for check in _skip_checks(args.harness, [name]):
            report.add(check)
        return
    logger.info("=== 场景 %s（harness=%s）===", name, args.harness)
    try:
        env = await _boot(args, _plain_reply)
    except Exception as exc:  # noqa: BLE001 — 启动失败要落报告，而不是让脚本中断
        logger.exception("场景 %s 启动失败", name)
        report.add(Check(name.upper(), f"场景 `{name}`", "fail", f"启动失败：{exc!r}", []))
        return
    checks: list[Check] = []
    try:
        checks = await fn(env)
        # 落盘任务快照，报告之外还能回看 timeline / pending_hitl
        for ini in env.orch.store.list():
            (out_dir / f"initiative_{name}.json").write_text(
                ini.model_dump_json(indent=2), encoding="utf-8"
            )
            break
    except Exception as exc:  # noqa: BLE001 — 单个场景异常不应吞掉其它场景的结论
        logger.exception("场景 %s 执行异常", name)
        checks = [Check(name.upper(), f"场景 `{name}`", "fail", f"执行异常：{exc!r}", [])]
    finally:
        await _shutdown(env)
    for check in checks:
        if args.harness != SCRIPTED:
            check.detail += " · 真机模式"
        report.add(check)


async def main_async(args: argparse.Namespace) -> int:
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.harness}"
    out_dir = Path(args.out) if args.out else REPO_ROOT / "data" / "verify" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report(
        run_id=run_id,
        harness=args.harness,
        mode="scripted" if args.harness == SCRIPTED else "real",
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    report.probes = _probe_rows()
    logger.info("harness 探测：%s", {r["name"]: r["status"] for r in report.probes})

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    # 真机模式先看探测结果：凭据/二进制缺失时显式 skip，而不是把环境问题报成能力失败
    probe = next((r for r in report.probes if r["name"] == args.harness), None)
    unavailable = ""
    if args.harness != SCRIPTED and (probe is None or probe["status"] != "ok"):
        unavailable = (
            f"harness `{args.harness}` 不可用："
            f"{probe['status'] if probe else 'missing'} — {probe['detail'] if probe else ''}"
        )
    if unavailable:
        logger.warning(unavailable)
        for name in names:
            report.add(Check(name.upper(), f"场景 `{name}`", "skip", unavailable, []))
    else:
        for name in names:
            await _run_scenario(args, name, report, out_dir)

    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "run_id": report.run_id,
                "harness": report.harness,
                "mode": report.mode,
                "started_at": report.started_at,
                "summary": report.summary(),
                "probes": report.probes,
                "checks": [c.__dict__ for c in report.checks],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(report.markdown(), encoding="utf-8")
    summary = report.summary()
    logger.info("报告已写入 %s（%s）", out_dir, summary)
    return 0 if summary.get("fail", 0) == 0 else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多人协作 + Agent Harness 验收")
    parser.add_argument(
        "--harness",
        default=SCRIPTED,
        choices=[SCRIPTED, "claude_code", "codex", "opencode", "cursor"],
        help="harness 环境；mock=剧本化回归，其余为真实 agent 环境",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", *SCENARIOS.keys()],
        help="只跑某个场景（真机模式仅 basic/handoff/subroom/escalate）",
    )
    parser.add_argument(
        "--timeout", type=float, default=0, help="单场景超时秒数（默认 90；真机 1800）"
    )
    parser.add_argument("--out", default="", help="报告输出目录（默认 data/verify/<run_id>）")
    parser.add_argument("--keep-data", action="store_true", help="保留临时 data 目录便于排查")
    args = parser.parse_args(argv)
    if not args.timeout:
        args.timeout = 90 if args.harness == SCRIPTED else 1800
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
