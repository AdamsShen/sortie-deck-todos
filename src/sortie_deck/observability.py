"""OpenTelemetry 全链路追踪的唯一接触面。

设计约束：

* 只依赖 ``opentelemetry-api``（主依赖，纯接口无传递依赖）。``sdk`` 与
  OTLP exporter 走 ``[otel]`` 可选 extra —— 没装 SDK 或没配 endpoint 时
  tracer 是 no-op，``span()`` 退化为空上下文。业务代码因此可以无条件埋点，
  不必到处写 ``if settings.otel_enabled``。
* 业务模块只允许 import 本模块，禁止直接 import ``opentelemetry.sdk``。
  换后端、换版本、临时关闭都只改这里一处。
* 观测设施是增强项，任何失败都必须降级为 warning：初始化失败不能阻断启动，
  属性写入失败不能打断业务流程，脏 traceparent 不能打挂队列任务。

属性命名约定：官方语义约定优先（``gen_ai.*`` / ``http.*`` / ``process.*``），
项目自有字段统一用 ``sortie.`` 前缀，避免与将来的官方 spec 撞名。

用法::

    with obs.span("stage.run", **{"sortie.stage": stage}) as sp:
        result = await run(...)
        obs.mark_result(sp, result)
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

# opentelemetry-api 已在主依赖中；这里的兜底只为「依赖被手工删掉」的极端情况，
# 缺包时全模块退化为 no-op，不拖垮服务启动。
try:
    from opentelemetry import context as _otel_context
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    _API_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - 仅依赖缺失时触发
    _otel_context = None  # type: ignore[assignment]
    _otel_trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    _API_AVAILABLE = False

_TRACER_NAME = "sortie_deck"
_PROPAGATOR = TraceContextTextMapPropagator() if _API_AVAILABLE else None

# 进程级状态：init_tracing 只生效一次（FastAPI lifespan 与 CLI 都可能调）
_enabled = False
_capture_content = False
_init_done = False


def init_tracing(cfg: Any = None) -> bool:
    """按配置初始化 TracerProvider，返回是否真正启用。幂等。

    以下情况只打 warning 不抛异常，保证服务照常启动：
    开关关闭；endpoint 为空（建了 provider 也没人消费，span 只会堆内存）；
    SDK / exporter 未安装（只装了 api）。
    """
    global _enabled, _capture_content, _init_done
    if _init_done:
        return _enabled
    _init_done = True

    if not _API_AVAILABLE:
        logger.warning("opentelemetry-api 缺失，全链路追踪降级为 no-op")
        return False

    cfg = cfg or _settings()
    if cfg is None or not bool(getattr(cfg, "otel_enabled", False)):
        return False

    endpoint = str(getattr(cfg, "otel_endpoint", "") or "").strip()
    if not endpoint:
        logger.warning("TDT_OTEL_ENABLED=true 但 TDT_OTEL_ENDPOINT 为空，追踪未启用")
        return False

    _capture_content = bool(getattr(cfg, "otel_capture_content", False))

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ModuleNotFoundError:
        logger.warning(
            "OpenTelemetry SDK 未安装，追踪降级为 no-op；"
            "请 pip install 'sortie-deck[otel]'"
        )
        return False

    try:
        ratio = _clamp_ratio(getattr(cfg, "otel_sample_ratio", 1.0))
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": str(
                        getattr(cfg, "otel_service_name", "sortie-deck") or "sortie-deck"
                    ),
                    "service.version": _package_version(),
                    "deployment.environment": str(getattr(cfg, "env", "development")),
                }
            ),
            # ParentBased：跨进程/队列续接时沿用上游采样决策，否则 worker 侧
            # 会按自己的比例重新抽，导致一条任务半条有 trace 半条没有。
            sampler=ParentBased(TraceIdRatioBased(ratio)),
        )
        exporter = OTLPSpanExporter(
            endpoint=_traces_endpoint(endpoint),
            headers=_parse_headers(str(getattr(cfg, "otel_headers", "") or "")) or None,
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=2048,
                max_export_batch_size=512,
                schedule_delay_millis=5000,
            )
        )
        _otel_trace.set_tracer_provider(provider)
    except Exception as exc:  # noqa: BLE001 — 观测设施不得阻断启动
        logger.warning("OpenTelemetry 初始化失败，追踪关闭: %s: %s", type(exc).__name__, exc)
        return False

    _enabled = True
    logger.info("OpenTelemetry 已启用: endpoint=%s ratio=%.2f", endpoint, ratio)
    return True


def shutdown_tracing() -> None:
    """flush + shutdown；挂在 FastAPI lifespan 收尾，避免最后一批 span 丢失。"""
    if not _enabled or not _API_AVAILABLE:
        return
    with contextlib.suppress(Exception):
        provider = _otel_trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush(timeout_millis=5000)
        stop = getattr(provider, "shutdown", None)
        if callable(stop):
            stop()


def enabled() -> bool:
    """追踪是否真正启用（供 doctor / 测试断言用）。"""
    return _enabled


def capture_content() -> bool:
    """是否允许把 prompt/completion 全文写进 span（默认关，含 PII）。"""
    return _capture_content


@contextlib.contextmanager
def span(name: str, /, **attrs: Any) -> Iterator[Any]:
    """开一个 span；未启用时是空上下文，yield None。

    真正抛出的异常会自动 record_exception + ERROR 后原样抛出。但项目里大量
    失败是「转成返回值」的（``StageResult(status="fail")``、边界降级兜底），
    这类必须显式调 ``mark_failure`` / ``mark_result``，否则 trace 全绿。
    """
    if not _enabled:
        yield None
        return

    tracer = _otel_trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(name) as sp:
        set_attributes(sp, attrs)
        try:
            yield sp
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, _short(f"{type(exc).__name__}: {exc}")))
            raise


def mark_failure(sp: Any, reason: str, *, status_code: int | None = None) -> None:
    """把「已经转成返回值的失败」标成 ERROR，并附上定位信息。"""
    if sp is None:
        return
    with contextlib.suppress(Exception):
        sp.set_status(Status(StatusCode.ERROR, _short(reason)))
        if status_code is not None:
            sp.set_attribute("sortie.status_code", int(status_code))


def mark_result(sp: Any, result: dict[str, Any] | None) -> None:
    """按 StageResult.status 决定 span 状态；``ok`` 之外一律 ERROR。

    needs_hitl 也算非成功：它意味着任务停住等人工，在瀑布图里应当可辨。
    """
    if sp is None or not isinstance(result, dict):
        return
    status = str(result.get("status") or "")
    if not status:
        return
    with contextlib.suppress(Exception):
        sp.set_attribute("sortie.result_status", status)
        if status != "ok":
            sp.set_status(Status(StatusCode.ERROR, _short(f"stage result={status}")))


def record_event(sp: Any, name: str, **attrs: Any) -> None:
    """在 span 上加事件（HITL 决策、降级等瞬时点），不新开 span。"""
    if sp is None:
        return
    with contextlib.suppress(Exception):
        sp.add_event(name, {k: v for k, v in attrs.items() if v is not None})


def record_llm(
    sp: Any,
    *,
    model: str,
    provider: str,
    usage: dict[str, Any] | None = None,
    finish_reason: str = "",
) -> None:
    """写 GenAI 语义约定属性；usage 缺失时只写模型信息，不抛异常。

    usage 形如 LangChain ``AIMessage.usage_metadata``：
    ``{"input_tokens": 120, "output_tokens": 340, "total_tokens": 460}``。
    部分 OpenAI 兼容端点不返回 usage，此时 token 归因缺失是事实，不做估算。
    """
    if sp is None:
        return
    attrs: dict[str, Any] = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
    }
    if usage:
        for src, dst in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
        ):
            value = usage.get(src)
            if isinstance(value, int) and value >= 0:
                attrs[dst] = value
    if finish_reason:
        attrs["gen_ai.response.finish_reasons"] = [finish_reason]
    set_attributes(sp, attrs)


def current_trace_id() -> str:
    """当前活跃 trace 的 32 位小写 hex（与 Jaeger 查询、前端深链格式一致）。"""
    if not _enabled:
        return ""
    ctx = _otel_trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return ""
    return format(ctx.trace_id, "032x")


def headers_from_context() -> dict[str, str]:
    """序列化当前 context 为 W3C traceparent，用于跨进程 / 队列传播。"""
    if not _enabled or _PROPAGATOR is None:
        return {}
    carrier: dict[str, str] = {}
    with contextlib.suppress(Exception):
        _PROPAGATOR.inject(carrier)
    return carrier


@contextlib.contextmanager
def context_from_carrier(carrier: dict[str, str] | None) -> Iterator[None]:
    """从 carrier 还原父级 context；入口是 worker 消费队列 job 时。

    脏的 traceparent 只记 debug 并继续执行任务——观测数据坏了不该影响业务。
    """
    if not _enabled or _PROPAGATOR is None or not carrier:
        yield
        return
    try:
        ctx = _PROPAGATOR.extract(carrier)
    except Exception as exc:  # noqa: BLE001 — 非法 traceparent 不能打挂任务
        logger.debug("traceparent 解析失败，按新 trace 处理: %s", exc)
        yield
        return
    token = _otel_context.attach(ctx)
    try:
        yield
    finally:
        _otel_context.detach(token)


def truncate(text: Any, limit: int = 200) -> str:
    """属性 / 日志用：压平换行并截断，避免整段 prompt 或命令行进 span。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."


def set_attributes(sp: Any, attrs: dict[str, Any]) -> None:
    """批量写属性；单个属性类型非法不影响其余字段。"""
    if sp is None:
        return
    for key, value in attrs.items():
        if value is None:
            continue
        with contextlib.suppress(Exception):
            sp.set_attribute(key, value)


def _short(text: str) -> str:
    return truncate(text, 255)


def _clamp_ratio(raw: Any) -> float:
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return 1.0


def _traces_endpoint(raw: str) -> str:
    """补全 OTLP/HTTP 的 traces 路径。

    实测：``OTLPSpanExporter(endpoint=...)`` 传进来的 URL 会被原样使用（不像
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` 环境变量那样自动拼 ``/v1/traces``）。
    只给 ``http://host:4318`` 会 POST 到根路径，Jaeger/Tempo 一律 404，
    表现为「初始化成功但一条 span 都看不到」。
    """
    value = raw.strip().rstrip("/")
    if value.endswith("/v1/traces"):
        return value
    return f"{value}/v1/traces"


def _parse_headers(raw: str) -> dict[str, str]:
    """解析 ``k=v,k2=v2``；值里允许出现 ``=``，按第一个切分。"""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("sortie-deck")
    except Exception:  # noqa: BLE001 — 未安装（editable 早期）时不影响启动
        return "0.0.0"


def _settings() -> Any:
    try:
        from sortie_deck.settings import settings

        return settings
    except Exception:  # noqa: BLE001
        return None
