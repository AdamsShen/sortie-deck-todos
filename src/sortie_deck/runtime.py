from __future__ import annotations

import logging
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

from sortie_deck import observability as obs
from sortie_deck.settings import Settings, settings

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [trace=%(otel_trace_id)s] %(message)s"


@dataclass
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


class _TraceIdFilter(logging.Filter):
    """往 LogRecord 注入 ``otel_trace_id``，让日志和 trace 能直接对照。

    没有这条，排查时仍要靠时间戳人工对齐「哪条日志属于哪次执行」，
    全链路的最后一公里等于没打通。未启用追踪时统一填 ``-``。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.otel_trace_id = obs.current_trace_id() or "-"
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format=_LOG_FORMAT,
        )
    else:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # filter 必须挂在 handler 上：冒泡到 root 的子 logger 记录是在 handler 层过滤的，
    # 挂在 logger 上会漏掉绝大多数第三方日志
    for handler in root.handlers:
        if not any(isinstance(f, _TraceIdFilter) for f in handler.filters):
            handler.addFilter(_TraceIdFilter())
    # Avoid accidental token leaks from httpx/uvicorn access if misconfigured
    logging.getLogger("httpx").setLevel(logging.WARNING)


def ensure_runtime_dirs(cfg: Settings | None = None) -> None:
    cfg = cfg or settings
    # 任务目录收敛后，本地产物/工作区/聊天都在 data/missions/<id>/ 下
    #（见 layout.py）；旧布局由启动迁移搬运（api lifespan 调 migrate_legacy_layout）。
    for path in (
        cfg.data_dir,
        cfg.missions_dir,
        cfg.rooms_dir,  # 旧布局根，仅作迁移探测与兼容；新写入不再使用
    ):
        path.mkdir(parents=True, exist_ok=True)


def validate_settings(cfg: Settings | None = None) -> None:
    """Raise on unsafe production configuration."""
    cfg = cfg or settings
    # Forces production secret check
    cfg.resolved_token_secret()
    backend = (cfg.artifact_backend or "local").lower()
    if backend in {"s3", "minio"} and not (cfg.s3_bucket or "").strip():
        raise RuntimeError("TDT_S3_BUCKET required when TDT_ARTIFACT_BACKEND=s3")
    # 任务存储只支持 sqlite：JSON 是整文件读-改-写，并发（多任务/多 worker）
    # 会静默丢失任务状态。这里在启动期拦死，而不是等线上发现状态跳变。
    if (cfg.storage or "").strip().lower() not in {"sqlite", "sql"}:
        raise RuntimeError(
            "TDT_STORAGE 只支持 sqlite：JSON 存储已废弃（整文件读-改-写，"
            "多任务并发会互相覆盖）。请删除该项或设为 TDT_STORAGE=sqlite；"
            "首次启动会把 data/initiatives.json 自动导入（原文件保留备份）。"
        )
    if (cfg.run_backend or "").lower() == "sqlite":
        cfg.runs_sqlite.parent.mkdir(parents=True, exist_ok=True)


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def run_doctor(cfg: Settings | None = None) -> DoctorReport:
    cfg = cfg or settings
    report = DoctorReport()
    try:
        secret = cfg.resolved_token_secret()
        weak = secret == "tdt-dev-secret"
        report.checks.append(
            DoctorCheck(
                "token_secret",
                ok=True,
                detail=(
                    "using development default secret"
                    if weak
                    else "custom TDT_TOKEN_SECRET set"
                ),
            )
        )
        if cfg.env.lower() == "production" and weak:
            report.checks[-1] = DoctorCheck(
                "token_secret", False, "production requires non-default TDT_TOKEN_SECRET"
            )
    except Exception as exc:  # noqa: BLE001
        report.checks.append(DoctorCheck("token_secret", False, str(exc)))

    data = Path(cfg.data_dir)
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        report.checks.append(DoctorCheck("data_dir", True, f"writable: {data}"))
    except Exception as exc:  # noqa: BLE001
        report.checks.append(DoctorCheck("data_dir", False, str(exc)))

    free = _port_free(cfg.host, cfg.port)
    report.checks.append(
        DoctorCheck(
            "api_port",
            free,
            f"{cfg.host}:{cfg.port} {'available' if free else 'in use'}",
        )
    )

    backend = (cfg.artifact_backend or "local").lower()
    if backend in {"s3", "minio"}:
        ok = bool(cfg.s3_bucket)
        report.checks.append(
            DoctorCheck(
                "artifacts",
                ok,
                f"s3 bucket={'set' if ok else 'missing'}",
            )
        )
    else:
        report.checks.append(DoctorCheck("artifacts", True, f"local → {cfg.missions_dir}"))

    run_backend = (cfg.run_backend or "inline").strip().lower()
    if run_backend == "sqlite":
        queue_detail = (
            f"sqlite (worker={cfg.resolved_worker_id})"
            if cfg.run_worker
            else "sqlite（本进程不消费队列，需另起 sortie worker）"
        )
    else:
        queue_detail = "inline（任务在 API 进程内直接执行）"
    report.checks.append(DoctorCheck("run_backend", True, queue_detail))

    storage = (cfg.storage or "").strip().lower()
    storage_ok = storage in {"sqlite", "sql"}
    report.checks.append(
        DoctorCheck(
            "storage",
            storage_ok,
            f"{storage} → {cfg.initiatives_sqlite}"
            if storage_ok
            else f"{storage} — 已废弃，需改 TDT_STORAGE=sqlite（旧 JSON 会自动导入）",
        )
    )

    # LLM 体检：半配置（有 base_url/model 但没 key）最容易表现为"任务卡在某个阶段"
    llm_key = (cfg.llm_api_key or "").strip()
    if llm_key:
        llm_detail = f"http · {cfg.llm_base_url or 'default'} · {cfg.llm_model or 'default'}"
    elif cfg.llm_cli_fallback and shutil.which("claude"):
        # CLI 回退可用但每次调用可能很慢，明确标出最坏耗时
        llm_detail = (
            f"claude CLI 回退（单次最长 {cfg.llm_cli_timeout:.0f}s）；"
            "建议配 TDT_LLM_API_KEY，否则每个阶段都可能等满超时"
        )
    else:
        llm_detail = "未配置（LLM 相关能力走规则/模板降级）"
    report.checks.append(DoctorCheck("llm", True, llm_detail))

    # 任务目录体检：目录总数 + 待 TTL 回收的 intake 目录（会话开了但没建队）
    try:
        from sortie_deck.repositories.sqlite_initiatives import SqliteInitiativeStore

        missions_dir = Path(cfg.missions_dir)
        dirs = [p.name for p in missions_dir.iterdir() if p.is_dir()] if missions_dir.is_dir() else []
        known = {ini.id for ini in SqliteInitiativeStore(cfg.initiatives_sqlite).list()}
        pending = [
            name
            for name in dirs
            if name not in known and (missions_dir / name / "chat" / "planner.state.json").exists()
        ]
        report.checks.append(
            DoctorCheck(
                "missions",
                True,
                f"{len(dirs)} mission dirs, {len(pending)} intake pending sweep "
                f"(ttl {getattr(cfg, 'intake_ttl_days', 7)}d)",
            )
        )
    except Exception as exc:  # noqa: BLE001 — 体检项失败不应让 doctor 崩掉
        report.checks.append(DoctorCheck("missions", False, str(exc)))
    return report
