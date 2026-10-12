export type Lang = "zh" | "en";

export type View = "home" | "tasks" | "squad" | "knowledge" | "memory" | "agents" | "toolkit" | "admin";

export type UserRole = "admin" | "member";

export type InitiativeStatus =
  | "draft"
  | "running"
  | "waiting_hitl"
  | "stopped"
  | "failed"
  | "done";

export type AgentSlot =
  | "product"
  | "design"
  | "eng"
  | "eng_ios"
  | "eng_android"
  | "eng_web"
  | "eng_backend"
  | "eng_agent"
  | "qa"
  | "deploy";

export type CodingExecutor = "mock" | "claude_code" | "cursor_cli";
export type PipelineTrack = "auto" | "express" | "standard" | "default";
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

export type KnowledgeCategory = "playbook" | "runbook" | "lore" | "reference";

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

export interface PublicUser {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
}

export interface LoginResponse {
  token: string;
  user: PublicUser;
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

export interface HitlRequest {
  stage: string;
  prompt: string;
  allowed_actions?: string[];
  artifacts?: ArtifactRef[];
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

export interface Initiative {
  id: string;
  title: string;
  brief: string;
  template?: string;
  status: InitiativeStatus;
  current_stage: string;
  thread_id?: string;
  room_id?: string;
  roles: RoleAgent[];
  participants: RoomParticipant[];
  artifacts: ArtifactRef[];
  timeline?: TimelineEvent[];
  pending_hitl?: HitlRequest | null;
  human_instruction?: string | null;
  created_at?: string;
  updated_at?: string;
  webhook_url?: string | null;
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
  tags: string[];
  source?: string;
  author_id?: string;
  author_name?: string;
  created_at?: string;
  updated_at?: string;
}

export interface PublishedAgent {
  id: string;
  slug: string;
  title: string;
  slot: AgentSlot;
  summary: string;
  persona: string;
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

export class ApiError extends Error {
  code?: number;
  constructor(message: string, code?: number) {
    super(message);
    this.code = code;
  }
}
