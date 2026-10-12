import { useCallback, useEffect, useState } from "react";
import { answerDecision, listDecisions, sweepDecisions } from "../api";
import { t } from "../i18n";
import type { HumanDecisionRecord, Lang } from "../types";

type Props = {
  token: string;
  lang: Lang;
  /** Only show decisions for this mission when set. */
  initiativeId?: string | null;
};

function countdown(deadline: string, lang: Lang): string {
  const at = Date.parse(deadline);
  if (Number.isNaN(at)) return "—";
  const seconds = Math.round((at - Date.now()) / 1000);
  if (seconds <= 0) return t(lang, "expired");
  const minutes = Math.floor(seconds / 60);
  return minutes >= 1 ? `${minutes}m` : `${seconds}s`;
}

/**
 * Human decision centre — the escalation ladder's last resort.
 *
 * Shows what is blocking a mission, when its SLA runs out, and which fallback
 * will be applied if nobody answers.
 */
export function DecisionCenter({ token, lang, initiativeId }: Props) {
  const [decisions, setDecisions] = useState<HumanDecisionRecord[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setDecisions(await listDecisions(token, { initiativeId: initiativeId ?? undefined }));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [token, initiativeId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function submit(decision: HumanDecisionRecord) {
    const answer = (drafts[decision.decision_id] ?? "").trim();
    if (!answer) return;
    setBusy(true);
    try {
      const outcome = await answerDecision(token, decision.decision_id, { answer, channel: "ui" });
      if (!outcome.accepted) {
        setNotice(outcome.duplicate ? "already answered" : outcome.reason);
      } else {
        setNotice(`${decision.decision_id} ✓`);
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function sweep() {
    setBusy(true);
    try {
      const result = await sweepDecisions(token);
      setNotice(`${result.expired.length} expired`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const pending = decisions.filter((d) => d.status === "pending");
  const closed = decisions.filter((d) => d.status !== "pending");

  return (
    <>
      <div className="panel-label">{t(lang, "decisions")}</div>
      <p className="tiny" style={{ opacity: 0.75, margin: "4px 0 12px" }}>
        {t(lang, "decisionsHint")}
      </p>

      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <button className="btn ghost" type="button" disabled={busy} onClick={() => void refresh()}>
          {t(lang, "refresh")}
        </button>
        <button className="btn ghost" type="button" disabled={busy} onClick={() => void sweep()}>
          {t(lang, "sweepExpired")}
        </button>
      </div>

      {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
      {notice && <p className="tiny">{notice}</p>}
      {pending.length === 0 && <p className="tiny">{t(lang, "noDecisions")}</p>}

      {pending.map((d) => (
        <section className="panel" key={d.id} style={{ marginBottom: 10 }}>
          <div className="row-between">
            <span className="panel-label">
              {d.initiative_id.slice(0, 10)} · {d.stage || "mission"}
            </span>
            <span className="tiny">
              {t(lang, "awaitingHuman")} · {countdown(d.deadline_at, lang)}
            </span>
          </div>

          <div style={{ marginTop: 6 }}>{d.question}</div>
          {d.context && (
            <div className="tiny" style={{ opacity: 0.75, marginTop: 6 }}>
              {d.context}
            </div>
          )}
          <div className="tiny" style={{ marginTop: 6 }}>
            {t(lang, "assignee")}: {d.assignee || "—"} · fallback `{d.fallback_action}`
          </div>

          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <input
              value={drafts[d.decision_id] ?? ""}
              placeholder={t(lang, "answer")}
              onChange={(e) => setDrafts({ ...drafts, [d.decision_id]: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") void submit(d);
              }}
              style={{ flex: 1 }}
            />
            <button
              className="btn"
              type="button"
              disabled={busy || !(drafts[d.decision_id] ?? "").trim()}
              onClick={() => void submit(d)}
            >
              {t(lang, "answer")}
            </button>
          </div>

          {d.options.length > 0 && (
            <div className="tiny" style={{ marginTop: 6 }}>
              {d.options.join(" / ")}
            </div>
          )}
        </section>
      ))}

      {closed.length > 0 && (
        <section className="panel">
          <div className="panel-label">{t(lang, "history")}</div>
          <ul className="tiny" style={{ margin: "6px 0 0 16px" }}>
            {closed.slice(0, 10).map((d) => (
              <li key={d.id}>
                {d.decision_id} · {d.status}
                {d.answer ? ` · ${d.answer.slice(0, 60)}` : ""}
                {d.fallback_reason ? ` · ${d.fallback_reason}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
