import { useCallback, useEffect, useRef, useState } from "react";
import { getSubRoomTranscript, listSubRooms } from "../api";
import { t } from "../i18n";
import { MOCK_SUBROOMS, MOCK_TRANSCRIPTS } from "../mock";
import { RoleAvatar } from "./RoleAvatar";
import type { Lang, SubRoomRecord } from "../types";

type Props = {
  token: string;
  lang: Lang;
  /** When set, only this mission's sub-rooms are listed. */
  initiativeId?: string | null;
  /** Bumped by the mission page on every live sub-room SSE event. */
  liveSignal?: number;
  /** Reports the sub-room count so the mission dock badge stays in sync. */
  onCount?: (count: number) => void;
  /** Optional role key → display title lookup for the member chips. */
  roleTitles?: Record<string, string>;
  /** "dock" trims the standalone-page copy for the in-mission side drawer. */
  variant?: "page" | "dock";
};

type Turn = { role?: string | null; text: string; at?: string };

type Member = { key: string; label: string; tag?: "initiator" | "lead" };

/**
 * Group members in display order — opener, lead, then the rest, de-duplicated.
 * The roster is derived from the record only: there is deliberately no way to
 * add or remove members from the UI.
 */
function membersOf(sub: SubRoomRecord, titles: Record<string, string> = {}): Member[] {
  const seen = new Set<string>();
  const out: Member[] = [];
  const push = (raw: string, tag?: Member["tag"]) => {
    const key = (raw || "").trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push({ key, label: titles[key] || key, tag });
  };
  push(sub.initiator_role, "initiator");
  push(sub.lead_role, "lead");
  for (const p of sub.participants || []) push(p);
  return out;
}

/**
 * Sub-room ("小群") panel.
 *
 * Read-only by design: the transcript lives here and never flows back into the
 * big room — only the conclusion became stage-scoped memory. Members are shown
 * as-is and cannot be edited. When `liveSignal` is provided the panel re-pulls
 * records + the open transcript on every tick, so a running discussion is
 * followed in real time; the transcript endpoint stays the source of truth.
 */
export function SubRoomPanel({
  token,
  lang,
  initiativeId,
  liveSignal = 0,
  onCount,
  roleTitles,
  variant = "page",
}: Props) {
  const [subrooms, setSubrooms] = useState<SubRoomRecord[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<Turn[]>([]);
  const [fullscreen, setFullscreen] = useState(false);
  const [error, setError] = useState("");

  // Kept in refs so live refreshes never rebuild the callbacks below (which
  // would restart the SSE-driven effect on every parent render).
  const openIdRef = useRef<string | null>(null);
  const onCountRef = useRef<Props["onCount"]>(onCount);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    onCountRef.current = onCount;
  }, [onCount]);

  const loadTranscript = useCallback(
    async (subroomId: string) => {
      // mock 小群没有后端会话，直接取本地演示对白
      if (MOCK_TRANSCRIPTS[subroomId]) {
        setTranscript(MOCK_TRANSCRIPTS[subroomId]);
        return;
      }
      try {
        setTranscript(await getSubRoomTranscript(token, subroomId));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [token],
  );

  const refresh = useCallback(async (): Promise<SubRoomRecord[]> => {
    try {
      let rows = await listSubRooms(token, initiativeId ?? undefined);
      // 接口为空时回退 mock 演示数据；badge 仍按真实数据计数，避免误导
      if (rows.length === 0) {
        setSubrooms(MOCK_SUBROOMS);
        setError("");
        return MOCK_SUBROOMS;
      }
      setSubrooms(rows);
      onCountRef.current?.(rows.length);
      setError("");
      return rows;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return [];
    }
  }, [token, initiativeId]);

  // Initial load + one refresh per live event from the mission SSE stream.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const rows = await refresh();
      if (cancelled) return;
      const id = openIdRef.current;
      if (id && rows.some((r) => r.id === id)) await loadTranscript(id);
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh, loadTranscript, liveSignal]);

  // Esc closes the fullscreen conversation, mirroring the big-room chat panel.
  useEffect(() => {
    if (!fullscreen) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [fullscreen]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [transcript.length, openId, fullscreen]);

  async function toggle(sub: SubRoomRecord) {
    if (openId === sub.id) {
      openIdRef.current = null;
      setOpenId(null);
      setTranscript([]);
      setFullscreen(false);
      return;
    }
    openIdRef.current = sub.id;
    setOpenId(sub.id);
    setTranscript([]);
    await loadTranscript(sub.id);
  }

  const active = subrooms.find((s) => s.id === openId) || null;

  function renderTurns() {
    if (transcript.length === 0) return <p className="tiny">—</p>;
    const display = (role?: string | null) => roleTitles?.[role ?? ""] || role || "?";
    return (
      <>
        {transcript.map((turn, idx) => (
          <div className="wr-msg" key={`${openId}-${idx}`}>
            <RoleAvatar role={turn.role ?? ""} title={display(turn.role)} size={34} />
            <span>
              <span className="who">
                {display(turn.role)}
                {turn.role && turn.role === active?.initiator_role && (
                  <small>{t(lang, "subroomInitiator")}</small>
                )}
                {turn.role && turn.role === active?.lead_role && (
                  <small>{t(lang, "subroomLead")}</small>
                )}
              </span>
              <p>{turn.text}</p>
            </span>
          </div>
        ))}
        <div ref={endRef} />
      </>
    );
  }

  function renderMembers(sub: SubRoomRecord) {
    const members = membersOf(sub, roleTitles);
    if (members.length === 0) return null;
    return (
      <div className="subroom-members">
        <span className="panel-label tiny">
          {t(lang, "subroomMembers")}
          <em className="subroom-locked">🔒 {t(lang, "subroomMembersLocked")}</em>
        </span>
        <div className="subroom-member-list">
          {members.map((m) => (
            <span className={`subroom-member${m.tag ? ` ${m.tag}` : ""}`} key={m.key}>
              {m.label}
              {m.tag ? (
                <em>{t(lang, m.tag === "initiator" ? "subroomInitiator" : "subroomLead")}</em>
              ) : null}
            </span>
          ))}
        </div>
      </div>
    );
  }

  // V2 作战室：page 变体改为「窄轨道 + 对话时间线」布局；dock 变体沿用下方卡片列表
  if (variant === "page") {
    // token 预算进度：estimate/max 封顶 100%，防预估超限把条撑爆
    const tokenPct = active
      ? Math.min(100, Math.round(((active.token_estimate || 0) / Math.max(1, active.max_tokens || 1)) * 100))
      : 0;
    return (
      <>
        <div className="wr-page-head">
          <h2>{t(lang, "subrooms")}</h2>
          <span className="wr-hint">{t(lang, "subroomsHint")}</span>
          <div className="wr-head-right">
            {liveSignal > 0 && <span className="st green">● {t(lang, "subroomLive")}</span>}
          </div>
        </div>
        {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
        {subrooms.length === 0 && <p className="tiny">{t(lang, "noSubrooms")}</p>}
        <div className="wr-subroom-layout">
          <aside>
            {subrooms.map((sub) => (
              <button
                className={`wr-rail-item${openId === sub.id ? " active" : ""}`}
                key={sub.id}
                type="button"
                onClick={() => void toggle(sub)}
              >
                <span className="wr-t">
                  <span className="wr-tt">
                    {sub.initiator_role} → {sub.parent_stage}
                  </span>
                  <em className="subroom-status">{sub.status}</em>
                </span>
                <span className="wr-g">{sub.goal}</span>
              </button>
            ))}
          </aside>
          <section className="wr-timeline">
            {active ? (
              <>
                <div className="wr-tl-head">
                  <b>{active.initiator_role} → {active.parent_stage}</b>
                  <span style={{ display: "flex", gap: 8 }}>
                    <button
                      className="btn ghost tiny"
                      type="button"
                      onClick={() => void toggle(active)}
                    >
                      {t(lang, "close")}
                    </button>
                    <button
                      className="btn ghost tiny subroom-fs-btn"
                      type="button"
                      onClick={() => setFullscreen(true)}
                      title={t(lang, "subroomFullscreen")}
                    >
                      ⛶ {t(lang, "subroomFullscreen")}
                    </button>
                  </span>
                </div>
                <span className="mono" style={{ fontSize: "0.7rem", color: "var(--muted)" }}>
                  {active.initiative_id.slice(0, 10)}
                </span>
                <div className="wr-budget">
                  <span className="wr-lab">
                    tokens ≈ {active.token_estimate}/{active.max_tokens}
                  </span>
                  <span className="wr-track">
                    <span className="wr-fill" style={{ width: `${tokenPct}%` }} />
                  </span>
                  <span className="wr-lab">
                    rounds ≤ {active.max_rounds} · {active.timeout_seconds}s
                  </span>
                </div>
                {renderMembers(active)}
                <div>{renderTurns()}</div>
                {active.conclusion && (
                  <div className="wr-conclusion-bar">
                    <span className="k">{t(lang, "conclusion")}</span>
                    <p>{active.conclusion}</p>
                  </div>
                )}
                {active.reason && (
                  <p className="tiny" style={{ marginTop: 8, opacity: 0.75 }}>
                    {active.reason}
                  </p>
                )}
              </>
            ) : (
              <p className="tiny">{t(lang, "wrPickSubroom")}</p>
            )}
          </section>
        </div>
        {fullscreen && active && (
          <section className="subroom-fullscreen">
            <header className="subroom-fs-head">
              <div>
                <span className="panel-label">
                  {active.initiator_role} → {active.parent_stage}
                </span>
                <span className="tiny subroom-fs-goal">{active.goal}</span>
              </div>
              <button
                className="btn ghost tiny"
                type="button"
                onClick={() => setFullscreen(false)}
                title={`${t(lang, "subroomExitFullscreen")} (Esc)`}
              >
                ⤡ {t(lang, "subroomExitFullscreen")}
              </button>
            </header>
            <div className="subroom-fs-side">{renderMembers(active)}</div>
            <div className="subroom-chat subroom-fs-body">{renderTurns()}</div>
          </section>
        )}
      </>
    );
  }

  // dock 变体：任务内抽屉形态，沿用紧凑卡片列表
  return (
    <>
      {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
      {subrooms.length === 0 && <p className="tiny">{t(lang, "noSubrooms")}</p>}

      {subrooms.map((sub) => (
        <section className="panel subroom-card" key={sub.id}>
          <div className="row-between">
            <span className="panel-label">
              {sub.initiator_role} → {sub.parent_stage}
            </span>
            <span className="tiny">
              <em className="subroom-status">{sub.status}</em>
              {liveSignal > 0 && <em className="subroom-live">● {t(lang, "subroomLive")}</em>}
            </span>
          </div>

          <div className="tiny" style={{ marginTop: 6 }}>
            {sub.goal}
          </div>
          {renderMembers(sub)}
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

          <div className="subroom-actions">
            <button className="btn ghost tiny" type="button" onClick={() => void toggle(sub)}>
              {openId === sub.id ? t(lang, "close") : t(lang, "transcript")}
            </button>
            {openId === sub.id && (
              <button
                className="btn ghost tiny subroom-fs-btn"
                type="button"
                onClick={() => setFullscreen(true)}
                title={t(lang, "subroomFullscreen")}
              >
                ⛶ {t(lang, "subroomFullscreen")}
              </button>
            )}
          </div>

          {openId === sub.id && !fullscreen && (
            <div className="subroom-chat">{renderTurns()}</div>
          )}
        </section>
      ))}

      {fullscreen && active && (
        <section className="subroom-fullscreen">
          <header className="subroom-fs-head">
            <div>
              <span className="panel-label">
                {active.initiator_role} → {active.parent_stage}
              </span>
              <span className="tiny subroom-fs-goal">{active.goal}</span>
            </div>
            <button
              className="btn ghost tiny"
              type="button"
              onClick={() => setFullscreen(false)}
              title={`${t(lang, "subroomExitFullscreen")} (Esc)`}
            >
              ⤡ {t(lang, "subroomExitFullscreen")}
            </button>
          </header>
          <div className="subroom-fs-side">{renderMembers(active)}</div>
          <div className="subroom-chat subroom-fs-body">{renderTurns()}</div>
        </section>
      )}
    </>
  );
}
