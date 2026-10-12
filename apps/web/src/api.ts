import { ApiError } from "./types";
import type {
  DecisionAnswerResult,
  ForgeDraft,
  HarnessesResponse,
  HumanDecisionRecord,
  Initiative,
  RoleSpec,
  SubRoomRecord,
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
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: authHeaders(token, options.headers),
  });
  if (res.status === 401) {
    throw new ApiError("unauthorized", 401);
  }
  if (!res.ok) throw new ApiError((await res.text()) || res.statusText, res.status);
  return res.json() as Promise<T>;
}

// --- role registry --------------------------------------------------------

export async function listRoles(token: string): Promise<RoleSpec[]> {
  return api<RoleSpec[]>("/api/roles", { token });
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
  patch: Partial<Pick<ForgeDraft, "title" | "summary" | "persona" | "role_key" | "tags">>,
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
