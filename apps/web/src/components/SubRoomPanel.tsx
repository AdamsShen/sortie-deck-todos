import { useCallback, useEffect, useState } from "react";
import { getSubRoomTranscript, listSubRooms } from "../api";
import { t } from "../i18n";
import type { Lang, SubRoomRecord } from "../types";

type Props = {
  token: string;
  lang: Lang;
  /** When set, only this mission's sub-rooms are listed. */
  initiativeId?: string | null;
};

type Turn = { role?: string | null; text: string; at?: string };

/**
 * Sub-room side panel.
 *
 * Read-only by design: the transcript lives here and never flows back into the
 * big room — only the conclusion became stage-scoped memory.
 */
export function SubRoomPanel({ token, lang, initiativeId }: Props) {
  const [subrooms, setSubrooms] = useState<SubRoomRecord[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<Turn[]>([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setSubrooms(await listSubRooms(token, initiativeId ?? undefined));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [token, initiativeId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function toggle(sub: SubRoomRecord) {
    if (openId === sub.id) {
      setOpenId(null);
      setTranscript([]);
      return;
    }
    setOpenId(sub.id);
    setTranscript([]);
    try {
      setTranscript(await getSubRoomTranscript(token, sub.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <div className="panel-label">{t(lang, "subrooms")}</div>
      <p className="tiny" style={{ opacity: 0.75, margin: "4px 0 12px" }}>
        {t(lang, "subroomsHint")}
      </p>

      {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
      {subrooms.length === 0 && <p className="tiny">{t(lang, "noSubrooms")}</p>}

      {subrooms.map((sub) => (
        <section className="panel" key={sub.id} style={{ marginBottom: 10 }}>
          <div className="row-between">
            <span className="panel-label">
              {sub.initiator_role} → {sub.parent_stage}
            </span>
            <span className="tiny">{sub.status}</span>
          </div>

          <div className="tiny" style={{ marginTop: 6 }}>
            {sub.goal}
          </div>
          <div className="tiny" style={{ opacity: 0.75, marginTop: 6 }}>
            {t(lang, "participants")}: {sub.participants.join(", ") || "—"} · depth {sub.depth}
          </div>
          <div className="tiny" style={{ opacity: 0.75 }}>
            tokens ≈ {sub.token_estimate}/{sub.max_tokens} · rounds ≤ {sub.max_rounds} ·{" "}
            {sub.timeout_seconds}s
          </div>

          {sub.conclusion && (
            <div style={{ marginTop: 8 }}>
              <div className="panel-label tiny">{t(lang, "conclusion")}</div>
              <pre className="tiny" style={{ whiteSpace: "pre-wrap", margin: "4px 0 0" }}>
                {sub.conclusion}
              </pre>
            </div>
          )}

          {sub.reason && (
            <div className="tiny" style={{ marginTop: 6, opacity: 0.75 }}>
              {sub.reason}
            </div>
          )}

          <button className="btn ghost" type="button" style={{ marginTop: 8 }} onClick={() => void toggle(sub)}>
            {openId === sub.id ? t(lang, "close") : t(lang, "transcript")}
          </button>

          {openId === sub.id && (
            <div style={{ marginTop: 8 }}>
              {transcript.length === 0 && <span className="tiny">—</span>}
              {transcript.map((turn, idx) => (
                <div key={`${sub.id}-${idx}`} className="tiny" style={{ marginTop: 6 }}>
                  <strong>{turn.role ?? "?"}</strong>: {turn.text}
                </div>
              ))}
            </div>
          )}
        </section>
      ))}
    </>
  );
}
