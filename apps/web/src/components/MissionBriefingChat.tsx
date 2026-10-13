import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "../api";
import { playSfx } from "../sfx";
import type { RepoConfig } from "../types";
import { ChatComposer, type ComposerAttachment, type ComposerPayload } from "./ChatComposer";

type RepoRow = { url: string; branch: string };
type ReposBySlot = Record<string, RepoRow[]>;

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
  initiative_id?: string | null;
};

type Props = {
  token: string;
  lang: "zh" | "en";
  busy: boolean;
  setBusy: (v: boolean) => void;
  onCreated: (initiativeId: string) => void;
  onError: (msg: string) => void;
  labels: {
    title: string;
    placeholder: string;
    hint: string;
    send: string;
    confirm: string;
    reject: string;
    restart: string;
    waterfall: string;
    attach?: string;
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
}: Props) {
  const [session, setSession] = useState<BriefingSession | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [bootError, setBootError] = useState("");
  const [repos, setRepos] = useState<ReposBySlot>({});
  const bottomRef = useRef<HTMLDivElement>(null);
  const locked = busy || sending;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api<BriefingSession>("/api/briefing/sessions", {
          token,
          method: "POST",
          body: "{}",
        });
        if (!cancelled) {
          setSession(s);
          setBootError("");
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
      playSfx("whoosh");
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
      playSfx("warn");
    } finally {
      setBusy(false);
    }
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
        .map((r) => ({ url: r.url.trim(), branch: r.branch.trim() }))
        .filter((r) => r.url);
      if (clean.length) out[slot] = clean;
    }
    return out;
  }

  async function confirmPlan() {
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

    try {
      const s = await api<BriefingSession>(`/api/briefing/sessions/${session.id}/messages`, {
        token,
        method: "POST",
        body: JSON.stringify({
          text,
          attachments: attachments.map(toApiAttachment),
        }),
      });
      setSession(s);
      playSfx(s.status === "awaiting_confirm" || s.status === "done" ? "ok" : "toggle");
      if (s.status === "done" && s.initiative_id) {
        onCreated(s.initiative_id);
      }
    } catch (err) {
      // Roll back optimistic bubble + restore draft text
      setSession((prev) =>
        prev
          ? {
              ...prev,
              messages: (prev.messages || []).filter((m) => m !== optimistic && m.at !== optimistic.at),
              events: (prev.events || []).filter((e) => !String(e.id).startsWith("local_")),
            }
          : prev,
      );
      setInput(text);
      onError(err instanceof Error ? err.message : String(err));
      playSfx("warn");
    } finally {
      setSending(false);
      setBusy(false);
    }
  }

  function onSubmit(e?: FormEvent | KeyboardEvent, payload?: ComposerPayload) {
    e?.preventDefault?.();
    void send(payload?.text ?? input, payload?.attachments ?? []);
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
            <div className="briefing-repos">
              <div className="panel-label">
                {lang === "zh" ? "开发仓库（可选，多个用「+」添加）" : "Dev repos (optional, add multiple)"}
              </div>
              <p className="muted briefing-repos-hint">
                {lang === "zh"
                  ? "为对应开发 Agent 绑定 git 仓库与分支；确认后其工作区会先 clone 这些仓库，读代码再迭代。"
                  : "Bind git repos & branches per dev agent; on confirm the agent clones and reads them before iterating."}
              </p>
              {repoSlots().map((slot) => (
                <div key={slot} className="briefing-repo-slot">
                  <span className="loadout-label">{slot}</span>
                  {(repos[slot] || []).map((row, i) => (
                    <div key={i} className="briefing-repo-row">
                      <input
                        value={row.url}
                        placeholder="https://github.com/org/repo.git"
                        onChange={(e) => updateRepoRow(slot, i, { url: e.target.value })}
                      />
                      <input
                        className="briefing-repo-branch"
                        value={row.branch}
                        placeholder={lang === "zh" ? "分支（可选）" : "branch (optional)"}
                        onChange={(e) => updateRepoRow(slot, i, { branch: e.target.value })}
                      />
                      <button
                        type="button"
                        className="btn linkish"
                        onClick={() => removeRepoRow(slot, i)}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  <button type="button" className="toolkit-chip" onClick={() => addRepoRow(slot)}>
                    + {lang === "zh" ? "仓库" : "repo"}
                  </button>
                </div>
              ))}
            </div>
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
        disabled={locked || !session || session?.status === "done"}
        mentions={[]}
        commands={[
          { id: "confirm", label: labels.confirm, hint: "confirm plan" },
          { id: "reject", label: labels.reject, hint: "reject plan" },
        ]}
        attachmentsEnabled
        attachLabel={labels.attach || (lang === "zh" ? "附件" : "Attach")}
      />
    </div>
  );
}
