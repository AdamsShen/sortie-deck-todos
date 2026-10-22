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
  // 流水线状态：03 自检发现问题时点亮，04 有待审草稿时点亮
  const hasIssues = pending.some((d) => d.issues.length > 0);

  return (
    <>
      {/* V2 作战室：输入 → 生成 → 自检 → 审核 四段流水线，卡点一眼可见 */}
      <div className="wr-pipeline">
        <div className="wr-stage done">
          <span className="wr-dot" />
          <span className="wr-no">01 INPUT</span>
          <h4>{t(lang, "wrStageInput")}</h4>
          <p>{t(lang, "wrStageInputHint")}</p>
        </div>
        <div className="wr-stage done">
          <span className="wr-dot" />
          <span className="wr-no">02 GENERATE</span>
          <h4>{t(lang, "wrStageGenerate")}</h4>
          <p>{t(lang, "wrStageGenerateHint")}</p>
        </div>
        <div className={`wr-stage ${hasIssues ? "hot" : "done"}`}>
          <span className="wr-dot" />
          <span className="wr-no">03 SELF-CHECK</span>
          <h4>{t(lang, "wrStageCheck")}</h4>
          <p>{t(lang, "wrStageCheckHint")}</p>
        </div>
        <div className={`wr-stage ${pending.length > 0 ? "hot" : ""}`}>
          <span className="wr-dot" />
          <span className="wr-no">04 REVIEW</span>
          <h4>{t(lang, "wrStageReview")}</h4>
          <p>{t(lang, "wrStageReviewHint")}</p>
        </div>
      </div>

      <p className="tiny" style={{ opacity: 0.75, margin: "0 0 12px" }}>
        {t(lang, "forgeReviewHint")}
      </p>

      <div className="wr-cap-row" style={{ position: "relative" }}>
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
        <button className="btn primary" type="button" disabled={busy || !capability.trim()} onClick={() => void propose()}>
          {t(lang, "agents")}
        </button>
      </div>

      {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
      {notice && <p className="tiny">{notice}</p>}

      {pending.map((draft) => {
        const form = draftForm(draft);
        return (
          <div className="wr-forge-body" key={draft.id} style={{ marginBottom: 16 }}>
            <section className="wr-card">
              <div className="wr-card-head">
                <span className="st warn">{t(lang, "pendingReview")}</span>
                <span className="mono" style={{ fontSize: "0.7rem", color: "var(--muted)" }}>
                  {draft.role_key} · {t(lang, "generatedBy")}: {draft.generator}
                </span>
              </div>

              <div className="wr-field-stack">
                <label className="wr-field">
                  <span>{t(lang, "agentTitle")}</span>
                  <input
                    value={form.title}
                    onChange={(e) =>
                      setEditing({ ...editing, [draft.id]: { ...form, title: e.target.value } })
                    }
                  />
                </label>
                <label className="wr-field">
                  <span>{t(lang, "agentSummary")}</span>
                  <input
                    value={form.summary}
                    onChange={(e) =>
                      setEditing({ ...editing, [draft.id]: { ...form, summary: e.target.value } })
                    }
                  />
                </label>
                {/* 人设编辑框：右下角浮动增强按钮，增强中显示浮层 */}
                <label className="wr-field" style={{ position: "relative", display: "block" }}>
                  <span>{t(lang, "agentPersona")}</span>
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
                    onChange={(e) =>
                      setEditing({ ...editing, [draft.id]: { ...form, persona: e.target.value } })
                    }
                  />
                </label>
              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                <button className="btn ghost" type="button" disabled={busy} onClick={() => void save(draft)}>
                  {t(lang, "save")}
                </button>
                <button className="btn primary" type="button" disabled={busy} onClick={() => void approve(draft)}>
                  {t(lang, "approveAndPublish")}
                </button>
                <button className="btn danger" type="button" disabled={busy} onClick={() => void reject(draft)}>
                  {t(lang, "rejectDraft")}
                </button>
              </div>
            </section>

            <aside className="wr-side-stack">
              <div className="wr-card wr-side-card">
                <h4>{t(lang, "issues")}</h4>
                {draft.issues.length === 0 ? (
                  <p className="tiny" style={{ color: "var(--signal)" }}>✓</p>
                ) : (
                  draft.issues.map((issue) => (
                    <div className="wr-issue" key={issue}>⚠ {issue}</div>
                  ))
                )}
              </div>
              <div className="wr-card wr-side-card">
                <h4>{t(lang, "similarRoles")}</h4>
                {draft.similar.length === 0 ? (
                  <p className="tiny" style={{ color: "var(--muted)" }}>—</p>
                ) : (
                  draft.similar.map((s) => (
                    <div className="wr-sim-row" key={s.role_key}>
                      <span className="mono">{s.role_key}</span>
                      <span className="wr-score">{s.score}</span>
                    </div>
                  ))
                )}
              </div>
              <div className="wr-card wr-side-card">
                <h4>{t(lang, "handoff")} / {t(lang, "outputs")}</h4>
                <div className="wr-contract-row">
                  <span className="k">{t(lang, "outputs")}</span>
                  <span>{(draft.contract.outputs ?? []).join(", ") || "—"}</span>
                </div>
                <div className="wr-contract-row">
                  <span className="k">{t(lang, "handoff")}</span>
                  <span>{(draft.contract.handoff_targets ?? []).join(", ") || "—"}</span>
                </div>
              </div>
            </aside>
          </div>
        );
      })}

      {reviewed.length > 0 && (
        <div className="wr-history">
          <span className="mono" style={{ fontSize: "0.64rem", letterSpacing: "0.14em", color: "var(--muted)" }}>
            {t(lang, "history")}
          </span>
          {reviewed.slice(0, 10).map((d) => (
            <span className="wr-h" key={d.id}>
              {d.role_key} · {d.status}
            </span>
          ))}
        </div>
      )}
    </>
  );
}
