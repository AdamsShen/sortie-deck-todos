import { useCallback, useEffect, useState } from "react";
import {
  approveForgeDraft,
  api,
  listForgeDrafts,
  proposeForgeDraft,
  rejectForgeDraft,
  updateForgeDraft,
} from "../api";
import { t } from "../i18n";
import { SparkleIcon } from "./ChatComposer";
import type { ForgeDraft, Lang } from "../types";

type Props = {
  token: string;
  lang: Lang;
};

/**
 * Role Forge review queue.
 *
 * Generated roles never go live by themselves — this is where a human reads the
 * self-check findings, edits the persona, and approves (optionally publishing
 * the agent into the catalog) or rejects.
 */
export function ForgeReview({ token, lang }: Props) {
  const [drafts, setDrafts] = useState<ForgeDraft[]>([]);
  const [capability, setCapability] = useState("");
  const [editing, setEditing] = useState<Record<string, { title: string; summary: string; persona: string }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // 能力输入框「增强提示词」：优化中 spinner；成功覆盖后保留原文供撤销
  const [enhancing, setEnhancing] = useState(false);
  const [enhancePrev, setEnhancePrev] = useState<string | null>(null);
  // 草稿人设 persona 增强：记录当前优化的草稿 id 与原文（撤销用）
  const [enhancingPersonaId, setEnhancingPersonaId] = useState("");
  const [personaPrev, setPersonaPrev] = useState<{ id: string; text: string } | null>(null);

  const refresh = useCallback(async () => {
    try {
      setDrafts(await listForgeDrafts(token));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function draftForm(draft: ForgeDraft) {
    return (
      editing[draft.id] ?? {
        title: draft.title,
        summary: draft.summary,
        persona: draft.persona,
      }
    );
  }

  async function propose() {
    if (!capability.trim()) return;
    setBusy(true);
    try {
      await proposeForgeDraft(token, { capability: capability.trim() });
      setCapability("");
      setNotice("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  // 能力描述「增强提示词」：LLM 把关键词扩写为清晰能力描述，覆盖输入框，可撤销
  async function enhanceCapability() {
    if (enhancing || !capability.trim()) return;
    const original = capability;
    setEnhancing(true);
    try {
      const d = await api<{ text: string }>("/api/prompts/enhance", {
        token,
        method: "POST",
        body: JSON.stringify({ scene: "forge", text: original }),
      });
      if (d.text && d.text !== original) {
        setEnhancePrev(original);
        setCapability(d.text);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setEnhancing(false);
    }
  }

  function undoEnhance() {
    if (enhancePrev === null) return;
    setCapability(enhancePrev);
    setEnhancePrev(null);
  }

  // 草稿人设「增强提示词」：LLM 把人设改写为专业系统提示，覆盖编辑框，可撤销
  async function enhancePersona(draftId: string, text: string) {
    if (enhancingPersonaId || !text.trim()) return;
    setEnhancingPersonaId(draftId);
    try {
      const d = await api<{ text: string }>("/api/prompts/enhance", {
        token,
        method: "POST",
        body: JSON.stringify({ scene: "persona", text }),
      });
      if (d.text && d.text !== text) {
        setPersonaPrev({ id: draftId, text });
        setEditing((prev) => ({
          ...prev,
          [draftId]: { ...(prev[draftId] ?? draftForm(drafts.find((x) => x.id === draftId)!)), persona: d.text },
        }));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setEnhancingPersonaId("");
    }
  }

  function undoPersona() {
    if (!personaPrev) return;
    const draft = drafts.find((x) => x.id === personaPrev.id);
    if (draft) {
      setEditing((prev) => ({
        ...prev,
        [personaPrev.id]: { ...draftForm(draft), persona: personaPrev.text },
      }));
    }
    setPersonaPrev(null);
  }

  async function save(draft: ForgeDraft) {
    setBusy(true);
    try {
      await updateForgeDraft(token, draft.id, draftForm(draft));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function approve(draft: ForgeDraft) {
    setBusy(true);
    try {
      await updateForgeDraft(token, draft.id, draftForm(draft));
      await approveForgeDraft(token, draft.id, { publish_agent: true });
      setNotice(`${draft.role_key} ✓`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function reject(draft: ForgeDraft) {
    setBusy(true);
    try {
      await rejectForgeDraft(token, draft.id, "rejected in workbench");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const pending = drafts.filter((d) => d.status === "pending-review");
  const reviewed = drafts.filter((d) => d.status !== "pending-review");

  return (
    <>
      <div className="panel-label">{t(lang, "forge")}</div>
      <p className="tiny" style={{ opacity: 0.75, margin: "4px 0 12px" }}>
        {t(lang, "forgeReviewHint")}
      </p>

      <section className="panel" style={{ marginBottom: 12 }}>
        <div className="row-between">
          <span className="panel-label">{t(lang, "newTask")}</span>
          <span className="tiny">
            {pending.length} · {t(lang, "pendingReview")}
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8, position: "relative" }}>
          <input
            value={capability}
            placeholder="育儿顾问 / stocks research …"
            onChange={(e) => {
              setCapability(e.target.value);
              if (enhancePrev !== null) setEnhancePrev(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") void propose();
            }}
            style={{ flex: 1 }}
          />
          {/* 增强中浮层：与聊天输入框一致的效果 */}
          {enhancing ? (
            <span className="composer-enhance-toast" role="status">
              <span className="mini-spinner" aria-hidden />
              {t(lang, "enhanceLoading")}
            </span>
          ) : null}
          {enhancePrev !== null && !enhancing ? (
            <button
              className="btn ghost composer-enhance-btn composer-enhance-icon"
              type="button"
              onClick={undoEnhance}
              title={t(lang, "enhanceUndo")}
              aria-label={t(lang, "enhanceUndo")}
            >
              ↺
            </button>
          ) : (
            <button
              className="btn ghost composer-enhance-btn composer-enhance-icon"
              type="button"
              disabled={enhancing || !capability.trim()}
              onClick={() => void enhanceCapability()}
              title={t(lang, "enhancePrompt")}
              aria-label={t(lang, "enhancePrompt")}
            >
              {enhancing ? <span className="mini-spinner" aria-hidden /> : <SparkleIcon />}
            </button>
          )}
          <button className="btn" type="button" disabled={busy || !capability.trim()} onClick={() => void propose()}>
            {t(lang, "agents")}
          </button>
        </div>
      </section>

      {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
      {notice && <p className="tiny">{notice}</p>}

      {pending.map((draft) => {
        const form = draftForm(draft);
        return (
          <section className="panel" key={draft.id} style={{ marginBottom: 12 }}>
            <div className="row-between">
              <span className="panel-label">{draft.role_key}</span>
              <span className="tiny">
                {t(lang, "generatedBy")}: {draft.generator}
              </span>
            </div>

            <input
              value={form.title}
              style={{ width: "100%", marginTop: 6 }}
              onChange={(e) =>
                setEditing({ ...editing, [draft.id]: { ...form, title: e.target.value } })
              }
            />
            <input
              value={form.summary}
              style={{ width: "100%", marginTop: 6 }}
              onChange={(e) =>
                setEditing({ ...editing, [draft.id]: { ...form, summary: e.target.value } })
              }
            />
            {/* 人设编辑框：右下角浮动增强按钮，增强中显示浮层 */}
            <div style={{ position: "relative", marginTop: 6 }}>
              {enhancingPersonaId === draft.id ? (
                <span className="composer-enhance-toast" role="status">
                  <span className="mini-spinner" aria-hidden />
                  {t(lang, "enhanceLoading")}
                </span>
              ) : (
                <button
                  type="button"
                  className="btn ghost composer-enhance-btn composer-enhance-icon composer-enhance-corner"
                  disabled={!form.persona.trim()}
                  onClick={() => void enhancePersona(draft.id, form.persona)}
                  title={t(lang, "enhancePrompt")}
                  aria-label={t(lang, "enhancePrompt")}
                >
                  <SparkleIcon />
                </button>
              )}
              {personaPrev?.id === draft.id && enhancingPersonaId !== draft.id && (
                <button
                  type="button"
                  className="btn ghost tiny composer-enhance-btn composer-enhance-corner"
                  style={{ bottom: "2.8rem" }}
                  onClick={undoPersona}
                  title={t(lang, "enhanceUndo")}
                  aria-label={t(lang, "enhanceUndo")}
                >
                  ↺ {t(lang, "enhanceUndo")}
                </button>
              )}
              <textarea
                value={form.persona}
                rows={4}
                style={{ width: "100%" }}
                onChange={(e) =>
                  setEditing({ ...editing, [draft.id]: { ...form, persona: e.target.value } })
                }
              />
            </div>

            <div className="tiny" style={{ marginTop: 8 }}>
              <div>
                {t(lang, "handoff")}: {(draft.contract.handoff_targets ?? []).join(", ") || "—"}
              </div>
              <div>
                {t(lang, "outputs")}: {(draft.contract.outputs ?? []).join(", ") || "—"}
              </div>
            </div>

            {draft.issues.length > 0 && (
              <div className="tiny" style={{ marginTop: 8, color: "var(--danger, #d33)" }}>
                <strong>{t(lang, "issues")}</strong>
                <ul style={{ margin: "4px 0 0 16px" }}>
                  {draft.issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              </div>
            )}

            {draft.similar.length > 0 && (
              <div className="tiny" style={{ marginTop: 8 }}>
                <strong>{t(lang, "similarRoles")}</strong>
                <ul style={{ margin: "4px 0 0 16px" }}>
                  {draft.similar.map((s) => (
                    <li key={s.role_key}>
                      {s.role_key} ({s.score})
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <button className="btn ghost" type="button" disabled={busy} onClick={() => void save(draft)}>
                {t(lang, "save")}
              </button>
              <button className="btn" type="button" disabled={busy} onClick={() => void approve(draft)}>
                {t(lang, "approveAndPublish")}
              </button>
              <button className="btn danger" type="button" disabled={busy} onClick={() => void reject(draft)}>
                {t(lang, "rejectDraft")}
              </button>
            </div>
          </section>
        );
      })}

      {reviewed.length > 0 && (
        <section className="panel">
          <div className="panel-label">{t(lang, "history")}</div>
          <ul className="tiny" style={{ margin: "6px 0 0 16px" }}>
            {reviewed.slice(0, 10).map((d) => (
              <li key={d.id}>
                {d.role_key} · {d.status}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
