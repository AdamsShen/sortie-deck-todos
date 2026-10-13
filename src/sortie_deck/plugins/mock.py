from __future__ import annotations

import json
from pathlib import Path

from sortie_deck.models import StageContext, StageResult


class MockProductExecutor:
    name = "mock_product"

    async def run(self, ctx: StageContext) -> StageResult:
        title_hint = ctx.brief.strip().split("\n", 1)[0][:80]
        extra = f"\n\nHuman note: {ctx.human_instruction}" if ctx.human_instruction else ""
        prd = f"""# PRD: {title_hint}

## Goal
Deliver the requested capability described below.

## Background
{ctx.brief}{extra}

## Scope
- Implement the core user-facing flow
- Persist required data
- Expose minimal observability

## Non-goals
- Multi-tenant billing
- Full RBAC rewrite

## User stories
1. As a user, I can complete the primary flow end-to-end.
2. As an operator, I can see success/failure status.

## Acceptance criteria
- Happy path works in preview
- Error states are covered by QA cases
- Artifacts are handed off without human retyping
"""
        acceptance = {
            "criteria": [
                {"id": "AC1", "text": "Happy path works in preview", "required": True},
                {"id": "AC2", "text": "Error states covered", "required": True},
                {"id": "AC3", "text": "Artifact handoff without retyping", "required": True},
            ]
        }
        return StageResult(
            status="ok",
            message="PRD and acceptance criteria drafted",
            artifacts={
                "prd.md": prd,
                "acceptance.json": json.dumps(acceptance, ensure_ascii=False, indent=2),
            },
            proposed_next="design_ui",
        )


class MockDesignExecutor:
    name = "mock_design"

    async def run(self, ctx: StageContext) -> StageResult:
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)[:600]
        note = ctx.human_instruction or "Align with PRD flows"
        design = f"""# Design spec

Instruction: {note}

## Upstream PRD (excerpt)
{prd}

## Flows
1. Entry → primary action → success / empty / error
2. Platform notes: respect iOS / Android / Web conventions when multi-end

## Visual
- Hierarchy: brand → primary CTA → supporting copy
- States: loading, empty, error, success

## Handoff
Engineers implement screens listed in `screens.json` without re-asking product for layout.
"""
        screens = {
            "screens": [
                {"id": "S1", "name": "Home", "platform": ["web", "ios", "android"]},
                {"id": "S2", "name": "Primary flow", "platform": ["web", "ios", "android"]},
                {"id": "S3", "name": "Error / empty", "platform": ["web", "ios", "android"]},
            ]
        }
        return StageResult(
            status="ok",
            message="Design spec drafted",
            artifacts={
                "design.md": design,
                "screens.json": json.dumps(screens, ensure_ascii=False, indent=2),
            },
            proposed_next="eng_implement",
        )


class MockEngExecutor:
    name = "mock_eng"

    async def run(self, ctx: StageContext) -> StageResult:
        prd = ctx.upstream_artifacts.get("prd.md", "(no prd)")
        design = ctx.upstream_artifacts.get("design.md", "")
        note = ctx.human_instruction or "Follow PRD"
        specialty = {
            "eng_ios": "iOS / Swift",
            "eng_android": "Android / Kotlin",
            "eng_web": "Web frontend",
            "eng_backend": "Backend / API",
            "eng_agent": "Agent / tools / MCP",
        }.get(ctx.stage, ctx.role.value)
        api_contract = ctx.upstream_artifacts.get("api_contract.md", "")
        tasks = {
            "tasks": [
                {"id": "T1", "title": f"Scaffold {specialty} module", "status": "done"},
                {"id": "T2", "title": "Wire contracts / persistence", "status": "done"},
                {"id": "T3", "title": "Add basic tests", "status": "done"},
            ]
        }
        design_block = f"\n## Upstream design (excerpt)\n{design[:400]}\n" if design else ""
        contract_block = (
            f"\n## Upstream API contract (excerpt)\n{api_contract[:500]}\n" if api_contract else ""
        )
        diff = f"""diff --git a/src/feature/{ctx.stage}.py b/src/feature/{ctx.stage}.py
new file mode 100644
--- /dev/null
+++ b/src/feature/{ctx.stage}.py
@@ -0,0 +1,5 @@
+def handle(request: dict) -> dict:
+    return {{"ok": True, "stage": "{ctx.stage}", "echo": request}}
"""
        if ctx.stage == "eng_backend":
            contract_doc = f"""# API contract (draft)

## Endpoints
- `GET /api/health` → `{{"status":"ok"}}`
- `POST /api/{ctx.initiative_id}/action` → `{{"ok": true}}`

## Notes
- Frontend may mock these shapes while backend implements.
- Breaking changes require bumping the contract before 联调.
"""
            implementation = f"""# Implementation notes ({specialty})

Instruction: {note}

## Upstream PRD (excerpt)
{prd[:500]}
{design_block}
## Changes
- Drafted API contract for parallel client work
- Scaffolded service handlers
- Added unit tests

## Residual risk
- Contract may evolve before 联调
"""
            artifacts = {
                "implementation.md": implementation,
                "api_contract.md": contract_doc,
                "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                "diff.patch": diff,
            }
        elif ctx.stage == "eng_web":
            mock_note = (
                "Using upstream `api_contract.md` + local mocks."
                if api_contract
                else "No API contract yet — UI + mocks first; wire real endpoints at 联调."
            )
            implementation = f"""# Implementation notes ({specialty})

Instruction: {note}

## Parallel-dev approach
{mock_note}

## Upstream PRD (excerpt)
{prd[:500]}
{design_block}{contract_block}
## Changes
- Built UI screens from design
- Added mock API client adapters
- Ready to swap mocks when backend contract stabilizes

## Residual risk
- Mock drift vs final API
"""
            artifacts = {
                "implementation.md": implementation,
                "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                "diff.patch": diff,
            }
        else:
            implementation = f"""# Implementation notes ({specialty})

Instruction: {note}

## Upstream PRD (excerpt)
{prd[:500]}
{design_block}{contract_block}
## Changes
- Added feature module for `{ctx.stage}`
- Wired contracts from PRD / design
- Added unit tests

## Residual risk
- Preview deploy still mock unless shell plugin configured
"""
            artifacts = {
                "implementation.md": implementation,
                "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                "diff.patch": diff,
            }
        workspace = Path(ctx.workspace_dir) if ctx.workspace_dir else None
        if workspace:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "demo.py").write_text(
                'def handle(request: dict) -> dict:\n    return {"ok": True, "echo": request}\n',
                encoding="utf-8",
            )
        return StageResult(
            status="ok",
            message=f"Mock {specialty} implementation completed",
            artifacts=artifacts,
            proposed_next="qa_verify",
        )


class MockQaExecutor:
    name = "mock_qa"

    async def run(self, ctx: StageContext) -> StageResult:
        force_fail = bool(ctx.human_instruction and "fail qa" in ctx.human_instruction.lower())
        attempt = int(ctx.permissions.get("qa_attempt", 1))
        # First attempt fails once to demonstrate loop unless user forced otherwise
        passed = not force_fail and attempt > 1
        if force_fail:
            passed = False
        if ctx.human_instruction and "pass qa" in ctx.human_instruction.lower():
            passed = True

        cases = """# Test cases

1. Happy path creates expected response
2. Invalid input returns error
3. Artifact contract files exist
"""
        report = {
            "passed": passed,
            "attempt": attempt,
            "summary": "All checks passed" if passed else "Acceptance AC1 not met yet",
            "failures": [] if passed else [{"id": "AC1", "detail": "Happy path incomplete in mock run #1"}],
            "cases_executed": 3,
        }
        return StageResult(
            status="ok" if passed else "fail",
            message="QA passed" if passed else "QA failed — looping to eng",
            artifacts={
                "test_cases.md": cases,
                "test_report.json": json.dumps(report, ensure_ascii=False, indent=2),
            },
            proposed_next="deploy_preview" if passed else "eng_implement",
            qa_passed=passed,
        )


class MockDeployExecutor:
    name = "mock_deploy"

    async def run(self, ctx: StageContext) -> StageResult:
        plan = f"""# Deploy plan (preview)

Initiative: {ctx.initiative_id}
Strategy: dry-run mock deploy
Steps:
1. Build package
2. Push preview tag
3. Smoke health endpoint
"""
        result = {
            "status": "success",
            "environment": "preview",
            "url": f"https://preview.local/{ctx.initiative_id}",
            "dry_run": True,
        }
        return StageResult(
            status="ok",
            message="Preview deploy plan executed (mock)",
            artifacts={
                "deploy_plan.md": plan,
                "deploy_result.json": json.dumps(result, ensure_ascii=False, indent=2),
            },
            proposed_next="done",
        )


class MockIntakeExecutor:
    name = "mock_intake"

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            status="ok",
            message="Intake recorded",
            artifacts={
                "intake.md": f"# Intake\n\n{ctx.brief}\n",
            },
            proposed_next="product_prd",
        )


class MockTechDesignExecutor:
    """Deterministic tech-design document for the per-role ``*_tech_design`` stages.

    Reads the upstream PRD and produces a reviewable technical approach
    (architecture, module breakdown, risks) *before* any build stage runs.
    """

    name = "mock_tech_design"

    async def run(self, ctx: StageContext) -> StageResult:
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief or "(no prd)")
        design_md = (
            f"# Technical Design — {ctx.brief}\n\n"
            f"## Context\n\n{prd[:2000]}\n\n"
            "## Approach\n\n"
            "- Reuse the existing delivery pipeline; keep the blast radius small.\n"
            "- Implement behind the primary module touched by the PRD; no schema change "
            "unless acceptance requires one.\n"
            "- Cover acceptance.json criteria with the QA cases produced downstream.\n\n"
            "## Module breakdown\n\n"
            "| Module | Change | Risk |\n|---|---|---|\n"
            "| core | adapt to PRD flow | low |\n"
            "| api | expose/adjust endpoints if needed | medium |\n"
            "| tests | cover acceptance criteria | low |\n\n"
            "## Rollout & rollback\n\n"
            "- Ship behind the stage gate (HITL review of this document).\n"
            "- Rollback = revert the build-stage diff; no data migration assumed.\n"
        )
        return StageResult(
            status="ok",
            message="Tech design drafted (mock)",
            artifacts={"tech_design.md": design_md},
            proposed_next=None,
        )


class MockCollaborativeExecutor:
    """Demo session-capable executor.

    Shows the pause/resume contract: mid-step the agent opens a temporary
    sub-room, waits for the conclusion, and then finishes its own work with that
    conclusion folded in. Registered as ``mock_collab`` for demos and tests.
    """

    name = "mock_collab"

    def __init__(self, *, question: str = "对齐验收边界与失败路径") -> None:
        self.question = question

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            status="ok",
            message=f"{ctx.stage} ran (single-shot)",
            artifacts={f"{ctx.stage}.md": f"# {ctx.stage}\n"},
        )

    async def run_session(self, ctx: StageContext, host) -> StageResult:
        conclusion = await host.open_subroom(
            goal=f"`{ctx.stage}` 上的疑问：{self.question}",
            questions=[self.question],
        )
        body = json.dumps(
            {
                "stage": ctx.stage,
                "question": self.question,
                "subroom_conclusion": conclusion,
            },
            ensure_ascii=False,
            indent=2,
        )
        return StageResult(
            status="ok",
            message=f"{ctx.stage} ran with a sub-room consult",
            artifacts={f"{ctx.stage}.md": body},
        )


class MockQaCasesExecutor:
    name = "mock_qa_cases"

    async def run(self, ctx: StageContext) -> StageResult:
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)[:800]
        plan = f"""# 测试计划

## 范围
基于已评审 PRD 编写可测用例。

## PRD 摘要
{prd}

## 策略
- 冒烟 / 主路径
- 边界与失败路径
- 回归关注点
"""
        cases = """# 测试用例

| ID | 场景 | 预期 | 优先级 |
|----|------|------|--------|
| TC1 | 主路径成功 | 返回成功态 | P0 |
| TC2 | 非法输入 | 明确错误 | P0 |
| TC3 | 权限拒绝 | 403/提示 | P1 |
"""
        return StageResult(
            status="ok",
            message="测试用例已起草，等待评审",
            artifacts={"test_plan.md": plan, "test_cases.md": cases},
            proposed_next="eng_implement",
        )


class MockSelfTestExecutor:
    name = "mock_selftest"

    async def run(self, ctx: StageContext) -> StageResult:
        note = ctx.human_instruction or "本地冒烟通过"
        body = f"""# 自测记录

Initiative: {ctx.initiative_id}
Result: PASS (mock)
Notes: {note}

Checklist:
- [x] 单元测试
- [x] 本地启动冒烟
- [x] 关键日志无 ERROR
"""
        return StageResult(
            status="ok",
            message="开发自测完成",
            artifacts={"selftest.md": body},
            proposed_next="eng_integrate",
        )


class MockIntegrateExecutor:
    name = "mock_integrate"

    async def run(self, ctx: StageContext) -> StageResult:
        body = f"""# 联调记录

Initiative: {ctx.initiative_id}
Upstream services: mock
Result: READY_FOR_QA

Verified:
- API contract matches PRD acceptance
- Preview env smoke OK
"""
        return StageResult(
            status="ok",
            message="联调完成，可提测",
            artifacts={"integrate.md": body},
            proposed_next="qa_handoff",
        )


class MockHandoffExecutor:
    name = "mock_handoff"

    async def run(self, ctx: StageContext) -> StageResult:
        body = f"""# 提测单

Initiative: {ctx.initiative_id}
Build: mock-build
Env: preview
Diff: see implementation artifacts

请 QA 按已评审用例执行；阻塞项回复 `/reject`。
"""
        return StageResult(
            status="ok",
            message="提测单已提交，等待放行",
            artifacts={"handoff.md": body},
            proposed_next="qa_verify",
        )
