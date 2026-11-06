import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";
import { playSfx } from "../sfx";
import type {
  ExternalMaterial,
  GitCredentialInfo,
  RepoConfig,
  UserRepoInfo,
  WorkspaceRepoInfo,
} from "../types";
import { ChatComposer, type ComposerAttachment, type ComposerPayload } from "./ChatComposer";

type RepoRow = {
  url: string;
  branch: string;
  name?: string;
  /** workspace=管理员配的工作区仓库；custom=本人登记的私有仓库 */
  source?: "workspace" | "custom";
  /** 新任务只带 repo_id，执行期回查仓库当前绑定的凭据 */
  repo_id?: string | null;
  /** 仅历史/手填 URL 兼容用 */
  credential_id?: string | null;
};
type ReposBySlot = Record<string, RepoRow[]>;

/** localStorage key holding the resumable briefing session id. */
const SESSION_KEY = "briefing_session_id";

export type BriefingAttachment = {
  name: string;
  mime: string;
  size?: number;
  kind: string;
  data_url?: string;
  text_excerpt?: string;
};

export type BriefingEvent = {
  id: string;
  kind: string;
  text: string;
  data?: Record<string, unknown>;
  at?: string;
};

export type BriefingMessage = {
  role: string;
  text: string;
  at?: string;
  attachments?: BriefingAttachment[];
};

export type BriefingSession = {
  id: string;
  status: string;
  title?: string;
  messages: BriefingMessage[];
  events: BriefingEvent[];
  draft?: Record<string, unknown>;
  materials?: ExternalMaterial[];
  initiative_id?: string | null;
};

type Props = {
  token: string;
  lang: "zh" | "en";
  busy: boolean;
  setBusy: (v: boolean) => void;
  onCreated: (initiativeId: string) => void;
  onError: (msg: string) => void;
  /** Snapshot of the intake page's external-materials panel (all join the discussion). */
  materials: ExternalMaterial[];
  /** Chat-composer attachments land in the external-materials panel instead of the message. */
  onImportFiles: (atts: ComposerAttachment[]) => void;
  /** Materials restored from a resumed briefing session (backfill the panel). */
  onMaterialsLoaded?: (mats: ExternalMaterial[]) => void;
  /** Session id == pre-generated mission id; parent uses it to upload originals. */
  onSessionReady?: (missionId: string) => void;
  /** Clears the parent's external-materials panel — wired to the "新会话" button. */
  onReset?: () => void;
  labels: {
    title: string;
    placeholder: string;
    hint: string;
    send: string;
    confirm: string;
    reject: string;
    restart: string;
    waterfall: string;
  };
};

function toApiAttachment(a: ComposerAttachment): BriefingAttachment {
  return {
    name: a.name,
    mime: a.mime,
    size: a.size,
    kind: a.kind,
    data_url: a.dataUrl,
    text_excerpt: a.textExcerpt,
  };
}

export function MissionBriefingChat({
  token,
  lang,
  busy,
  setBusy,
  onCreated,
  onError,
  labels,
  materials,
  onImportFiles,
  onMaterialsLoaded,
  onSessionReady,
  onReset,
}: Props) {
  const [session, setSession] = useState<BriefingSession | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [bootError, setBootError] = useState("");
  const [repos, setRepos] = useState<ReposBySlot>({});
  // 仓库选择器数据源：工作区仓库（管理员配）+ 我的私有仓库（本人登记）
  const [workspaceRepos, setWorkspaceRepos] = useState<WorkspaceRepoInfo[]>([]);
  const [myRepos, setMyRepos] = useState<UserRepoInfo[]>([]);
  const [wsCreds, setWsCreds] = useState<GitCredentialInfo[]>([]);
  const [myCreds, setMyCreds] = useState<GitCredentialInfo[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const locked = busy || sending;

  // 仓库清单加载失败的原因（含失败接口名），为空表示清单可用
  const [repoSourceError, setRepoSourceError] = useState("");
  const [repoLoadTick, setRepoLoadTick] = useState(0);
  // 工作区仓库全员可读、私有仓库仅本人可读；四个数据源彼此独立降级，不阻断会话
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    void (async () => {
      // 原先用 Promise.all + 静默 catch：任一接口失败会让整个下拉框没有选项
      const [ws, mine, wc, mc] = await Promise.allSettled([
        api<WorkspaceRepoInfo[]>("/api/git/workspace-repos", { token }),
        api<UserRepoInfo[]>("/api/me/git/repos", { token }),
        api<GitCredentialInfo[]>("/api/git/workspace-credentials", { token }),
        api<GitCredentialInfo[]>("/api/me/git-credentials", { token }),
      ]);
      if (cancelled) return;
      setWorkspaceRepos(ws.status === "fulfilled" ? ws.value : []);
      setMyRepos(mine.status === "fulfilled" ? mine.value : []);
      setWsCreds(wc.status === "fulfilled" ? wc.value : []);
      setMyCreds(mc.status === "fulfilled" ? mc.value : []);
      // 只展示 message，避免把响应体里的敏感上下文带进界面
      const reasonOf = (err: unknown) => (err instanceof Error ? err.message : String(err));
      const reasons: string[] = [];
      if (ws.status === "rejected") reasons.push(`/api/git/workspace-repos: ${reasonOf(ws.reason)}`);
      if (mine.status === "rejected") reasons.push(`/api/me/git/repos: ${reasonOf(mine.reason)}`);
      // 凭据元信息只影响下拉标签，缺失时不再禁用选项，因此不记入错误提示
      setRepoSourceError(reasons.join("；"));
    })();
    return () => {
      cancelled = true;
    };
  }, [token, repoLoadTick]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Resume the last session (messages + materials) when possible.
      const saved = typeof localStorage !== "undefined" ? localStorage.getItem(SESSION_KEY) : null;
      if (saved) {
        try {
          const s = await api<BriefingSession>(`/api/briefing/sessions/${saved}`, { token });
          if (!cancelled) {
            setSession(s);
            setBootError("");
            onSessionReady?.(s.id);
            onMaterialsLoaded?.(s.materials || []);
            return;
          }
        } catch {
          // Stale id — fall through and start a fresh session.
        }
      }
      try {
        const s = await api<BriefingSession>("/api/briefing/sessions", {
          token,
          method: "POST",
          body: "{}",
        });
        if (!cancelled) {
          setSession(s);
          setBootError("");
          onSessionReady?.(s.id);
          try {
            localStorage.setItem(SESSION_KEY, s.id);
          } catch {
            /* storage unavailable */
          }
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        if (!cancelled) {
          setBootError(msg);
          onError(msg);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- start once per token
  }, [token]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [session?.messages, session?.events, sending]);

  async function restart() {
    setBusy(true);
    setSending(false);
    try {
      const s = await api<BriefingSession>("/api/briefing/sessions", {
        token,
        method: "POST",
        body: "{}",
      });
      setSession(s);
      setInput("");
      setRepos({});
      setBootError("");
      // 新会话：同时清空父组件的外部资料（附件）面板，等价于从零重建任务
      onReset?.();
      try {
        localStorage.setItem(SESSION_KEY, s.id);
      } catch {
        /* storage unavailable */
      }
      playSfx("whoosh");
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
      playSfx("warn");
    } finally {
      setBusy(false);
    }
  }

  /** 凭据元信息仅用于下拉项的补充说明；查不到时返回空串，不阻止用户选择该仓库。 */
  function credentialLabel(id?: string | null): string {
    if (!id) return "";
    return [...wsCreds, ...myCreds].find((c) => c.id === id)?.label || "";
  }

  /** 取当前行绑定的仓库条目，用于在卡片上展示来源与凭据状态。 */
  function selectedRepoInfo(row: RepoRow): { credential_id?: string | null } | null {
    if (!row.repo_id) return null;
    const list: { id: string; credential_id?: string | null }[] =
      row.source === "workspace" ? workspaceRepos : myRepos;
    return list.find((r) => r.id === row.repo_id) || null;
  }

  function repoSlots(): string[] {
    const draft = session?.draft || {};
    const roleAgents = (draft.role_agents as Record<string, string>) || {};
    const slots = Object.keys(roleAgents);
    if (!slots.length && draft.role_repos) {
      return Object.keys(draft.role_repos as Record<string, unknown>);
    }
    return slots;
  }

  function setSlotRepos(slot: string, rows: RepoRow[]) {
    setRepos((prev) => ({ ...prev, [slot]: rows }));
  }

  function addRepoRow(slot: string) {
    setSlotRepos(slot, [...(repos[slot] || []), { url: "", branch: "" }]);
  }

  function updateRepoRow(slot: string, i: number, patch: Partial<RepoRow>) {
    setSlotRepos(
      slot,
      (repos[slot] || []).map((r, j) => (j === i ? { ...r, ...patch } : r)),
    );
  }

  function removeRepoRow(slot: string, i: number) {
    setSlotRepos(slot, (repos[slot] || []).filter((_, j) => j !== i));
  }

  function repoPayload(): Record<string, RepoConfig[]> {
    const out: Record<string, RepoConfig[]> = {};
    for (const [slot, rows] of Object.entries(repos)) {
      const clean = rows
        .map((r) => ({
          url: r.url.trim(),
          branch: r.branch.trim(),
          name: r.name,
          source: r.source || "custom",
          repo_id: r.repo_id || null,
          // 新任务只传 repo_id；手填 URL 的历史兼容路径才带 credential_id
          credential_id:
            !r.repo_id && r.source === "workspace" ? r.credential_id || null : null,
        }))
        .filter((r) => r.url);
      if (clean.length) out[slot] = clean;
    }
    return out;
  }

  async function confirmPlan() {
    // 分支是任务级必填：任一已选仓库为空就不允许确认
    const missing = Object.values(repos)
      .flat()
      .filter((r) => r.url.trim() && !r.branch.trim());
    if (missing.length) {
      onError(lang === "zh" ? "请填写仓库分支" : "Please fill in a branch for every repo");
      playSfx("warn");
      return;
    }
    const payload = repoPayload();
    if (session && Object.keys(payload).length) {
      try {
        const s = await api<BriefingSession>(`/api/briefing/sessions/${session.id}/repos`, {
          token,
          method: "PATCH",
          body: JSON.stringify({ role_repos: payload }),
        });
        setSession(s);
      } catch (err) {
        onError(err instanceof Error ? err.message : String(err));
        playSfx("warn");
        return;
      }
    }
    await send("确认");
  }

  async function send(text: string, attachments: ComposerAttachment[] = []) {
    if (sending) return;
    if (!text.trim() && !attachments.length) return;
    if (!session) {
      onError(lang === "zh" ? "规划会话未就绪，请点「新会话」重试" : "Briefing session not ready — try New chat");
      playSfx("warn");
      return;
    }

    const displayText = text.trim() || (attachments.length ? `[附件 ×${attachments.length}]` : "");
    const optimistic: BriefingMessage = {
      role: "user",
      text: displayText,
      at: new Date().toISOString(),
      attachments: attachments.map(toApiAttachment),
    };

    setSending(true);
    setBusy(true);
    setInput("");
    setSession((prev) =>
      prev
        ? {
            ...prev,
            messages: [...(prev.messages || []), optimistic],
            events: [
              ...(prev.events || []),
              {
                id: `local_${Date.now()}`,
                kind: "waterfall",
                text: lang === "zh" ? "⏳ 规划官思考中…" : "⏳ Planner thinking…",
              },
            ],
          }
        : prev,
    );
    playSfx("click");

    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const s = await api<BriefingSession>(`/api/briefing/sessions/${session.id}/messages`, {
        token,
        method: "POST",
        body: JSON.stringify({
          text,
          attachments: attachments.map(toApiAttachment),
          materials,
        }),
        signal: ac.signal,
      });
      setSession(s);
      playSfx(s.status === "awaiting_confirm" || s.status === "done" ? "ok" : "toggle");
      if (s.status === "done" && s.initiative_id) {
        onCreated(s.initiative_id);
      }
    } catch (err) {
      const aborted = ac.signal.aborted;
      setSession((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          // On abort keep the user bubble (the backend persisted the turn);
          // on real errors roll it back and restore the draft text.
          messages: aborted
            ? prev.messages || []
            : (prev.messages || []).filter((m) => m !== optimistic && m.at !== optimistic.at),
          events: (prev.events || []).filter((e) => !String(e.id).startsWith("local_")),
        };
      });
      if (aborted) {
        playSfx("toggle");
      } else {
        setInput(text);
        onError(err instanceof Error ? err.message : String(err));
        playSfx("warn");
      }
    } finally {
      abortRef.current = null;
      setSending(false);
      setBusy(false);
    }
  }

  /** Abort the in-flight planning request (the backend cancels on disconnect). */
  function stop() {
    abortRef.current?.abort();
  }

  function onSubmit(e?: FormEvent | KeyboardEvent, payload?: ComposerPayload) {
    e?.preventDefault?.();
    // Composer attachments are routed into the external-materials panel; the
    // message itself only carries text (materials travel via props snapshot).
    if (payload?.attachments?.length) onImportFiles(payload.attachments);
    void send(payload?.text ?? input);
  }

  // 「增强提示词」：把输入与最近几轮会话交给 LLM 做规划向优化，失败时 toast 并返回 null（不覆盖）
  async function enhanceInput(text: string): Promise<string | null> {
    const ctx = (session?.messages || [])
      .slice(-6)
      .map((m) => `${m.role}: ${m.text.slice(0, 200)}`)
      .join("\n")
      .slice(0, 4000);
    try {
      const d = await api<{ text: string }>("/api/prompts/enhance", {
        token,
        method: "POST",
        body: JSON.stringify({ scene: "briefing", text, context: ctx }),
      });
      return d.text;
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
      return null;
    }
  }

  const waterfall = (session?.events || []).filter((e) =>
    ["waterfall", "tool", "confirm", "done", "plan"].includes(e.kind),
  );
  const awaiting = session?.status === "awaiting_confirm";

  return (
    <div className="briefing">
      <div className="briefing-head">
        <div className="panel-label">{labels.title}</div>
        <button type="button" className="btn linkish" onClick={() => void restart()} disabled={locked}>
          {labels.restart}
        </button>
      </div>

      {bootError ? (
        <div className="alert bar">
          {lang === "zh" ? "规划会话启动失败：" : "Failed to start briefing: "}
          {bootError}
        </div>
      ) : null}

      <div className="briefing-waterfall" aria-label={labels.waterfall}>
        {waterfall.map((ev) => (
          <div key={ev.id} className={`wf-step kind-${ev.kind}${sending && String(ev.id).startsWith("local_") ? " live" : ""}`}>
            <span className="wf-dot" />
            <div className="wf-body">
              <strong>{ev.text}</strong>
              {ev.kind === "tool" && ev.data?.phase === "end" && ev.data?.ok === false && (
                <span className="muted"> · fail</span>
              )}
            </div>
          </div>
        ))}
        {!waterfall.length && (
          <p className="muted">{lang === "zh" ? "规划步骤将在此展开" : "Planning steps appear here"}</p>
        )}
      </div>

      <div className="briefing-chat">
        {(session?.messages || []).map((m, i) => (
          <div key={`${m.at}-${i}`} className={`briefing-bubble ${m.role}`}>
            {m.attachments && m.attachments.length > 0 ? (
              <ul className="briefing-msg-atts">
                {m.attachments.map((a, j) => (
                  <li key={`${a.name}-${j}`} className={`briefing-msg-att kind-${a.kind}`}>
                    {a.kind === "image" && a.data_url ? (
                      <a href={a.data_url} target="_blank" rel="noreferrer">
                        <img src={a.data_url} alt={a.name} />
                      </a>
                    ) : (
                      <span className="briefing-msg-file" title={a.name}>
                        {a.name}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : null}
            {m.text ? <pre>{m.text}</pre> : null}
          </div>
        ))}
        {sending ? (
          <div className="briefing-bubble assistant planning" aria-live="polite">
            <pre>{lang === "zh" ? "规划官检索历史与知识库中…" : "Planner is searching history & knowledge…"}</pre>
            <span className="briefing-typing" aria-hidden>
              <i />
              <i />
              <i />
            </span>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </div>

      {awaiting && !sending && (
        <>
          {repoSlots().length > 0 && (
            <section className="briefing-repos" aria-label={lang === "zh" ? "开发仓库" : "Dev repos"}>
              <div className="briefing-repos-head">
                <div>
                  <div className="panel-label">
                    {lang === "zh" ? "开发仓库 · 可选" : "Dev repos · optional"}
                  </div>
                  <p className="muted briefing-repos-hint">
                    {lang === "zh"
                      ? "按角色绑定仓库与分支，确认后 Agent 会先 clone 再开发。"
                      : "Bind repos & branches per role; agents clone them before coding."}
                  </p>
                </div>
                <span className="briefing-repos-count">
                  {repoSlots().length} {lang === "zh" ? "角色" : "roles"}
                </span>
              </div>

              {repoSourceError ? (
                <p className="briefing-repos-empty warn">
                  {lang === "zh" ? "仓库清单加载失败：" : "Repo list failed: "}
                  {repoSourceError}
                  {lang === "zh" ? "，可直接手填 URL。" : " — enter a URL manually."}
                  <button
                    type="button"
                    className="briefing-repos-retry"
                    onClick={() => setRepoLoadTick((n) => n + 1)}
                  >
                    {lang === "zh" ? "重试" : "Retry"}
                  </button>
                </p>
              ) : workspaceRepos.length === 0 && myRepos.length === 0 ? (
                <p className="briefing-repos-empty">
                  {lang === "zh"
                    ? "暂无可选仓库：可在「设置 → Git」登记，或直接手填 URL。"
                    : "No registered repos: add one in Settings → Git, or enter a URL manually."}
                </p>
              ) : null}

              <div className="briefing-repo-slots">
                {repoSlots().map((slot) => {
                  const rows = repos[slot] || [];
                  return (
                    <div key={slot} className={`briefing-repo-slot${rows.length ? " has-repos" : ""}`}>
                      <div className="briefing-repo-slot-head">
                        <span className="briefing-repo-slot-name">{slot}</span>
                        <span className="briefing-repo-slot-meta">
                          {rows.length
                            ? rows
                                .map((r) => r.name || r.url || (lang === "zh" ? "未选择" : "unset"))
                                .join("、")
                            : lang === "zh"
                              ? "未绑定"
                              : "no repo"}
                        </span>
                        <button
                          type="button"
                          className="briefing-repo-add"
                          onClick={() => addRepoRow(slot)}
                        >
                          + {lang === "zh" ? "添加仓库" : "Add repo"}
                        </button>
                      </div>

                      {rows.length > 0 && (
                        <div className="briefing-repo-rows">
                          {rows.map((row, i) => {
                            // 绑定了注册表条目时回显该项，避免下拉框永远停在「选择仓库…」
                            const selected = row.repo_id
                              ? `${row.source || "custom"}:${row.repo_id}`
                              : "";
                            const bound = selectedRepoInfo(row);
                            return (
                              <div className="briefing-repo-row" key={i}>
                                <select
                                  className="briefing-repo-picker"
                                  value={selected}
                                  aria-label={`${slot} ${lang === "zh" ? "仓库" : "repo"}`}
                                  onChange={(e) => {
                                    const [source, id] = (e.target.value || "").split(":");
                                    if (!source || !id) {
                                      // 切回手填 URL：清掉注册表引用，否则后端仍按 repo_id 回查旧仓库
                                      updateRepoRow(slot, i, {
                                        source: "custom",
                                        repo_id: null,
                                        name: undefined,
                                        credential_id: null,
                                      });
                                      return;
                                    }
                                    const list = source === "workspace" ? workspaceRepos : myRepos;
                                    const hit = list.find((r) => r.id === id);
                                    if (!hit) return;
                                    // 同一角色内同一仓库只允许一行，避免重复 clone
                                    if (rows.some((r, j) => j !== i && r.repo_id === hit.id)) return;
                                    updateRepoRow(slot, i, {
                                      url: hit.url,
                                      name: hit.name,
                                      branch: row.branch || hit.default_branch || "",
                                      source: source === "workspace" ? "workspace" : "custom",
                                      repo_id: hit.id,
                                      credential_id: hit.credential_id ?? null,
                                    });
                                  }}
                                >
                                  <option value="">
                                    {lang === "zh" ? "手填 URL / 选择仓库…" : "Pick a repo or enter URL…"}
                                  </option>
                                  {/* 清单接口暂时不可用时仍显示已绑定项，不出现空白下拉 */}
                                  {selected && !bound && (
                                    <option value={selected}>
                                      {row.name || row.url || (lang === "zh" ? "已绑定仓库" : "bound repo")}
                                    </option>
                                  )}
                                  {workspaceRepos.length > 0 && (
                                    <optgroup label={lang === "zh" ? "工作区仓库" : "Workspace repos"}>
                                      {workspaceRepos.map((r) => (
                                        <option key={r.id} value={`workspace:${r.id}`}>
                                          {r.name}
                                          {credentialLabel(r.credential_id)
                                            ? ` · ${credentialLabel(r.credential_id)}`
                                            : r.credential_id
                                              ? ""
                                              : lang === "zh"
                                                ? " · 未绑定凭据"
                                                : " · unbound"}
                                        </option>
                                      ))}
                                    </optgroup>
                                  )}
                                  {myRepos.length > 0 && (
                                    <optgroup label={lang === "zh" ? "我的私有仓库" : "My private repos"}>
                                      {myRepos.map((r) => (
                                        <option key={r.id} value={`custom:${r.id}`}>
                                          {r.name}
                                          {r.credential_id
                                            ? ""
                                            : lang === "zh"
                                              ? " · 未绑定凭据"
                                              : " · unbound"}
                                        </option>
                                      ))}
                                    </optgroup>
                                  )}
                                </select>
                                <button
                                  type="button"
                                  className="briefing-repo-remove"
                                  title={lang === "zh" ? "移除" : "Remove"}
                                  aria-label={lang === "zh" ? "移除仓库" : "Remove repo"}
                                  onClick={() => removeRepoRow(slot, i)}
                                >
                                  ×
                                </button>
                                <div className="briefing-repo-fields">
                                  <input
                                    value={row.url}
                                    placeholder="https://github.com/org/repo.git"
                                    aria-label={lang === "zh" ? "仓库 URL" : "Repo URL"}
                                    onChange={(e) =>
                                      updateRepoRow(slot, i, {
                                        url: e.target.value,
                                        source: "custom",
                                        repo_id: null,
                                        name: undefined,
                                        credential_id: null,
                                      })
                                    }
                                  />
                                  <input
                                    className="briefing-repo-branch"
                                    value={row.branch}
                                    placeholder={lang === "zh" ? "分支 *" : "branch *"}
                                    aria-label={lang === "zh" ? "分支" : "Branch"}
                                    onChange={(e) => updateRepoRow(slot, i, { branch: e.target.value })}
                                  />
                                </div>
                                {bound && (
                                  <div className="briefing-repo-source">
                                    <span
                                      className={`briefing-repo-badge ${row.source === "workspace" ? "ws" : "mine"}`}
                                    >
                                      {row.source === "workspace"
                                        ? lang === "zh"
                                          ? "工作区"
                                          : "Workspace"
                                        : lang === "zh"
                                          ? "我的仓库"
                                          : "Mine"}
                                    </span>
                                    {bound.credential_id
                                      ? credentialLabel(bound.credential_id) ||
                                        (lang === "zh" ? "凭据已绑定" : "credential bound")
                                      : lang === "zh"
                                        ? "未绑定凭据，执行时可能失败"
                                        : "no credential bound"}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          )}
          <div className="briefing-actions">
            <button type="button" className="btn primary" disabled={locked} onClick={() => void confirmPlan()}>
              {labels.confirm}
            </button>
            <button type="button" className="btn" disabled={locked} onClick={() => void send("驳回")}>
              {labels.reject}
            </button>
          </div>
        </>
      )}

      <ChatComposer
        value={input}
        onChange={setInput}
        onSubmit={onSubmit}
        placeholder={labels.placeholder}
        hint={labels.hint}
        sendLabel={sending ? (lang === "zh" ? "规划中…" : "Planning…") : labels.send}
        stopping={sending}
        stopLabel={lang === "zh" ? "停止" : "Stop"}
        onStop={stop}
        disabled={locked || !session || session?.status === "done"}
        mentions={[]}
        onEnhance={enhanceInput}
        enhanceLabel={lang === "zh" ? "增强提示词" : "Enhance prompt"}
        enhanceUndoLabel={lang === "zh" ? "撤销优化" : "Undo"}
        enhanceLoadingLabel={lang === "zh" ? "增强中…" : "Enhancing…"}
        commands={[
          { id: "confirm", label: labels.confirm, hint: "confirm plan" },
          { id: "reject", label: labels.reject, hint: "reject plan" },
        ]}
      />
    </div>
  );
}
