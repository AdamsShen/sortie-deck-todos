import { ApiError } from "./types";
import type {
  DecisionAnswerResult,
  ExternalMaterial,
  ForgeDraft,
  HarnessesResponse,
  HumanDecisionRecord,
  Initiative,
  KnowledgeBaseInfo,
  KnowledgeBaseScope,
  KnowledgeDoc,
  MissionShare,
  PersonaSpecShape,
  PublicUser,
  RoleSpec,
  SubRoomRecord,
  UserStatus,
  UserRole,
} from "./types";

const API = "";

function authHeaders(token?: string, extra: HeadersInit = {}): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  };
}

type ApiOptions = RequestInit & { token?: string };

export async function api<T = unknown>(path: string, { token, ...options }: ApiOptions = {}): Promise<T> {
  // FormData（multipart 上传）时浏览器需自动设置含 boundary 的 Content-Type，跳过默认 JSON 头
  const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: isForm
      ? {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...((options.headers as Record<string, string>) || {}),
        }
      : authHeaders(token, options.headers),
  });
  if (res.status === 401) {
    throw new ApiError("unauthorized", 401);
  }
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw || res.statusText;
    // FastAPI 错误体形如 {"detail":"..."}，解析为友好文案；校验错误 detail 为数组
    try {
      const parsed = JSON.parse(raw) as { detail?: unknown };
      if (typeof parsed?.detail === "string") {
        msg = parsed.detail;
      } else if (Array.isArray(parsed?.detail) && typeof parsed.detail[0]?.msg === "string") {
        msg = parsed.detail[0].msg;
      }
    } catch {
      /* 非 JSON 响应保留原文 */
    }
    throw new ApiError(msg, res.status);
  }
  return res.json() as Promise<T>;
}

// --- auth / account -------------------------------------------------------

/** 自助注册：创建待审核账号，管理员通过后才能登录；用户名重复抛 ApiError(400) */
export async function registerUser(body: {
  username: string;
  password: string;
  display_name: string;
}): Promise<{ status: UserStatus; user: PublicUser }> {
  return api("/api/auth/register", { method: "POST", body: JSON.stringify(body) });
}

/** 管理员修改用户角色 / 审核状态（只传需要变更的字段） */
export async function adminUpdateUser(
  token: string,
  userId: string,
  patch: { role?: UserRole; status?: UserStatus },
): Promise<PublicUser> {
  return api<PublicUser>(`/api/admin/users/${encodeURIComponent(userId)}`, {
    token,
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** 账号注销：永久删除自己账号并清除登录 token（名下任务保留） */
export async function deleteOwnAccount(token: string): Promise<void> {
  await api("/api/auth/account", { token, method: "DELETE" });
}

// --- mission shares -------------------------------------------------------

/** 按账号名 / 显示名 / 用户 id 搜索账号（分享面板选人用） */
export async function searchUsers(token: string, q: string): Promise<PublicUser[]> {
  return api<PublicUser[]>(`/api/users/search?q=${encodeURIComponent(q)}`, { token });
}

export async function listMissionShares(token: string, initiativeId: string): Promise<MissionShare[]> {
  return api<MissionShare[]>(`/api/initiatives/${encodeURIComponent(initiativeId)}/shares`, { token });
}

/** 共享任务给指定账号用户（仅创建者或 admin；返回更新后的任务） */
export async function addMissionShare(
  token: string,
  initiativeId: string,
  userId: string,
): Promise<Initiative> {
  return api<Initiative>(`/api/initiatives/${encodeURIComponent(initiativeId)}/shares`, {
    token,
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

/** 取消共享：创建者/admin 可移除任意成员，被共享用户可移除自己（退出共享） */
export async function removeMissionShare(
  token: string,
  initiativeId: string,
  userId: string,
): Promise<Initiative> {
  return api<Initiative>(
    `/api/initiatives/${encodeURIComponent(initiativeId)}/shares/${encodeURIComponent(userId)}`,
    { token, method: "DELETE" },
  );
}

// --- role registry --------------------------------------------------------

export async function listRoles(token: string): Promise<RoleSpec[]> {
  return api<RoleSpec[]>("/api/roles", { token });
}

// --- mission intake -------------------------------------------------------

export async function updateInitiativeMaterials(
  token: string,
  initiativeId: string,
  materials: ExternalMaterial[],
): Promise<Initiative> {
  return api<Initiative>(`/api/initiatives/${encodeURIComponent(initiativeId)}/materials`, {
    token,
    method: "PATCH",
    body: JSON.stringify({ materials }),
  });
}

// --- agent harnesses --------------------------------------------------------

export async function listHarnesses(token: string, refresh = false): Promise<HarnessesResponse> {
  const query = refresh ? "?refresh=true" : "";
  return api<HarnessesResponse>(`/api/harnesses${query}`, { token });
}

export async function updateInitiativeHarness(
  token: string,
  initiativeId: string,
  harness: string,
): Promise<Initiative> {
  return api<Initiative>(`/api/initiatives/${encodeURIComponent(initiativeId)}/harness`, {
    token,
    method: "PATCH",
    body: JSON.stringify({ harness }),
  });
}

export async function createRole(
  token: string,
  body: { role_key: string; title?: string; summary?: string; tags?: string[] },
): Promise<RoleSpec> {
  return api<RoleSpec>("/api/roles", { token, method: "POST", body: JSON.stringify(body) });
}

export async function deleteRole(token: string, roleKey: string): Promise<void> {
  await api(`/api/roles/${encodeURIComponent(roleKey)}`, { token, method: "DELETE" });
}

// --- agent forge ----------------------------------------------------------

export async function listForgeDrafts(token: string, status?: string): Promise<ForgeDraft[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return api<ForgeDraft[]>(`/api/forge/drafts${query}`, { token });
}

export async function proposeForgeDraft(
  token: string,
  body: { capability: string; title?: string; role_key?: string; summary?: string; tags?: string[] },
): Promise<ForgeDraft> {
  return api<ForgeDraft>("/api/forge/drafts", {
    token,
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateForgeDraft(
  token: string,
  draftId: string,
  patch: Partial<Pick<ForgeDraft, "title" | "summary" | "persona" | "role_key" | "tags">> & {
    persona_spec?: PersonaSpecShape | null;
  },
): Promise<ForgeDraft> {
  return api<ForgeDraft>(`/api/forge/drafts/${encodeURIComponent(draftId)}`, {
    token,
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export async function approveForgeDraft(
  token: string,
  draftId: string,
  body: { publish_agent?: boolean; coding?: boolean; toolkit_ids?: string[] } = {},
): Promise<{ draft: ForgeDraft; role: RoleSpec | null; agent: unknown }> {
  return api(`/api/forge/drafts/${encodeURIComponent(draftId)}/approve`, {
    token,
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function rejectForgeDraft(token: string, draftId: string, note = ""): Promise<ForgeDraft> {
  return api<ForgeDraft>(
    `/api/forge/drafts/${encodeURIComponent(draftId)}/reject?note=${encodeURIComponent(note)}`,
    { token, method: "POST" },
  );
}

// --- sub-rooms ------------------------------------------------------------

export async function listSubRooms(token: string, initiativeId?: string): Promise<SubRoomRecord[]> {
  const query = initiativeId ? `?initiative_id=${encodeURIComponent(initiativeId)}` : "";
  return api<SubRoomRecord[]>(`/api/subrooms${query}`, { token });
}

export async function getSubRoomTranscript(
  token: string,
  subroomId: string,
): Promise<{ role?: string | null; text: string; at?: string }[]> {
  return api(`/api/subrooms/${encodeURIComponent(subroomId)}/transcript`, { token });
}

// --- human decisions ------------------------------------------------------

export async function listDecisions(
  token: string,
  filters: { initiativeId?: string; status?: string } = {},
): Promise<HumanDecisionRecord[]> {
  const params = new URLSearchParams();
  if (filters.initiativeId) params.set("initiative_id", filters.initiativeId);
  if (filters.status) params.set("status", filters.status);
  const query = params.toString() ? `?${params.toString()}` : "";
  return api<HumanDecisionRecord[]>(`/api/decisions${query}`, { token });
}

export async function answerDecision(
  token: string,
  decisionId: string,
  body: { answer: string; by?: string; channel?: string },
): Promise<DecisionAnswerResult> {
  return api<DecisionAnswerResult>(`/api/decisions/${encodeURIComponent(decisionId)}/answer`, {
    token,
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function sweepDecisions(token: string): Promise<{ expired: HumanDecisionRecord[] }> {
  return api<{ expired: HumanDecisionRecord[] }>("/api/decisions/sweep", { token, method: "POST" });
}

// --- knowledge bases ---------------------------------------------------------

export async function listKnowledgeBases(token: string): Promise<KnowledgeBaseInfo[]> {
  return api<KnowledgeBaseInfo[]>("/api/knowledge/bases", { token });
}

export async function createKnowledgeBase(
  token: string,
  body: { name: string; description: string; scope: KnowledgeBaseScope; task_id?: string },
): Promise<KnowledgeBaseInfo> {
  return api<KnowledgeBaseInfo>("/api/knowledge/bases", {
    token,
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteKnowledgeBase(token: string, baseId: string): Promise<void> {
  await api(`/api/knowledge/bases/${encodeURIComponent(baseId)}`, { token, method: "DELETE" });
}

/** multipart 文件导入知识库（api() 对 FormData 自动跳过 JSON Content-Type） */
export async function importKnowledgeFile(
  token: string,
  baseId: string,
  file: File,
): Promise<KnowledgeDoc> {
  const fd = new FormData();
  fd.append("file", file);
  return api<KnowledgeDoc>(`/api/knowledge/bases/${encodeURIComponent(baseId)}/import`, {
    token,
    method: "POST",
    body: fd,
  });
}

/** 轮询刷新库内文档的 WeKnora 解析状态（返回更新后的文档列表） */
export async function refreshKnowledgeParseStatus(
  token: string,
  baseId: string,
): Promise<KnowledgeDoc[]> {
  return api<KnowledgeDoc[]>(`/api/knowledge/bases/${encodeURIComponent(baseId)}/refresh-status`, {
    token,
    method: "POST",
  });
}
