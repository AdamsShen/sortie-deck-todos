export type Lang = "zh" | "en";

export type View =
  | "home"
  | "tasks"
  | "intake"
  | "squad"
  | "knowledge"
  | "memory"
  | "agents"
  | "toolkit"
  | "forge"
  | "subrooms"
  | "decisions"
  | "admin";

export type UserRole = "admin" | "member";

/** 账号状态：注册后为 pending，管理员审核通过为 active，拒绝为 rejected */
export type UserStatus = "pending" | "active" | "rejected";

export type InitiativeStatus =
  | "draft"
  | "running"
  | "waiting_hitl"
  | "stopped"
  | "failed"
  | "done";

/** Built-in role slots. The authoritative list lives in the backend role
 *  registry (`/api/roles`) and is no longer a closed set — custom / forge
 *  created roles (e.g. `parenting`) are valid slots too. */
export const BUILTIN_AGENT_SLOTS = [
  "product",
  "design",
  "eng",
  "eng_ios",
  "eng_android",
  "eng_web",
  "eng_backend",
  "eng_agent",
  "qa",
  "deploy",
] as const;

export type AgentSlot = string;

export type CodingExecutor = "mock" | "claude_code" | "cursor_cli";
export type PipelineTrack = "auto" | "express" | "standard" | "default" | "research";
export type Complexity = "auto" | "trivial" | "normal" | "complex";

export interface PipelineStageInfo {
  id: string;
  label: string;
  label_en?: string;
  role?: string;
  hitl_after?: boolean;
  on_fail?: string | null;
  on_reject?: string | null;
  parallel_group?: string | null;
  branch?: string | null;
  depends_on?: string[] | null;
  output_contract?: string[];
}

export interface PipelineTrackInfo {
  id: string;
  name: string;
  description: string;
  complexity?: string;
  stages: PipelineStageInfo[];
  stage_count: number;
}

export interface PipelineProposal {
  agent?: string;
  agent_title?: string;
  track_hint?: string;
  rationale?: string;
  stages?: PipelineStageInfo[];
  status?: string;
}

export type TaskKind = "auto" | "delivery" | "knowledge";
export type PipelineTier = "auto" | "express" | "standard" | "full";

export interface TierStageInfo {
  id: string;
  label: string;
  label_en?: string;
}

/** 单档定义：阶段链路 + 预估时长，来自 GET /api/pipelines/tiers。 */
export interface TierDefinition {
  tier: Exclude<PipelineTier, "auto">;
  label: string;
  label_en: string;
  description: string;
  stages: TierStageInfo[];
  stage_count: number;
  eta_minutes: [number, number];
  max_roles: number;
}

export interface TierGroup {
  task_kind: Exclude<TaskKind, "auto">;
  label: string;
  tiers: TierDefinition[];
}

/** 耗时分析：任务摘要 / 单阶段指标 / 单任务详情。 */
export interface InsightMissionBrief {
  id: string;
  title: string;
  status: string;
  current_stage: string;
  created_at: string;
  updated_at: string;
}

export interface InsightMissionSummary extends InsightMissionBrief {
  has_metrics: boolean;
  stage_count: number;
  total_duration_ms: number;
  tool_calls: number;
  review_rounds: number;
  /** 首阶段起点 / 末阶段终点（epoch 秒）；无指标的老任务为 null */
  first_started_at: number | null;
  last_ended_at: number | null;
}

export interface InsightStage {
  stage: string;
  role: string;
  harness: string;
  attempt: number;
  status: string;
  started_at: number;
  ended_at: number;
  offset_ms: number;
  duration_ms: number;
  tool_calls: number;
  review_rounds: number;
  review_verdict: string;
  meta: Record<string, unknown>;
}

export interface InsightSpan {
  started_at: number;
  ended_at: number;
  duration_ms: number;
}

export interface InsightMissionDetail {
  mission: InsightMissionBrief;
  stages: InsightStage[];
  span: InsightSpan | null;
}

export interface InsightTotals {
  mission_count: number;
  total_duration_ms: number;
  tool_calls: number;
  review_rounds: number;
}

export type KnowledgeCategory = "playbook" | "runbook" | "lore" | "reference";

export type KnowledgeBaseScope = "global" | "user" | "task";

export interface KnowledgeBaseInfo {
  id: string;
  name: string;
  description: string;
  scope: KnowledgeBaseScope;
  /** global→"global"；user→所有者用户 id；task→关联任务（initiative）id */
  scope_id: string;
  created_by?: string;
  creator_name?: string;
  created_at?: string;
  updated_at?: string;
  doc_count?: number;
}

export type MemoryScope = "workspace" | "user" | "mission" | "agent";

export type MemoryKind = "fact" | "preference" | "episode" | "lesson";

export type Visibility = "public" | "private";

export type ToolkitKind = "tool" | "mcp" | "skill";

export type ToolRuntime = "cli" | "http" | "function" | "plugin";

export type McpTransport = "stdio" | "sse" | "http";

export interface SkillScript {
  path: string;
  content: string;
  language?: string;
  description?: string;
}

export type ToolkitCategory =
  | "engineering"
  | "product"
  | "qa"
  | "ops"
  | "knowledge"
  | "browser"
  | "office"
  | string;

export interface ToolkitItem {
  id: string;
  kind: ToolkitKind;
  name: string;
  summary: string;
  runtime?: ToolRuntime | null;
  endpoint?: string;
  transport?: McpTransport | null;
  command?: string;
  url?: string;
  args?: string[];
  env?: Record<string, string>;
  body?: string;
  scripts?: SkillScript[];
  config?: Record<string, unknown>;
  category?: string;
  tags: string[];
  featured?: boolean;
  enabled: boolean;
  source?: string;
  /** 上下文负载策略：always=常驻（每轮注入，计入预算）/ defer=按需（tool_search 检索后上浮） */
  load?: "always" | "defer";
  author_id?: string;
  author_name?: string;
  created_at?: string;
  updated_at?: string;
}

export const TOOLKIT_CATEGORIES: ToolkitCategory[] = [
  "engineering",
  "product",
  "qa",
  "ops",
  "knowledge",
  "browser",
  "office",
];

/** 市场精选技能（GET /api/market/skills） */
export interface MarketSkill {
  key: string;
  name: string;
  summary: string;
  src: string;
  category: string;
  tags: string;
  installed?: boolean;
}

/** MCP 连接测试结果（POST /api/toolkit/mcp/test） */
export interface McpTestResult {
  ok: boolean;
  tools?: string[];
  error?: string;
  log?: string[];
}

/** skill-install 优化草稿（POST /api/toolkit/optimize_skill） */
export interface SkillDraft {
  draft_id: string;
  name: string;
  category: string;
  original: string;
  optimized: string;
  backend: string;
}

export interface PublicUser {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
  status: UserStatus;
}

export interface LoginResponse {
  token: string;
  user: PublicUser;
}

/** 用户数据导出载荷（GET /api/export/user）；schema_version=1 起字段只增不改 */
export interface UserDataExport {
  meta: {
    schema_version: number;
    exported_at: string;
    owner: string;
    app: string;
  };
  profile: Record<string, unknown>;
  api_keys: Record<string, unknown>[];
  missions: Record<string, unknown>[];
  knowledge: Record<string, unknown>[];
  memory: Record<string, unknown>[];
  rooms: Record<string, unknown>[];
}

export interface RepoConfig {
  url: string;
  branch?: string;
  name?: string;
  /** workspace=管理员维护的工作区仓库；custom=用户自填 URL（凭据按 host 匹配） */
  source?: "workspace" | "custom";
  credential_id?: string | null;
  /** @deprecated 旧版明文凭据，后端已忽略 */
  token?: string | null;
}

/** 凭据对外视图：永远不含 secret，只有指纹 */
export interface GitCredentialInfo {
  id: string;
  scope: "workspace" | "user";
  label: string;
  host: string;
  scheme: "https" | "ssh";
  username: string;
  fingerprint: string;
  expires_at?: number | null;
  disabled_at?: number | null;
  created_at: number;
  last_used_at?: number | null;
}

/** 工作区仓库条目（全员可见，仅管理员可改） */
export interface WorkspaceRepoInfo {
  id: string;
  name: string;
  url: string;
  default_branch: string;
  role_slots: string[];
  credential_id: string;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

/** 个人登记的私有仓库（仅本人可见） */
export interface UserRepoInfo {
  id: string;
  name: string;
  url: string;
  default_branch: string;
  credential_id?: string | null;
  created_at: number;
}

export interface RoleAgent {
  id: string;
  role: string;
  title: string;
  persona: string;
  executor: string;
  status: string;
  avatar?: string | null;
  toolkit_ids?: string[];
  repos?: RepoConfig[];
  catalog_agent_id?: string | null;
}

export interface RoomParticipant {
  id: string;
  name: string;
  kind: "human" | "agent" | "system";
  role?: string | null;
  discussing?: boolean;
  joined_at?: string;
}

export interface ArtifactRef {
  kind: string;
  path: string;
  stage: string;
  created_at?: string;
  meta?: Record<string, unknown>;
}

export interface WorkspaceFile {
  path: string;
  name: string;
  size: number;
  mtime: number;
}

/** A user-supplied input file attached to a mission (PRD / plan / design …). */
export interface ExternalMaterial {
  id?: string;
  name: string;
  /** prd | plan | design | data | other (free-form, UI taxonomy) */
  kind: string;
  mime?: string;
  size?: number;
  /** Pinned materials ride along as persistent context in every prompt */
  pinned: boolean;
  /** Decoded text for text-like materials */
  text_excerpt?: string;
  /** 任务输入快照（missions/<id>/input/）中的相对路径；空表示只有摘录 */
  stored_path?: string;
  text_path?: string;
  sha256?: string;
  /** ok | skipped | failed | pending */
  extract_status?: string;
  extract_error?: string;
  added_at?: string;
}

export interface MissionWorkspace {
  initiative_id: string;
  artifacts_dir: string;
  artifacts_exists: boolean;
  worktree_dir: string;
  worktree_exists: boolean;
  git_repo: string | null;
  git_from_worktree: boolean;
  artifact_files: WorkspaceFile[];
  worktree_files: WorkspaceFile[];
}

/** 门禁推荐动作：QA 判失败时后端下发 reroute（返工），前端据此替换按钮语义 */
export interface HitlRecommendation {
  action: string;
  next_stage?: string | null;
  label?: string;
  hint?: string;
}

export interface HitlRequest {
  stage: string;
  prompt: string;
  allowed_actions?: string[];
  artifacts?: ArtifactRef[];
  /** 阶段裁决：目前只有 qa_verify 会带 */
  verdict?: { passed?: boolean | null } | null;
  /** 推荐动作：有值时用它当主按钮（例如「返工」），而不是默认的「放行」 */
  recommended_action?: HitlRecommendation | null;
}

/** POST /api/system/reveal 的返回：available=false 表示服务端无桌面环境，只能回传路径 */
export interface RevealResult {
  ok: boolean;
  available?: boolean;
  path: string;
  reason?: string;
  missing?: boolean;
  created?: boolean;
}

export interface TimelineEvent {
  id?: string;
  at?: string;
  stage: string;
  role?: string | null;
  kind?: "info" | "artifact" | "hitl" | "error" | "transition" | "consult" | string;
  message: string;
  data?: Record<string, unknown>;
}

export interface HarnessProbe {
  status: string;
  detail: string;
}

export interface HarnessInfo {
  name: string;
  label: string;
  probe: HarnessProbe;
  available: boolean;
}

export interface HarnessesResponse {
  harnesses: HarnessInfo[];
  default: string;
}

export interface MissionShare {
  user_id: string;
  username?: string;
  display_name?: string;
  added_by?: string;
  added_at?: string;
}

export interface Initiative {
  id: string;
  title: string;
  brief: string;
  template?: string;
  status: InitiativeStatus;
  current_stage: string;
  /** 当前阶段开始执行的时刻（服务端持久化）；刷新后用它续算已等待时长 */
  stage_started_at?: string | null;
  /** stage_started_at 属于哪个阶段，用于刷新后兜底展示阶段名 */
  stage_started_stage?: string;
  thread_id?: string;
  room_id?: string;
  // Initiative-locked agent harness (claude_code | opencode | codex | cursor | mock)
  harness?: string;
  harness_locked?: boolean;
  roles: RoleAgent[];
  participants: RoomParticipant[];
  artifacts: ArtifactRef[];
  /** External input materials; pinned ones are folded into the brief */
  materials?: ExternalMaterial[];
  timeline?: TimelineEvent[];
  pending_hitl?: HitlRequest | null;
  human_instruction?: string | null;
  created_at?: string;
  updated_at?: string;
  webhook_url?: string | null;
  /** 创建者用户 id（共享/删除权限判定用；旧数据为空） */
  created_by?: string | null;
  /** 共享给其他账号用户的记录（被共享者拥有除删除外的全部权限） */
  shared_with?: MissionShare[];
  meta?: Record<string, unknown>;
}

export interface RoomMessage {
  id: string;
  at: string;
  actor_kind: string;
  actor_name: string;
  role?: string | null;
  text: string;
  msg_type?: string;
  meta?: Record<string, unknown>;
}

export interface Dashboard {
  user: PublicUser;
  totals: {
    initiatives: number;
    draft: number;
    running: number;
    waiting_hitl: number;
    done: number;
    failed: number;
    stopped: number;
  };
  by_status: Record<string, number>;
  recent: Initiative[];
}

export interface KnowledgeDoc {
  id: string;
  title: string;
  summary: string;
  body: string;
  tags: string[];
  category: KnowledgeCategory;
  /** 所属知识库 id；空串表示存量文档（归属默认全局库） */
  base_id?: string;
  /** 文档后端来源：weknora 文档的解析状态需轮询展示 */
  backend?: "local" | "weknora";
  remote_id?: string | null;
  /** WeKnora 异步解析状态：processing / completed / failed；local 文档为空 */
  parse_status?: string;
  author_id?: string;
  author_name?: string;
  created_at?: string;
  updated_at?: string;
}

export interface MemoryEntry {
  id: string;
  scope: MemoryScope;
  scope_id: string;
  kind: MemoryKind;
  content: string;
  /** 一句话简介（写入时 LLM 生成，失败降级为 content 截断） */
  summary?: string;
  /** 简介生成状态：pending=生成中 / done=已生成 / failed=失败可重试；空串为旧数据 */
  summary_status?: "" | "pending" | "done" | "failed";
  /** 记忆治理状态：active=生效 / superseded=被取代 / expired=过期（失效不删除） */
  status?: "active" | "superseded" | "expired";
  /** 取代本条的新记忆 id（superseded 时的演变链指针） */
  superseded_by?: string;
  tags: string[];
  source?: string;
  author_id?: string;
  author_name?: string;
  created_at?: string;
  updated_at?: string;
}

/** 人设中的一条示例：典型输入 → 期望输出。 */
export interface PersonaExampleShape {
  situation: string;
  response: string;
}

/** 结构化人设（角色 / 背景 / 技能 / 工作流 / 示例 / 约束），保存时编译为 persona。 */
export interface PersonaSpecShape {
  role: string;
  background: string;
  skills: string[];
  workflow: string[];
  examples: PersonaExampleShape[];
  constraints: string[];
}

export interface PublishedAgent {
  id: string;
  slug: string;
  title: string;
  slot: AgentSlot;
  summary: string;
  persona: string;
  persona_spec?: PersonaSpecShape | null;
  tags: string[];
  visibility: Visibility;
  toolkit_ids?: string[];
  avatar?: string | null;
  author_id?: string;
  author_name?: string;
  version?: number;
  published?: boolean;
  created_at?: string;
  updated_at?: string;
}

export type RoleLoadout = Record<AgentSlot, string>;
export type RoleToolkitLoadout = Record<AgentSlot, string[]>;

// --- role registry / agent forge -----------------------------------------

export interface RoleSpec {
  role_key: string;
  title: string;
  title_en?: string;
  summary?: string;
  source?: "builtin" | "persona" | "custom" | "forge";
  coding?: boolean;
  executor?: string | null;
  handoff_targets?: string[];
  tags?: string[];
}

export interface AgentContractShape {
  role_key: string;
  title?: string;
  inputs?: string[];
  outputs?: string[];
  handoff_targets?: string[];
  timeout_seconds?: number;
  on_fail?: "retry" | "escalate" | "halt";
  tools_allowed?: string[];
}

export type ForgeDraftStatus = "pending-review" | "approved" | "rejected";

export interface ForgeDraft {
  id: string;
  role_key: string;
  title: string;
  summary: string;
  persona: string;
  persona_spec?: PersonaSpecShape | null;
  contract: AgentContractShape;
  tags: string[];
  status: ForgeDraftStatus;
  /** Self-check findings surfaced for the reviewer. */
  issues: string[];
  /** Near-duplicate existing roles, for de-duplication. */
  similar: { role_key: string; title: string; score: number }[];
  generator: "llm" | "template";
  requested_by?: string;
  review_note?: string;
  reviewed_by?: string;
  created_at?: string;
  updated_at?: string;
}

// --- sub-rooms ------------------------------------------------------------

export type SubRoomStatus = "open" | "closed" | "timeout" | "rejected";

export interface SubRoomRecord {
  id: string;
  initiative_id: string;
  parent_stage: string;
  initiator_role: string;
  goal: string;
  lead_role: string;
  participants: string[];
  questions: string[];
  max_rounds: number;
  max_tokens: number;
  timeout_seconds: number;
  depth: number;
  status: SubRoomStatus;
  conclusion: string;
  token_estimate: number;
  manager_digest?: string;
  reason?: string;
  created_at?: string;
  updated_at?: string;
}

// --- human decisions ------------------------------------------------------

export type DecisionStatus = "pending" | "answered" | "expired" | "cancelled";

export interface HumanDecisionRecord {
  id: string;
  decision_id: string;
  initiative_id: string;
  stage: string;
  question: string;
  context?: string;
  options: string[];
  assignee?: string;
  channel?: string;
  status: DecisionStatus;
  answer?: string;
  answered_by?: string;
  answered_at?: string;
  sla_seconds: number;
  deadline_at: string;
  fallback_action: string;
  fallback_reason?: string;
  created_at?: string;
  updated_at?: string;
}

export interface DecisionAnswerResult {
  decision: HumanDecisionRecord;
  accepted: boolean;
  duplicate: boolean;
  reason: string;
}

export interface LoadoutChoiceRecord {
  item_id: string;
  name: string;
  kind: string;
  capability?: string;
  score?: number;
  probe: "ok" | "degraded" | "unavailable" | "skipped";
  detail?: string;
  loaded: boolean;
  read_only?: boolean;
}

export class ApiError extends Error {
  code?: number;
  constructor(message: string, code?: number) {
    super(message);
    this.code = code;
  }
}

// --- API keys（远程 MCP / CLI 凭据）----------------------------------------

export type ApiKeyRecord = {
  id: string;
  user_id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
  /** 到期时间戳；null 表示长期有效（历史数据） */
  expires_at: number | null;
};

/** 一个 MCP 客户端的接入片段：目标文件 + 可直接粘贴的内容 */
export type McpClientSnippet = {
  id: "claude-code" | "codex" | "cursor" | "opencode" | string;
  label: string;
  target: string;
  language: string;
  code: string;
  /** 仅 Claude Code 提供等价命令行 */
  command?: string;
};

export type ApiKeyCreated = {
  key: ApiKeyRecord;
  plaintext: string;
  mcp: {
    url: string;
    /** 兼容旧字段，前端不再使用 */
    json?: Record<string, unknown>;
    command?: string;
    clients: McpClientSnippet[];
  };
};

// --- 阶段实时进度（SSE stage_progress）--------------------------------------

/** 阶段内子步骤：起止、自审开工、单轮起止、熔断跳过 */
export type StageProgressPhase =
  | "stage_start"
  | "stage_end"
  | "activity"
  | "review_begin"
  | "review_start"
  | "review_end"
  | "review_skip";

/** 执行器在阶段内部推送的实时进度，用于前端转圈与子步骤文案 */
export type StageProgress = {
  type: "stage_progress";
  initiative_id: string;
  stage: string;
  role?: string;
  phase: StageProgressPhase;
  harness?: string;
  status?: string;
  round?: number;
  total?: number;
  lens?: string;
  title?: string;
  timeout?: number;
  findings?: number;
  duration_ms?: number;
  /** activity 阶段：工具名与短目标（文件名 / 命令 / 检索词） */
  tool?: string;
  detail?: string;
  error?: string;
  message?: string;
  seq?: number;
  /** 服务端记录的阶段开始时刻（stage_start 事件带），优先作为计时锚点 */
  started_at?: string;
  /** 前端收到该事件的时间戳（用于展示已等待秒数） */
  at: number;
};

// --- IM 集成（设置 → IM 集成）------------------------------------------------
export type ImProviderId = "wechat" | "wecom" | "feishu" | "dingtalk" | "slack";
export type ImMode = "qr" | "stream" | "webhook";
export type ImStatus =
  | "unconfigured"
  | "connecting"
  | "connected"
  | "error"
  | "expired"
  | "disabled";

/** 渠道能力表：由后端 /api/im/providers 下发，前端不硬编码表单字段 */
export interface ImProvider {
  provider: ImProviderId;
  name: string;
  beta: boolean;
  recommended: boolean;
  modes: ImMode[];
  fields: string[];
  hint: string;
  /** 弹窗副标题（说明这个渠道要填什么） */
  lede: string;
  /** 官方配置指南链接 */
  guide_url: string;
}

/** 一条工作区级接入记录（永不包含明文凭据） */
export interface ImConnection {
  id: string;
  provider: ImProviderId;
  mode: ImMode;
  label: string;
  status: ImStatus;
  app_id: string;
  has_secret: boolean;
  secret_fingerprint: string;
  qr_expires_at: number | null;
  last_error: string;
  last_connected_at: number | null;
  events_seen_at: number | null;
  enabled: boolean;
  created_by: string;
  created_at: number;
  updated_at: number;
}

/** 扫码会话：qr_content 用于渲染二维码，expires_in 用于倒计时 */
export interface ImQrSession {
  ticket: string;
  status: "pending" | "scaned" | "confirmed" | "expired" | "need_verifycode" | "error";
  qr_content: string;
  /** 已渲染好的二维码（SVG data URI）。微信给的是网页链接，不是图片 */
  qr_image: string;
  expires_in: number;
  error: string;
  conn_id?: string;
}

/** IM 账号 → 工作台账号的绑定（一人一码，不是会话绑任务） */
export interface ImIdentity {
  provider: string;
  sender_id: string;
  user_id: string;
  username: string;
  bound_at: number;
}

/** 一次性绑定码 */
export interface ImLinkCode {
  code: string;
  expires_at: number;
  ttl: number;
  instruction: string;
}

/** 会话 → 任务的绑定（已废弃：IM 改为命令终端后不再需要） */
export interface ImChatBinding {
  provider: ImProviderId;
  channel_id: string;
  initiative_id: string;
  label: string;
  chat_type: string;
  created_by: string;
}
