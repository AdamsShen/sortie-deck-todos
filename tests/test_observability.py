"""observability 模块的行为契约测试。

重点不是覆盖率，而是四条容易在后续改动中悄悄退化的语义：

1. 关闭时彻底 no-op —— 不产生 span、不返回 trace_id，业务可以无条件埋点；
2. 转成返回值的失败必须显式标记 ERROR —— 项目里大量 ``StageResult(status="fail")``
   和边界降级，光靠捕获异常会让 trace 全绿；
3. traceparent 跨进程往返后 trace_id 不变 —— 多 worker 队列不断链的前提；
4. usage 缺失时不估算 token —— 估算值混进成本看板比没有更糟。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from sortie_deck import observability as obs  # noqa: E402


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    # tracer provider 是进程级单例，只能设置一次；后续测试共用它，
    # 每个用例用 exporter.clear() 隔离。
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(sink))
    trace.set_tracer_provider(provider)
    return sink


@pytest.fixture
def tracing(exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch):
    exporter.clear()
    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(obs, "_init_done", True)
    return exporter


def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs, "_enabled", False)
    with obs.span("should.not.exist") as sp:
        assert sp is None
        assert obs.current_trace_id() == ""


def test_nested_span_tree(tracing: InMemorySpanExporter) -> None:
    with (
        obs.span("mission.run", **{"sortie.initiative_id": "ini_x"}),
        obs.span("stage.run", **{"sortie.stage": "intake"}),
    ):
        pass

    spans = {s.name: s for s in tracing.get_finished_spans()}
    assert set(spans) == {"mission.run", "stage.run"}
    assert spans["mission.run"].attributes["sortie.initiative_id"] == "ini_x"
    # 子 span 必须挂在编排 span 下，平铺会丢失「谁触发了这次调用」的因果
    assert spans["stage.run"].parent.span_id == spans["mission.run"].context.span_id


def test_exception_marks_error_and_reraises(tracing: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), obs.span("stage.run"):
        raise RuntimeError("kaboom")

    span = tracing.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.events, "异常必须记录成 span event，否则只有一个状态码无法定位"


def test_mark_result_turns_non_ok_into_error(tracing: InMemorySpanExporter) -> None:
    with obs.span("stage.run") as sp:
        obs.mark_result(sp, {"status": "fail", "message": "contract missing"})

    span = tracing.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["sortie.result_status"] == "fail"


def test_mark_result_keeps_ok_unset(tracing: InMemorySpanExporter) -> None:
    with obs.span("stage.run") as sp:
        obs.mark_result(sp, {"status": "ok"})

    span = tracing.get_finished_spans()[0]
    assert span.status.status_code.name == "UNSET"


def test_mark_result_treats_needs_hitl_as_error(tracing: InMemorySpanExporter) -> None:
    with obs.span("stage.run") as sp:
        obs.mark_result(sp, {"status": "needs_hitl"})

    span = tracing.get_finished_spans()[0]
    # needs_hitl 不是成功：任务停住等人工，瀑布图上必须可辨
    assert span.status.status_code.name == "ERROR"


def test_traceparent_roundtrip_keeps_trace_id(tracing: InMemorySpanExporter) -> None:
    with obs.span("mission.run"):
        carrier = obs.headers_from_context()
        expected = obs.current_trace_id()
    assert carrier.get("traceparent")
    assert expected

    # 模拟跨进程消费：父 span 已关闭，靠 carrier 还原出的 context 续接
    with obs.context_from_carrier(carrier), obs.span("mission.run.worker"):
        assert obs.current_trace_id() == expected

    spans = tracing.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].context.trace_id == spans[1].context.trace_id


def test_context_from_empty_carrier_is_noop(tracing: InMemorySpanExporter) -> None:
    with obs.context_from_carrier(None), obs.span("mission.run"):
        assert obs.current_trace_id()
    assert len(tracing.get_finished_spans()) == 1


def test_record_llm_writes_genai_attributes(tracing: InMemorySpanExporter) -> None:
    with obs.span("gen_ai.chat") as sp:
        obs.record_llm(
            sp,
            model="gpt-4o-mini",
            provider="openai-compatible",
            usage={"input_tokens": 12, "output_tokens": 34},
            finish_reason="stop",
        )

    span = tracing.get_finished_spans()[0]
    assert span.attributes["gen_ai.request.model"] == "gpt-4o-mini"
    assert span.attributes["gen_ai.provider.name"] == "openai-compatible"
    assert span.attributes["gen_ai.usage.input_tokens"] == 12
    assert span.attributes["gen_ai.usage.output_tokens"] == 34
    assert span.attributes["gen_ai.response.finish_reasons"] == ("stop",)


def test_record_llm_without_usage_does_not_estimate(tracing: InMemorySpanExporter) -> None:
    with obs.span("gen_ai.chat") as sp:
        obs.record_llm(sp, model="m", provider="p", usage=None)

    span = tracing.get_finished_spans()[0]
    assert "gen_ai.usage.input_tokens" not in span.attributes
    assert "gen_ai.usage.output_tokens" not in span.attributes


def test_truncate_flattens_and_clamps() -> None:
    assert obs.truncate("a\nb\n  c", 20) == "a b c"
    assert len(obs.truncate("x" * 500, 200)) == 200


def test_log_filter_injects_trace_id(tracing: InMemorySpanExporter) -> None:
    from sortie_deck.runtime import _TraceIdFilter

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", None, None)
    with obs.span("mission.run"):
        _TraceIdFilter().filter(record)
        assert record.otel_trace_id == obs.current_trace_id()


def test_log_filter_falls_back_to_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    from sortie_deck.runtime import _TraceIdFilter

    monkeypatch.setattr(obs, "_enabled", False)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", None, None)
    _TraceIdFilter().filter(record)
    assert record.otel_trace_id == "-"


async def _drain(exporter: InMemorySpanExporter, name: str, tries: int = 100) -> bool:
    """等某个 span 出现：编排跑在 asyncio.create_task 里，任务收尾比状态翻转晚一步。"""
    for _ in range(tries):
        if any(s.name == name for s in exporter.get_finished_spans()):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_real_pipeline_produces_stage_spans(
    tracing: InMemorySpanExporter, tmp_path
) -> None:
    """真实跑一次编排（到第一个 HITL），验证埋点位置而不是埋点 API。

    单测只能证明 span() 行为正确；这条用例证明 ``run_stage`` /
    ``_run_until_interrupt`` 里的埋点确实被走到、且层级挂对了。
    """
    from sortie_deck.models import CreateInitiativeRequest
    from sortie_deck.orchestrator import Orchestrator
    from sortie_deck.settings import Settings

    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        # 关掉 LLM 规划与房间讨论：结果确定、不依赖网络
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="span demo",
                brief="Build notification center",
                coding_executor="mock",
                harness="mock",
                pipeline_track="default",
                auto_start=True,
                auto_confirm_pipeline=True,
            )
        )
        assert await _drain(tracing, "stage.run")
        for _ in range(120):
            await asyncio.sleep(0.1)
            current = orch.get(ini.id)
            assert current
            if current.status.value in {"waiting_hitl", "done", "failed"}:
                break
    finally:
        await orch.shutdown()

    assert await _drain(tracing, "mission.run")
    spans = tracing.get_finished_spans()
    mission = next(s for s in spans if s.name == "mission.run")
    stages = [s for s in spans if s.name == "stage.run"]

    assert stages, "阶段 span 必须产生"
    assert mission.attributes["sortie.initiative_id"] == ini.id
    # 串行阶段挂在 mission.run 之下：这一条断言就是「全链路」能否拼接的最小证据
    assert any(s.parent.span_id == mission.context.span_id for s in stages)
    assert all(s.attributes.get("sortie.stage") for s in stages)
