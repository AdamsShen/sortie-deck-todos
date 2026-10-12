import { useCallback, useEffect, useState } from "react";
import {
  approveForgeDraft,
  listForgeDrafts,
  proposeForgeDraft,
  rejectForgeDraft,
  updateForgeDraft,
} from "../api";
import { t } from "../i18n";
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
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <input
            value={capability}
            placeholder="育儿顾问 / stocks research …"
            onChange={(e) => setCapability(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void propose();
            }}
            style={{ flex: 1 }}
          />
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
            <textarea
              value={form.persona}
              rows={4}
              style={{ width: "100%", marginTop: 6 }}
              onChange={(e) =>
                setEditing({ ...editing, [draft.id]: { ...form, persona: e.target.value } })
              }
            />

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
