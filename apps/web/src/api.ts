import { ApiError } from "./types";
import type {
  ApiKeyCreated,
  ApiKeyRecord,
  DecisionAnswerResult,
  ExternalMaterial,
  ForgeDraft,
  HarnessesResponse,
  HumanDecisionRecord,
  Initiative,
  InsightMissionDetail,
  InsightMissionSummary,
  InsightTotals,
  KnowledgeBaseInfo,
  KnowledgeBaseScope,
  KnowledgeDoc,
  MissionShare,
  PersonaSpecShape,
  PublicUser,
  RoleSpec,
  SubRoomRecord,
  TierGroup,
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

/** 解析 Content-Disposition 中的文件名（优先 RFC 5987 的 filename*，兼容普通 filename） */
function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      /* 非法编码时退回普通 filename */
    }
  }
  return /filename="?([^";]+)"?/i.exec(header)?.[1] ?? null;
}

/**
 * 导出当前账号数据：返回二进制 Blob 与后端建议的文件名。
 *
 * 不走 `api<T>()`——它固定按 JSON 解析响应体；这里需要完整字节与
 * Content-Disposition 头，下载内容才能与接口响应字节一致（PRD C-2）。
 */
export async function exportUserData(
  token: string,
  fallbackName = "sortie-data.json",
): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch(`${API}/api/export/user`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (res.status === 401) {
    throw new ApiError("unauthorized", 401);
  }
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw || res.statusText;
    try {
      const parsed = JSON.parse(raw) as { detail?: unknown };
      if (typeof parsed?.detail === "string") msg = parsed.detail;
    } catch {
      /* 非 JSON 响应保留原文 */
    }
    throw new ApiError(msg, res.status);
  }
  const blob = await res.blob();
  return {
    blob,
    filename: filenameFromDisposition(res.headers.get("Content-Disposition")) || fallbackName,
  };
}

/**
 * 下载任务产物（二进制，如 code-archive 代码归档 zip）。
 *
 * 不走 `api<T>()`——它固定按 JSON 解析响应体；这里返回完整 Blob 与
 * Content-Disposition 建议的文件名，交给浏览器触发另存。
 */
export async function downloadMissionArtifact(
  token: string,
  initiativeId: string,
  path: string,
): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch(
    `${API}/api/initiatives/${encodeURIComponent(initiativeId)}/artifacts/${path}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    throw new ApiError("unauthorized", 401);
  }
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw || res.statusText;
    try {
      const parsed = JSON.parse(raw) as { detail?: unknown };
      if (typeof parsed?.detail === "string") msg = parsed.detail;
    } catch {
      /* 非 JSON 响应保留原文 */
    }
    throw new ApiError(msg, res.status);
  }
  const blob = await res.blob();
  return {
    blob,
    filename:
      filenameFromDisposition(res.headers.get("Content-Disposition")) ||
      path.split("/").pop() ||
      "artifact.zip",
  };
}

// --- api keys -------------------------------------------------------------

/** 列出当前账号的 API Key（不含明文与哈希） */
export async function listApiKeys(token: string): Promise<ApiKeyRecord[]> {
  return api<ApiKeyRecord[]>("/api/keys", { token });
}

/** 新建 Key：响应里的 plaintext 只出现这一次；有效期按日历月（1/3/6/12） */
export async function createApiKey(
  token: string,
  body: { name: string; expiresInMonths?: number },
): Promise<ApiKeyCreated> {
  return api<ApiKeyCreated>("/api/keys", {
    token,
    method: "POST",
    body: JSON.stringify({
      name: body.name,
      expires_in_months: body.expiresInMonths ?? 1,
    }),
  });
}

/** 删除 Key：后端物理删除，使用它的客户端立即失效 */
export async function deleteApiKey(token: string, keyId: string): Promise<void> {
  await api(`/api/keys/${encodeURIComponent(keyId)}`, { token, method: "DELETE" });
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

/** 建任务页的档位数据：开发/办公两套各三档（阶段链路 + 预估时长）。 */
export async function listPipelineTiers(
  token: string,
  taskKind?: "delivery" | "knowledge",
): Promise<{ task_kinds: TierGroup[] }> {
  const qs = taskKind ? `?task_kind=${encodeURIComponent(taskKind)}` : "";
  return api<{ task_kinds: TierGroup[] }>(`/api/pipelines/tiers${qs}`, { token });
}

// --- 耗时分析（严格只看自己创建的任务） ------------------------------------

/**
 * 耗时分析任务列表，默认取后端硬上限 300 条。
 * TODO: 前端搜索只覆盖这 300 条，任务数增长后需在列表接口增加 q 参数做服务端过滤。
 */
export async function listInsightMissions(
  token: string,
  limit = 300,
): Promise<{ missions: InsightMissionSummary[]; totals: InsightTotals }> {
  return api<{ missions: InsightMissionSummary[]; totals: InsightTotals }>(
    `/api/insights/missions?limit=${limit}`,
    { token },
  );
}

export async function getInsightMission(
  token: string,
  initiativeId: string,
): Promise<InsightMissionDetail> {
  return api<InsightMissionDetail>(
    `/api/insights/missions/${encodeURIComponent(initiativeId)}`,
    { token },
  );
}

export async function compareInsightMissions(
  token: string,
  ids: string[],
): Promise<{ missions: InsightMissionDetail[] }> {
  const qs = ids.map((id) => encodeURIComponent(id)).join(",");
  return api<{ missions: InsightMissionDetail[] }>(`/api/insights/compare?ids=${qs}`, {
    token,
  });
}

/** 保存建任务页的档位选择；确认建队时据此裁剪航线。 */
export async function updateBriefingPrefs(
  token: string,
  sessionId: string,
  body: { task_kind?: string; tier?: string },
): Promise<unknown> {
  return api(`/api/briefing/sessions/${encodeURIComponent(sessionId)}/prefs`, {
    token,
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

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

/** 上传任务输入原件：落 missions/<id>/input/files/ 并抽文本（manifest 登记） */
export async function uploadMissionInput(
  token: string,
  missionId: string,
  file: File,
  opts?: { kind?: string; pinned?: boolean },
): Promise<ExternalMaterial> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("kind", opts?.kind || "other");
  fd.append("pinned", String(Boolean(opts?.pinned)));
  return api<ExternalMaterial>(`/api/missions/${encodeURIComponent(missionId)}/input`, {
    token,
    method: "POST",
    body: fd,
  });
}

export async function setMissionInputPinned(
  token: string,
  missionId: string,
  materialId: string,
  pinned: boolean,
): Promise<ExternalMaterial> {
  return api<ExternalMaterial>(
    `/api/missions/${encodeURIComponent(missionId)}/input/${encodeURIComponent(materialId)}`,
    { token, method: "PATCH", body: JSON.stringify({ pinned }) },
  );
}

export async function deleteMissionInput(
  token: string,
  missionId: string,
  materialId: string,
): Promise<void> {
  await api(`/api/missions/${encodeURIComponent(missionId)}/input/${encodeURIComponent(materialId)}`, {
    token,
    method: "DELETE",
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
