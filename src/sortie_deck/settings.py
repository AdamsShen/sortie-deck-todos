from __future__ import annotations

import os
import socket
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TDT_", env_file=".env", extra="ignore")

    data_dir: Path = REPO_ROOT / "data"
    # 默认绑 0.0.0.0 允许局域网访问；仅本机调试时可显式设 TDT_HOST=127.0.0.1
    host: str = "0.0.0.0"
    port: int = 8787
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    postgres_uri: str | None = None
    deploy_executor: str = "mock"  # mock | deploy_shell
    eng_concurrency: int = 3
    webhook_timeout: float = 5.0
    # development | production — production refuses empty / default token secrets
    env: str = "development"
    token_secret: str = "tdt-dev-secret"
    # 仅 sqlite：JSON 存储已废弃（整文件读改写，多任务并发会互相覆盖）。
    # 保留该字段只为在配置写错时给出明确报错，以及兼容旧的 .env。
    storage: str = "sqlite"
    # 对外可访问地址（生成 MCP / CLI 配置片段用）。留空时回落 http://{host}:{port}
    public_base_url: str = ""
    # 前端构建产物目录；留空时按仓库布局推导（容器里请显式指定 TDT_WEB_DIST）
    web_dist: str = ""
    # MCP 端点允许的 Host 白名单（逗号分隔）；留空表示关闭 DNS-rebinding 防护
    mcp_allowed_hosts: str = ""
    # 每个 API Key 每分钟请求上限（进程内软限流，多副本各算各的）
    api_key_rate_limit_per_minute: int = 240
    # Optional LLM features (require TDT_LLM_API_KEY / OPENAI_API_KEY, or Claude CLI fallback)
    # Defaults on: plan_pipeline_smart / room replies fall back to heuristic/templates when unavailable
    pipeline_llm: bool = True
    room_llm: bool = True
    # When true, LlmClient may use `claude -p` if no HTTP API key is set
    llm_cli_fallback: bool = True
    # CLI 回退的独立超时（秒）。它挂在阶段关键路径上，一次挂死就会拖垮整个阶段
    llm_cli_timeout: float = 60.0
    # OpenAI-compatible HTTP (also read by LlmClient; prefer these over bare os.environ)
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    # mock | claude_code | cursor_cli | auto (detect CLI on PATH)
    default_coding_executor: str = "auto"
    # --- agent harness (system-wide capability) ------------------------------
    # Initiative-level harness; locked once the mission starts.
    # claude_code | dsh | opencode | codex | cursor | mock
    default_harness: str = "claude_code"
    # Probe cache TTL for harness availability checks (seconds)
    harness_probe_ttl: float = 60.0
    # When the chosen harness provider is unavailable, fall back to the legacy
    # stage executors (mock / claude_code CLI / ...) instead of failing the stage.
    harness_fallback: bool = True
    # 文档型阶段（PRD / 用例 / 技术方案）把 cwd 直接指向 artifacts/<stage>/，
    # 让契约产物在运行期就落进任务档案（见 docs/plans/2026-10-30-artifact-direct-write.md）。
    # 关闭后退回旧行为：cwd=workspace，阶段结束再从 workspace 归档。
    harness_artifact_direct_write: bool = True
    # 任务终态/TTL 回收 workspace 前，把未绑定仓库任务的产出代码（约定写在
    # workspace/repos/）打包成 zip 归档到 artifacts/code-archive/；绑定仓库任务
    # 的代码在远端 git（clone 特征 repos/<name>/.git），跳过归档。
    archive_workspace_on_cleanup: bool = True
    # 单个归档 zip 的累计体积上限（MB），超出停止添加并在 manifest 标记 truncated
    archive_workspace_max_mb: int = 500
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
    # Codex `exec --sandbox` 档位（read-only | workspace-write | danger-full-access）。
    # 默认 workspace-write：编码阶段需要写文件；会话续跑沿用首次档位。
    codex_sandbox: str = "workspace-write"
    # codex 非交互审批策略：never = 沙箱边界内不弹审批；空 = 用 CLI 默认
    codex_ask_for_approval: str = "never"
    # workspace-write 下是否允许网络（装依赖需要）；False 时只能跑本地已有依赖
    codex_network_access: bool = True
    # 建队规划官发现目录外能力时会生成 forge 草稿；草稿未经人工审核前禁止出击。
    # 关闭后退化为旧行为（未发布 agent 直接随任务跑），仅建议临时排障时使用。
    forge_require_review: bool = True
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
    # --- 权限与环境阻塞（见 docs/plans/2026-11-09-权限阻断与环境阻塞熔断.md）---
    # SDK 的 deny 规则在 bypassPermissions 下仍生效；默认空表示不改变既有工具策略
    permission_deny_rules: str = ""
    # worker 启动自检：跑一条解释器命令，失败只告警（不判定任务、不跳过阶段）
    permission_preflight: bool = True
    # 环境阻塞（命令审批 / 无运行时证据）时第 1 轮即暂停，不再打回开发返工
    env_block_pause_enabled: bool = True
    # 兜底：同一失败签名连续出现 N 次自动暂停；0 表示关闭
    max_consecutive_same_failure: int = 3
    # 传给 claude-agent-sdk 的 setting_sources："" 按 CLI 默认加载
    # user/project/local；"none" 为 SDK 隔离模式（不加载本机 settings 与插件
    # hooks，避免开发机的 hook 打断云端 stage）；其余为逗号分隔的 source 列表。
    harness_setting_sources: str = ""
    # Claude Code 沙箱配置文件（SDK settings 路径）。留空 = 不注入，保持现有权限模式；
    # docker 部署指向镜像内 /etc/sortie/claude-sandbox.json（见 docker/claude-sandbox.json）。
    harness_settings_file: str = ""
    # --- DSH（DeepSeek Harness）provider --------------------------------------
    # 官方 Python SDK 走 stdio NDJSON-RPC 驱动随包附带的 dsh 运行时（不需要系统
    # Node）；会话落在任务级 dsh_home 下，每轮新进程靠 resume 续跑，见
    # docs/plans/2026-11-09-dsh-harness-provider.md。
    dsh_profile: str = "sdk"
    # 模型路由（provider/model 是 DSH 侧的概念，与 llm_* 无关）
    dsh_llm_provider: str = "deepseek-official"
    dsh_model: str = "deepseek-v4-flash"
    # 输出上限；0 = 用 profile 默认（实测 256000）
    dsh_max_tokens: int = 0
    # 推理强度 low|medium|high；空 = 用 profile 默认
    dsh_reasoning_effort: str = ""
    # read-only | workspace-write | danger-full-access。
    # workspace-write 与 profile 的 ask 审批组合：越界写直接 fail closed。
    dsh_permission_mode: str = "workspace-write"
    # 任务级 DSH_HOME 根目录；空 = <data_dir>/dsh_home/<initiative_id>。
    # 禁止指向 ~/.dsh：会把宿主机会话/技能带进任务。
    dsh_home_root: str = ""
    # 凭据：空则读环境变量 DEEPSEEK_API_KEY；base_url 可指向兼容代理
    dsh_api_key: str | None = None
    dsh_base_url: str | None = None
    # 单轮请求超时（秒）；0 = 回落 coding_timeout_seconds。
    # DSH 没有回合上限配置，长回合只能靠超时兜底。
    dsh_request_timeout_seconds: float = 0.0
    # 会话事件默认随请求上传到 DeepSeek（session-log-deepseek）；企业数据面必须关掉
    dsh_session_log_upload: bool = False
    # 不扫描宿主机用户级技能根（~/.agents/skills、$DSH_HOME/skills）
    dsh_isolate_skills: bool = True
    # 用任务级 HOME 隔离（默认关闭：git commit 等仍需要 ~/.gitconfig）
    dsh_isolate_home: bool = False
    # --- 上下文成本控制（见 docs/plans/2026-10-27-harness-context-cost.md）----
    # 上游产物在 prompt 里只给「摘要 + 路径」，让 agent 需要时自行 Read。
    # 关闭后回落为旧行为（prd.md 全文注入），用于灰度对比与回退。
    prompt_artifact_briefs: bool = True
    # 小于该字符数的产物直接内联全文，避免为小文件多付一次摘要调用
    prompt_inline_artifact_chars: int = 1200
    # 单份产物摘要的 token 上限（估算口径 4 字符 ≈ 1 token）
    prompt_artifact_summary_max_tokens: int = 200
    # 单份摘要的 LLM 超时（秒）；超时/异常一律回落规则摘要，不让阶段失败
    prompt_artifact_summary_timeout: float = 12.0
    # false 时只用规则摘要（标题大纲/键名/diff 统计），不调 LLM
    prompt_artifact_summary_llm: bool = True
    # 严格 MCP 模式：只挂本阶段绑定/召回的 MCP（strict_mcp_config），
    # 不再从本机 ~/.claude.json 全量加载全局 mcpServers。
    # 置 false 可回退到旧的全量加载行为。
    harness_mcp_strict: bool = True
    # 会话内自动压缩窗口（token）。Claude Code 通过
    # CLAUDE_CODE_AUTO_COMPACT_WINDOW 控制，官方有效范围 100K–1M；
    # 0 表示不注入（沿用模型默认阈值，约 200K/967K 才触发）。
    harness_auto_compact_window: int = 200000
    # SDK 读取 CLI stdout 的单条 JSON 上限（字节）。claude-agent-sdk 默认 1 MiB，
    # agent 用 Read 打开截图时图片会 base64 进消息（`tool_use_result` 还会再带一份），
    # 单行超限会抛 SDKJSONDecodeError、整个阶段直接判失败（2026-11-05 实测：
    # 379 KB 的 Playwright 截图即触发）。官方 cookbook 建议图片场景用 10 MB。
    harness_max_buffer_bytes: int = 10 * 1024 * 1024
    # 阶段以 status=fail 收尾、且自审一轮都没跑成时，先按本预算做自检再放行下游
    # （0 = 关闭）。没有自检就直接进 QA，下游只能对着一份残缺交付判 fail，
    # 实测多花约 9 分钟（2026-11-05 贪吃蛇任务）。自检次数按阶段计。
    stage_self_check_attempts: int = 1
    # 单阶段最大 agent 轮次；0 表示不限制。用于兜住「反复打磨不收敛」的阶段。
    harness_max_turns: int = 0
    # 各阶段 agent 回合上限：`stage=轮次`，逗号分隔；未列出的阶段回落 harness_max_turns。
    # 注意这是 SDK 一次 query 内的 assistant turn 数（每次工具调用占 1 个 turn）。
    # 设太小会把 agent 截断成空回复、阶段直接 fail：实测 intake=2 / 6 都在
    # 30 秒内被截断（前几轮全是工具调用，没来得及输出文本，2026-11-04）。
    # 20 ≈ 正常收尾需要的 8-10 轮 + 充分余量，同时封死自由发挥式无限循环。
    stage_max_turns: str = "intake=20"
    # 禁止 agent 拉小群的阶段：逗号分隔。集结拉群 = 三队员回复 + 回注 + 收尾轮，
    # 直接把轮次翻倍；需求对齐最多问一个角色，默认在 intake 禁掉。
    subroom_disabled_stages: str = "intake"
    # 缺少契约文件即判失败的阶段：逗号分隔。默认只对 intake 生效——历史行为是
    # 把缺失产物包成「回复说明」继续走（mock harness 依赖），但 intake 缺
    # intake.md 时包装件会掩盖「越界写 PRD」的事实，所以这里硬失败。
    strict_contract_stages: str = "intake"
    # --- 多镜头自审（见 docs/plans/2026-11-02-多镜头自审方案与改动说明.md）---
    # 编码阶段结束后，由编排层在同一 provider 会话里追加 N 轮不同视角的审查；
    # 每轮追加 logs/<stage>/review.jsonl，收尾写 review.json 与 review.md 产物。
    # 关闭后完全退回旧行为（无额外轮次、无 review 产物）。
    review_lenses_enabled: bool = True
    # 轮次：3 = 正确性/边界/测试缺口（轻量）；>=5 = 再加并发/安全
    review_lens_rounds: int = 3
    # 生效档位：build（编码）/ integrate（联调），逗号分隔。
    # 自测阶段（*_selftest）已脚本化（runner 一次跑完），不再触发自审；
    # 配成 selftest 也不会生效，见 review_lens.stage_in_review_scope。
    review_lens_stages: str = "build"
    # 单轮审查超时（秒）；0 = 按 harness 自适应（见 review_lens_timeout_by_harness）。
    # 超时只把该轮标 failed，不打断阶段。
    review_lens_timeout_seconds: float = 0.0
    # 各 harness 的自适应单轮超时：`harness=秒`，逗号分隔。
    # 编码型 agent loop 单回合实测常在 5–10 分钟（2026-11-03 HK 实测 claude_code 单回合
    # 324s），统一 300s 会让每一轮都卡在超时线上，白等 3×5 分钟。
    review_lens_timeout_by_harness: str = (
        "claude_code=900,codex=900,cursor=900,opencode=900,dsh=900,mock=30"
    )
    # 自审熔断：任一轮超时后最多再补一轮，其余轮次标 skipped（不再各等一个超时窗口）
    review_lens_circuit_breaker: bool = True
    # 仍有 blocking 发现时的处理：hitl（默认，转人工门禁）/ fail / ignore
    review_blocking_policy: str = "hitl"
    # --- 自适应自审：先判任务复杂度，再决定跑不跑 / 跑几轮 -----------------------
    # 复杂度由编排层规则打分（改动文件数 / diff 行数 / 模块跨度 / 高风险词），
    # 不额外调用模型；判定结果与信号写进 review.json 与 meta.review，可复盘调阈值。
    review_adaptive: bool = True
    # simple 档：文件数 <= N、diff 行数 <= M、且只动 1 个模块，才判简单
    review_complexity_simple_max_files: int = 3
    review_complexity_simple_max_diff_lines: int = 500
    # complex 档：文件数 >= N、或 diff 行数 > M、或跨模块数 >= K，直接判复杂
    # 默认口径：3 文件/500 行以内算简单，10 文件/1500 行以内算中等，再往上算复杂
    review_complexity_complex_min_files: int = 11
    review_complexity_complex_min_diff_lines: int = 1500
    review_complexity_complex_min_modules: int = 3
    # 高风险词（逗号分隔）：命中即判 complex，再小的改动也不省自审。
    # 中文按子串匹配，英文按单词边界匹配（auth 不会命中 author）。
    review_complexity_high_risk_keywords: str = (
        "重构,迁移,并发,支付,权限,鉴权,多端,架构,协议,加密,审计,"
        "refactor,migration,concurrency,payment,auth,oauth,jwt,encryption"
    )
    # 各档位实际跑的轮次：0 = 按老逻辑（不做 LLM 自审）
    review_rounds_simple: int = 0
    review_rounds_standard: int = 3
    review_rounds_complex: int = 5
    # --- 影子仓库：无 git 任务的阶段改动采集 -------------------------------------
    # 未绑定仓库的任务把代码写在 workspace/repos/ 下，项目本身没有 git，
    # 自审只能拿到文件清单 -> 模型逐个 Read。开启后由 shadow_git 在 mission 根
    # 维护 .shadow.git（独立 GIT_DIR，不污染项目），阶段基线 + 末端采集真实 diff。
    shadow_git_enabled: bool = True
    # 超过该字节数的文件不整体进 prompt（降级为符号清单），避免大文件撑爆预算
    shadow_git_max_file_bytes: int = 1_048_576
    # 自审 prompt 内 diff 的总字符上限与单文件行数上限；diff.patch 产物不受影响
    review_diff_max_chars: int = 60_000
    review_diff_max_file_lines: int = 2_000
    # 自审轮禁用的工具（逗号分隔）。diff 已随 prompt 给出，模型仍逐文件 Read 是
    # 主要耗时源（2026-11-06 实测同一镜头：允许 Read 100.4s，禁用后 22.9s）。
    # 默认只禁只读检索类，保留 Edit/Write 以便 correctness / boundaries 就地修。
    # 留空 = 不禁用（回滚开关）。
    review_disallowed_tools: str = "Read,Bash,Grep,Glob"
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
    # --- IM 集成（工作台「设置 → IM 集成」）---------------------------------
    # 接入凭据的加密主密钥。优先 TDT_SECRET_KEY；未配置时回落 git_secret_key，
    # 使既有部署不改 .env 就能用（且不会让旧仓库凭据解不开）。
    secret_key: str | None = None
    # 接入记录 sqlite；空则用 <data_dir>/im.sqlite
    im_db_path: Path | None = None
    # 只允许一个进程建立长连接：飞书/钉钉集群推送不广播、企微新连接会踢旧连接，
    # 多副本多活会丢消息甚至互相踢掉。API 多副本时只给一个实例开。
    im_consumer: bool = True
    # 钉钉按钮「签名链接」的有效期（秒）。链接可被转发，必须限时。
    im_action_ttl_seconds: float = 86400.0
    # --- mission guardrails -------------------------------------------------
    # Runaway protection for long multi-agent missions (see guardrails.py).
    guardrail_max_tokens: int = 2_000_000
    guardrail_max_seconds: float = 21600.0  # 6h
    guardrail_max_subrooms: int = 20
    guardrail_max_handoffs: int = 40
    # Run queue: inline (default create_task) | sqlite (lease + worker loop)
    run_backend: str = "inline"
    # Worker 身份：留空则派生 "{hostname}-{pid}"。
    # 写死同一名字会让 run_queue.claim() 里 `lock[0] != worker_id` 的
    # initiative 互斥判断失效，多副本部署会把同一个任务重复执行。
    worker_id: str = ""
    run_lease_seconds: int = 120
    # 本进程是否消费运行队列。false = 只做 API/MCP 前端，执行交给 `sortie worker`。
    # 多副本部署时必须只有一个角色跑 worker，否则同一 job 会被抢两次。
    run_worker: bool = True
    # mission 事件表保留天数（0 = 永久保留）。事件是 SSE 的唯一数据源，
    # 需要覆盖「关掉工作台几天后再打开」的补播窗口。
    event_retention_days: int = 7
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
    # 阶段入口 RAG 召回的上限（秒）：知识库是增强项，graph 模式单次可达数十秒，
    # 超时即降级为空 brief，绝不能让检索拖住整个阶段执行
    harness_rag_timeout_seconds: float = 5.0
    # Chat bridges
    slack_signing_secret: str | None = None
    feishu_verification_token: str | None = None
    # 飞书「URL 回调」模式的 Encrypt Key（长连接模式不需要；配置后回调体加密）
    feishu_encrypt_key: str | None = None
    # Production polish
    log_level: str = "INFO"
    # 单阶段执行上限（秒）。harness 跑完整技术方案实测 15 分钟量级
    # （2026-10-27 `eng_backend_tech_design` 540 行方案 14m47s 才落盘），
    # 900s 会让"已产出但未返回"的阶段被判超时，故默认放宽到 30 分钟。
    stage_timeout_seconds: float = 1800.0
    # 开发/编码类阶段（ENG_BUILD_STAGES）的默认单阶段超时（秒）。
    # 编码 agent 要写多模块代码并跑自测，30 分钟不够（2026-11-04 本机实测
    # eng_backend 在 1800s 被砍时 agent 仍在正常产出），默认放宽到 1 小时。
    eng_stage_timeout_seconds: float = 3600.0
    # 测试/复验类阶段（qa_verify / *_verify）的默认单阶段超时（秒）。
    # 复验需要起服务 + 跑完整用例集，默认给 30 分钟；需要收紧时单独设
    # TDT_QA_STAGE_TIMEOUT_SECONDS，不影响文档/编码阶段的默认值。
    qa_stage_timeout_seconds: float = 1800.0
    # 阶段级超时覆盖：`stage=秒,stage2=秒`，支持 `*` 通配（`*_tech_design=600`）。
    # 优先级低于航线 spec 的 timeout_seconds，高于按阶段类别取的默认值。
    # 技术方案是文档型阶段（cwd 直指产物目录），600s 足够；套全局 1800s 会让
    # 一个卡住的设计阶段白等半小时（2026-11-05 实测超时 1800s 后被强杀）。
    stage_timeout_overrides: str = "*_tech_design=600"
    # 阶段级工具禁用：`stage=Tool1|Tool2,...`，支持 `*` 通配。
    # 技术方案阶段只该产出文档：放开 Bash 会让 agent 顺手做 spike，把「写方案」
    # 变成「提前实现」（2026-11-05 实测 eng_web_tech_design 58 步里 45 次 Bash）。
    stage_disallowed_tools: str = "*_tech_design=Bash"
    # --- OpenTelemetry 全链路追踪（见 docs/plans/2026-10-27-otel-full-tracing.md）---
    # 默认关；开启且 otel_endpoint 非空时才真正建 provider 导出 span。
    # 只装了 opentelemetry-api 没装 [otel] extra 时自动降级为 no-op，不影响启动。
    otel_enabled: bool = False
    otel_endpoint: str = ""  # OTLP/HTTP，如 http://127.0.0.1:4318
    otel_service_name: str = "sortie-deck"
    # head sampling 比例：本项目单任务 15-30 span，量小，默认全采
    otel_sample_ratio: float = 1.0
    # 是否把 prompt/completion 全文写进 span；含 PII，仅排障期临时打开
    otel_capture_content: bool = False
    # 形如 "authorization=Bearer xxx"：部分 OTLP 网关需要鉴权
    otel_headers: str = ""
    # --- git 回写链路（任务分支 + GitLab MR）---------------------------------
    # 任务分支命名前缀：统一 sortie/<id>-<repo>（单仓也带 repo 后缀，见 D6）
    git_branch_prefix: str = "sortie"
    # 两级凭据（见 docs/plans/2026-11-03-git-credentials-two-tier.md）：
    # 加密主密钥（urlsafe base64 32B，逗号分隔可做轮换，新钥在前）。
    # 未配置时拒绝保存任何 git 凭据，绝不降级为明文。
    git_secret_key: str | None = None
    # 凭据与仓库注册表 sqlite 路径；空则用 <data_dir>/git_registry.sqlite
    git_registry_path: Path | None = None
    # 绑定仓库时的 ls-remote 预检超时（秒）
    git_preflight_timeout: float = 10.0
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
    # 规划官会话（intake）目录 TTL：开了会话但从未建队的目录按此回收。
    # 比 workspace 的 1 天更长，给用户留足「先想清楚再建队」的时间。
    intake_ttl_days: float = 7.0
    # 磁盘水位防护：clone 前检查可用空间，低于该值（MB）拒绝新 clone
    min_disk_free_mb: int = 2048

    def resolved_secret_key(self) -> str | None:
        """凭据加密主密钥：``TDT_SECRET_KEY`` 优先，回落 ``git_secret_key``。

        回落是为了兼容已上线环境：它们只配了 ``TDT_GIT_SECRET_KEY``，
        新增 IM 凭据不该要求用户再去生成一把新密钥（换了还会导致旧凭据解不开）。
        """
        return (self.secret_key or "").strip() or (self.git_secret_key or "").strip() or None

    def im_db(self) -> Path:
        return self.im_db_path or (self.data_dir / "im.sqlite")

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
    def events_sqlite(self) -> Path:
        """mission 事件日志（SSE 的唯一数据源，多进程共享）。"""
        return self.data_dir / "events.sqlite"

    @property
    def metrics_sqlite(self) -> Path:
        """阶段指标（耗时/工具步数/review 轮次），供设置页耗时分析查询。"""
        return self.data_dir / "metrics.sqlite"

    @property
    def api_keys_db(self) -> Path:
        """用户级 API Key（远程 MCP / CLI 的长期凭据）。"""
        return self.data_dir / "api_keys.sqlite"

    @property
    def resolved_public_base_url(self) -> str:
        """对外可访问地址：显式配置优先，否则按 host:port 推断。

        MCP / CLI 的配置片段里必须给出「用户笔记本能访问到」的地址，
        监听 0.0.0.0 时不能把这个值直接抄进片段。
        """
        configured = (self.public_base_url or "").strip().rstrip("/")
        if configured:
            return configured
        host = self.host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    @property
    def resolved_web_dist(self) -> Path:
        """前端构建产物目录；容器里请用 TDT_WEB_DIST 显式指定。"""
        configured = (self.web_dist or "").strip()
        if configured:
            return Path(configured).expanduser()
        return REPO_ROOT / "apps" / "web" / "dist"

    @property
    def resolved_worker_id(self) -> str:
        """实际 worker 标识：显式配置优先，否则 hostname+pid 保证多副本唯一。"""
        configured = (self.worker_id or "").strip()
        if configured:
            return configured
        return f"{socket.gethostname()}-{os.getpid()}"

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
