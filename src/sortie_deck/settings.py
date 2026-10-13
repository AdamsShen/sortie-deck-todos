from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TDT_", env_file=".env", extra="ignore")

    data_dir: Path = REPO_ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    postgres_uri: str | None = None
    deploy_executor: str = "mock"  # mock | deploy_shell
    eng_concurrency: int = 3
    webhook_timeout: float = 5.0
    # development | production — production refuses empty / default token secrets
    env: str = "development"
    token_secret: str = "tdt-dev-secret"
    # json (default) | sqlite — initiative persistence backend
    storage: str = "json"
    # Optional LLM features (require TDT_LLM_API_KEY / OPENAI_API_KEY, or Claude CLI fallback)
    # Defaults on: plan_pipeline_smart / room replies fall back to heuristic/templates when unavailable
    pipeline_llm: bool = True
    room_llm: bool = True
    # When true, LlmClient may use `claude -p` if no HTTP API key is set
    llm_cli_fallback: bool = True
    # OpenAI-compatible HTTP (also read by LlmClient; prefer these over bare os.environ)
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    # mock | claude_code | cursor_cli | auto (detect CLI on PATH)
    default_coding_executor: str = "auto"
    # --- agent harness (system-wide capability) ------------------------------
    # Initiative-level harness; locked once the mission starts.
    # claude_code | opencode | codex | cursor | mock
    default_harness: str = "claude_code"
    # Probe cache TTL for harness availability checks (seconds)
    harness_probe_ttl: float = 60.0
    # When the chosen harness provider is unavailable, fall back to the legacy
    # stage executors (mock / claude_code CLI / ...) instead of failing the stage.
    harness_fallback: bool = True
    # Token budget for the per-mission context pack injected into harness prompts
    # (rough estimate: 4 chars ≈ 1 token). Exceeding it triggers auto-summary.
    mission_context_token_limit: int = 60_000
    # Shared memory backend: local (default) | mem0 — mem0 runs *alongside* the
    # local MemoryStore and both are merged into context blocks.
    memory_backend: str = "local"  # local | mem0
    mem0_base_url: str | None = None  # e.g. https://mem0.internal:8080
    mem0_api_key: str | None = None
    mem0_user_prefix: str = "sortie"
    mem0_timeout: float = 5.0
    # OpenCode server (deployed alongside the app in the cloud, started via
    # `opencode serve`). The provider talks HTTP to it — never a local CLI.
    opencode_base_url: str | None = None
    opencode_api_key: str | None = None
    opencode_timeout: float = 600.0
    # --- mission-scoped RAG namespace ----------------------------------------
    # Auto-index key stage artifacts into the knowledge base under the
    # initiative's namespace (tags `ini:<id>`, WeKnora folder `<prefix>/<id>`).
    kb_mission_index: bool = True
    # WeKnora folder prefix for per-mission namespaces (local catalog ignores it)
    kb_mission_folder_prefix: str = "missions"
    # Max chars per artifact body written into the mission namespace
    kb_mission_index_max_chars: int = 4000
    # Claude Code --permission-mode for non-interactive -p runs in worktrees
    # acceptEdits | auto | bypassPermissions | default | dontAsk | plan
    coding_permission_mode: str = "acceptEdits"
    # Stage timeout for coding CLI (seconds)
    coding_timeout_seconds: float = 900.0
    # When true, coding executors list/call MCP tools from toolkit bindings
    mcp_runtime: bool = False
    # --- Multi-agent collaboration switches (on by default) -----------------
    # Each one degrades safely when its dependency is missing (no LLM → rule
    # fallback; no IM channel → the request is only recorded). Set to false to
    # disable that capability.
    # Routing itself is not switchable: agent hand-off, manager join decisions and
    # HITL verdicts are all graph node jumps through one conditional-edge router.
    # Verify the frozen plan fingerprint before resuming a paused run.
    plan_digest_check: bool = True
    # Temporary sub-rooms ("小群") an executing agent may open on demand.
    # Transcripts stay inside the sub-room; only the conclusion enters the main
    # chain (as stage-scoped mission memory). Nesting depth is capped at 1.
    subroom_enabled: bool = True
    subroom_max_rounds: int = 2
    subroom_max_tokens: int = 20000
    subroom_timeout_seconds: float = 300.0
    # --- just-in-time capability loading ------------------------------------
    # Recall + select capabilities per stage instead of pre-binding everything.
    loadout_auto: bool = True
    # Allow MCP tools to actually be *called* (not just listed).
    mcp_call_enabled: bool = True
    # Allow skill scripts to actually be *executed* (not just stored).
    skill_run_enabled: bool = True
    skill_run_timeout_seconds: float = 30.0
    # --- human escalation ---------------------------------------------------
    # Push undecidable questions to a human and pause until they answer.
    # With no channel configured the question still lands in the decision centre
    # (workbench) and the mission pauses until it is answered or expires.
    im_escalation: bool = True
    # Seconds a human decision may stay unanswered before the fallback applies.
    decision_sla_seconds: float = 1800.0
    # pause (safe default) | continue | abort
    decision_fallback_action: str = "pause"
    # Outbound channels (empty = not configured)
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""
    feishu_webhook_url: str = ""
    # --- mission guardrails -------------------------------------------------
    # Runaway protection for long multi-agent missions (see guardrails.py).
    guardrail_max_tokens: int = 2_000_000
    guardrail_max_seconds: float = 21600.0  # 6h
    guardrail_max_subrooms: int = 20
    guardrail_max_handoffs: int = 40
    # Run queue: inline (default create_task) | sqlite (lease + worker loop)
    run_backend: str = "inline"
    worker_id: str = "worker-1"
    run_lease_seconds: int = 120
    # Artifacts: local | s3
    artifact_backend: str = "local"
    s3_bucket: str | None = None
    s3_prefix: str = "artifacts"
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    # Org policy JSON path (empty = disabled)
    org_policy_path: Path | None = None
    # Enterprise knowledge: local catalog | WeKnora (Tencent OSS)
    # Sortie Deck imports + catalogs; retrieval uses WeKnora hybrid-search when enabled
    kb_backend: str = "local"  # local | weknora
    weknora_base_url: str | None = None  # e.g. http://127.0.0.1:8080
    weknora_api_key: str | None = None
    weknora_kb_id: str | None = None
    # Chat bridges
    slack_signing_secret: str | None = None
    feishu_verification_token: str | None = None
    # Production polish
    log_level: str = "INFO"
    stage_timeout_seconds: float = 900.0

    def resolved_token_secret(self) -> str:
        secret = (self.token_secret or "").strip()
        if self.env.strip().lower() == "production" and (
            not secret or secret == "tdt-dev-secret"
        ):
            raise RuntimeError(
                "TDT_TOKEN_SECRET must be set to a non-default value when TDT_ENV=production"
            )
        return secret or "tdt-dev-secret"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def initiatives_db(self) -> Path:
        return self.data_dir / "initiatives.json"

    @property
    def initiatives_sqlite(self) -> Path:
        return self.data_dir / "initiatives.sqlite"

    @property
    def runs_sqlite(self) -> Path:
        return self.data_dir / "runs.sqlite"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def missions_dir(self) -> Path:
        return self.data_dir / "missions"

    @property
    def rooms_dir(self) -> Path:
        return self.data_dir / "rooms"

    @property
    def users_db(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def knowledge_db(self) -> Path:
        return self.data_dir / "knowledge.json"

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.json"

    @property
    def agents_db(self) -> Path:
        return self.data_dir / "agents.json"

    @property
    def toolkit_db(self) -> Path:
        return self.data_dir / "toolkit.json"

    @property
    def bridges_db(self) -> Path:
        return self.data_dir / "bridges.json"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
