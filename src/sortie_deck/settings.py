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
    # --- memory governance（记忆治理，见 docs/plans/2026-10-21-memory-governance.md）---
    # 滚动总结的角色分层摘要：协调角色用 LLM 提炼决策/结论/风险，执行角色留增量；
    # false 或无 LLM 时降级为旧的截断流水
    memory_stage_summary_llm: bool = True
    # 反思沉淀（程序性记忆）：stage 成功后异步生成同角色可复用的 lesson；
    # agent 作用域 lesson 数量上限，超限按 updated_at 淘汰最旧，防无限堆积
    memory_reflect_enabled: bool = True
    memory_reflect_agent_lesson_cap: int = 20
    # 新近度加权：打分时对最近 N 天内更新的记忆额外加分（线性衰减到 0）
    memory_recency_bonus_days: float = 3.0
    # 冲突检测阈值（本地规则兜底）：同 scope/scope_id/kind 且分词重合度 ≥ 该值
    # 视为同主题，旧条目标记 superseded（失效不删除，bi-temporal 思路）
    memory_conflict_threshold: float = 0.8
    # 条目记忆检索路径：local（默认，关键词打分+新近度）| weknora（混合检索+rerank，
    # graph 模式走图谱扩展，失败逐级降级 hybrid → 本地打分，见 docs/plans/
    # 2026-10-21-memory-weknora-retrieval.md）
    memory_retrieval: str = "local"
    # 记忆镜像专用 WeKnora 知识库 id；缺省回落主库 TDT_WEKNORA_KB_ID
    weknora_memory_kb_id: str | None = None
    # 内置记忆元工具（memory_search / memory_read）：Agent 在 stage 回复里声明
    # 意图，编排层兑现后经下阶段 brief 回传结果
    memory_tools: bool = True
    # 单阶段 memory_read 预算与单条全文截断（防上下文堆积）
    memory_read_budget_per_stage: int = 4
    memory_read_max_chars: int = 2000
    # OpenCode server (deployed alongside the app in the cloud, started via
    # `opencode serve`). The provider talks HTTP to it — never a local CLI.
    opencode_base_url: str | None = None
    opencode_api_key: str | None = None
    opencode_timeout: float = 600.0
    # opencode 探测请求超时（probe 走轻量 GET，独立于 send 的长超时）
    opencode_http_timeout: float = 5.0
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
    # In-loop toolkit meta-tool (mode 2): executors may declare toolkit_search /
    # toolkit_pick intents in StageResult.meta; the orchestration layer resolves
    # them (recall → validate → float). Independent from loadout_auto so the
    # in-loop path stays available even when JIT pre-loading is off.
    toolkit_search: bool = True
    # Toolkit 召回后端（方案 A：复用 WeKnora 混合检索 + rerank）：
    #   local   = 三层加权词元打分（默认，零依赖）
    #   weknora = 条目镜像进专用 KB，走 hybrid-search（向量+BM25→RRF→bge-reranker）
    # 复用 weknora_base_url / weknora_api_key；KB id 为空则始终降级 local。
    toolkit_recall_backend: str = "local"
    toolkit_weknora_kb_id: str | None = None
    # 阶段入口主动召回（模式 1.5）：每个阶段节点执行前由编排层直接 recall
    # （查询 = 角色+阶段名+阶段标签+任务 brief 摘要），分数过阈值的条目经探活、
    # 去重后注入阶段 brief；mcp 回写 stage_toolkits、skill 并入 toolkit_floats。
    # 关闭时零行为变更；与 loadout_auto（创建时 JIT）、toolkit_search（模式 2）互不影响。
    loadout_stage_recall: bool = True
    # 主动召回的最低分阈值：召回结果全部低于该分数时不注入（"无需则不调用"的实现）。
    # 打分口径同 loadout._score（能力标签 1.0 / tags 0.6 / 正文 0.25，WeKnora 复合分 0-1）。
    loadout_recall_min_score: float = 0.2
    # --- MCP session pool ----------------------------------------------------
    # stdio servers get one instance per (item, task); reused within the task,
    # idle-evicted after ttl (no in-flight calls) with a global LRU cap.
    mcp_pool_enabled: bool = True
    mcp_pool_ttl_seconds: float = 300.0
    mcp_pool_max_instances: int = 50
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
    # 检索模式：hybrid = hybrid-search（快，无图谱）；graph = knowledge-chat 管线
    # （图谱实体扩展召回，Neo4j 需开启，慢且每次检索触发一次 LLM 调用）
    weknora_retrieval_mode: str = "hybrid"  # hybrid | graph
    # graph 模式下 knowledge-chat SSE 流的超时（秒）
    weknora_chat_timeout: float = 90.0
    # Chat bridges
    slack_signing_secret: str | None = None
    feishu_verification_token: str | None = None
    # Production polish
    log_level: str = "INFO"
    stage_timeout_seconds: float = 900.0
    # --- git 回写链路（任务分支 + GitLab MR）---------------------------------
    # 任务分支命名前缀：单仓库 sortie/<id>，多仓库 sortie/<id>-<repo>
    git_branch_prefix: str = "sortie"
    # push 时机：on_stage_success（编码阶段成功即 push）| on_approval（HITL 放行后
    # 重入编码阶段时 push）| off（不回写，仅本地沙盒）
    git_push_mode: str = "on_stage_success"
    # GitLab API（自建/极狐）。base_url 形如 https://gitlab.example.com；
    # api_token 未配置时回退用各仓库 RepoConfig.token 调 API。
    gitlab_base_url: str = ""
    gitlab_api_token: str | None = None
    # push 全部成功后删除 workspace/repos 工作副本（分支已上 GitLab，副本即缓存；
    # QA 打回重入编码时重新浅克隆）。磁盘安全阀，建议保持开启。
    git_cleanup_after_push: bool = True
    # TTL 兜底扫描：非运行中任务的 workspace 保留时长（秒），超时删除；
    # 有未提交/未推送改动（脏状态）的改用 dirty TTL，避免误删未上库代码。
    worktree_ttl_seconds: float = 86400.0
    worktree_dirty_ttl_seconds: float = 604800.0
    # 磁盘水位防护：clone 前检查可用空间，低于该值（MB）拒绝新 clone
    min_disk_free_mb: int = 2048

    def resolved_token_secret(self) -> str:
        secret = (self.token_secret or "").strip()
        if self.env.strip().lower() == "production" and (
            not secret or secret == "tdt-dev-secret"
        ):
            raise RuntimeError(
                "TDT_TOKEN_SECRET must be set to a non-default value when TDT_ENV=production"
            )
        return secret or "tdt-dev-secret"

    # --- mission layout（任务目录收敛，见 layout.py）-------------------------
    # 任务专属数据收敛到 data/missions/<id>/{memory,artifacts,workspace,chat}。
    # 旧布局（data/artifacts、data/worktrees、data/rooms）由启动迁移搬运。

    @property
    def artifacts_dir(self) -> Path:
        # 兼容保留：仅作旧布局路径引用（doctor/迁移探测），本地产物存储已改由
        # missions_dir + mission 布局派生（见 artifacts_s3.build_artifact_store）。
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
        # 旧名保留（编码执行器调用点众多）：现在指向 missions 根，
        # 任务沙盒实际路径为 worktrees_dir/<id>/workspace（见 plugins.coding.ensure_worktree）。
        return self.data_dir / "missions"

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
    def knowledge_bases_db(self) -> Path:
        return self.data_dir / "knowledge_bases.json"

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
