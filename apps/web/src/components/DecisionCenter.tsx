import { useCallback, useEffect, useState } from "react";
import { answerDecision, listDecisions, sweepDecisions } from "../api";
import { t } from "../i18n";
import { mockDecisions } from "../mock";
import type { HumanDecisionRecord, Lang } from "../types";

type Props = {
  token: string;
  lang: Lang;
  /** Only show decisions for this mission when set. */
  initiativeId?: string | null;
  /** 管理员：可看所有人的拍板记录，并支持「按人 → 任务」聚合视图。 */
  isAdmin?: boolean;
  /** 任务 id → 标题映射，用于分组头展示任务名。 */
  initiativeTitles?: Record<string, string>;
};

/** 按key分组，保持首次出现顺序；空key归入 "" 组 */
function groupBy<T>(rows: T[], key: (r: T) => string): [string, T[]][] {
  const map = new Map<string, T[]>();
  for (const r of rows) {
    const k = key(r) || "";
    const list = map.get(k);
    if (list) list.push(r);
    else map.set(k, [r]);
  }
  return [...map.entries()];
}

function countdown(deadline: string, lang: Lang): string {
  const at = Date.parse(deadline);
  if (Number.isNaN(at)) return "—";
  const seconds = Math.round((at - Date.now()) / 1000);
  if (seconds <= 0) return t(lang, "expired");
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 1) return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  return `${seconds}s`;
}

/** 紧急度色条颜色：按 SLA 剩余时间比例分档（<25% 红，<60% 琥珀，其余绿） */
function urgencyColor(d: HumanDecisionRecord): string {
  const at = Date.parse(d.deadline_at);
  const sla = d.sla_seconds || 0;
  if (Number.isNaN(at) || sla <= 0) return "var(--warn)";
  const remain = (at - Date.now()) / 1000;
  if (remain <= 0) return "var(--danger)";
  const frac = remain / sla;
  if (frac < 0.25) return "var(--danger)";
  if (frac < 0.6) return "var(--warn)";
  return "var(--signal)";
}

/**
 * Human decision centre — the escalation ladder's last resort.
 *
 * Shows what is blocking a mission, when its SLA runs out, and which fallback
 * will be applied if nobody answers.
 */
export function DecisionCenter({ token, lang, initiativeId, isAdmin = false, initiativeTitles = {} }: Props) {
  const [decisions, setDecisions] = useState<HumanDecisionRecord[]>([]);
  // 管理员视角切换：task=按任务聚合（默认），person=按人→任务聚合
  const [groupByMode, setGroupByMode] = useState<"task" | "person">("task");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const rows = await listDecisions(token, { initiativeId: initiativeId ?? undefined });
      // 接口为空时回退 mock 演示数据，便于 UI 走查
      setDecisions(rows.length > 0 ? rows : mockDecisions());
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

  const missionLabel = (id: string) => initiativeTitles[id] || id.slice(0, 10);
  // 有真实标题时才在旁边补充 mono id，避免无标题时 id 重复显示两遍
  const missionIdTag = (id: string) => (initiativeTitles[id] ? id.slice(0, 10) : "");
  // 卡片在分组内按截止时间升序，最紧急的排最前
  const byDeadline = (a: HumanDecisionRecord, b: HumanDecisionRecord) =>
    Date.parse(a.deadline_at || "") - Date.parse(b.deadline_at || "");

  function pendingCard(d: HumanDecisionRecord) {
    const color = urgencyColor(d);
    const at = Date.parse(d.deadline_at);
    const sla = d.sla_seconds || 0;
    const calm = !Number.isNaN(at) && sla > 0 && (at - Date.now()) / 1000 / sla >= 0.6;
    return (
      <section className="wr-dec-card" key={d.id}>
        {/* 紧急度色条：三段渐弱，颜色随 SLA 剩余比例变化 */}
        <div className="wr-urgency">
          <i style={{ background: color }} />
          <i style={{ background: color, opacity: 0.45 }} />
          <i style={{ background: color, opacity: 0.2 }} />
        </div>
        <div className="wr-inner">
          <div className="wr-dec-head">
            <span className="st warn">{t(lang, "awaitingHuman")}</span>
            <span className="wr-id">
              {d.initiative_id.slice(0, 10)} · {d.stage || "mission"}
            </span>
            <span className={`wr-clock${calm ? " calm" : ""}`}>{countdown(d.deadline_at, lang)}</span>
          </div>
          <p className="wr-dec-q">{d.question}</p>
          {d.context && <p className="wr-dec-ctx">{d.context}</p>}
          <div className="wr-dec-meta">
            {t(lang, "assignee")}: {d.assignee || "—"}
            {d.options.length > 0 && <> · {t(lang, "decOptions")}: {d.options.join(" / ")}</>}
            <> · fallback: {d.fallback_action}</>
          </div>
          <div className="wr-answer-row">
            <input
              value={drafts[d.decision_id] ?? ""}
              placeholder={t(lang, "answer")}
              onChange={(e) => setDrafts({ ...drafts, [d.decision_id]: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") void submit(d);
              }}
            />
            <button
              className="btn primary"
              type="button"
              disabled={busy || !(drafts[d.decision_id] ?? "").trim()}
              onClick={() => void submit(d)}
            >
              {t(lang, "answer")}
            </button>
          </div>
        </div>
      </section>
    );
  }

  function closedCard(d: HumanDecisionRecord) {
    return (
      <section className="wr-dec-card closed" key={d.id}>
        <div className="wr-urgency">
          <i
            style={{
              background:
                d.status === "answered"
                  ? "var(--signal)"
                  : d.status === "expired"
                    ? "var(--danger)"
                    : "var(--line-hot)",
            }}
          />
        </div>
        <div className="wr-inner">
          <div className="wr-dec-head">
            <span className={`st ${d.status === "answered" ? "green" : d.status === "expired" ? "red" : ""}`}>
              {d.status}
            </span>
            <span className="wr-id">
              {d.decision_id} · {d.stage || "mission"}
            </span>
          </div>
          <p className="wr-dec-ctx">
            {d.question}
            {d.answer ? ` → ${d.answer.slice(0, 60)}` : ""}
            {d.fallback_reason ? ` · ${d.fallback_reason}` : ""}
          </p>
        </div>
      </section>
    );
  }

  // ── 按任务聚合：一个任务下可能有多项待拍板决策 ──
  function renderTaskGroups() {
    const pendingGroups = groupBy(pending, (d) => d.initiative_id);
    const closedGroups = groupBy(closed, (d) => d.initiative_id);
    return (
      <>
        {pendingGroups.length > 0 && (
          <div className="panel-label" style={{ marginBottom: 10 }}>
            {t(lang, "decPending")}
          </div>
        )}
        {pendingGroups.map(([taskId, rows]) => (
          <section className="wr-group" key={`p-${taskId}`}>
            <div className="wr-group-head">
              <b>{missionLabel(taskId)}</b>
              <span className="wr-group-id">{missionIdTag(taskId)}</span>
              <span className="wr-group-count">
                <span className="hot">{rows.length}</span> {t(lang, "decCountPending")}
              </span>
            </div>
            {rows.sort(byDeadline).map(pendingCard)}
          </section>
        ))}
        {pendingGroups.length === 0 && <p className="tiny">{t(lang, "noDecisions")}</p>}

        {closedGroups.length > 0 && (
          <div className="panel-label" style={{ margin: "20px 0 10px" }}>
            {t(lang, "history")}
          </div>
        )}
        {closedGroups.map(([taskId, rows]) => (
          <section className="wr-group" key={`c-${taskId}`}>
            <div className="wr-group-head">
              <b>{missionLabel(taskId)}</b>
              <span className="wr-group-id">{missionIdTag(taskId)}</span>
              <span className="wr-group-count">
                {rows.length} {t(lang, "decCountClosed")}
              </span>
            </div>
            {rows.map(closedCard)}
          </section>
        ))}
      </>
    );
  }

  // ── 按人聚合（管理员）：先按决策人，再按任务；含待决与已决 ──
  function renderPersonGroups() {
    const all = [...pending, ...closed];
    const personGroups = groupBy(all, (d) => d.assignee || t(lang, "decUnassigned"));
    // 待决多的决策人排前面
    personGroups.sort((a, b) => {
      const ap = a[1].filter((d) => d.status === "pending").length;
      const bp = b[1].filter((d) => d.status === "pending").length;
      return bp - ap;
    });
    if (personGroups.length === 0) return <p className="tiny">{t(lang, "noDecisions")}</p>;
    return personGroups.map(([person, rows]) => {
      const p = rows.filter((d) => d.status === "pending").length;
      const c = rows.length - p;
      return (
        <section className="wr-group" key={person}>
          <div className="wr-group-head">
            <span className="wr-person-name">{person}</span>
            <span className="wr-group-count">
              {p > 0 && (
                <>
                  <span className="hot">{p}</span> {t(lang, "decCountPending")} ·{" "}
                </>
              )}
              {c} {t(lang, "decCountClosed")}
            </span>
          </div>
          {groupBy(rows, (d) => d.initiative_id).map(([taskId, taskRows]) => (
            <div key={`t-${taskId}`}>
              <div className="wr-subgroup-head">
                {missionLabel(taskId)}
                {missionIdTag(taskId) && ` · ${missionIdTag(taskId)}`}
              </div>
              {taskRows.sort(byDeadline).map((d) => (d.status === "pending" ? pendingCard(d) : closedCard(d)))}
            </div>
          ))}
        </section>
      );
    });
  }

  return (
    <>
      <div className="wr-page-head">
        <h2>{t(lang, "decisions")}</h2>
        <span className="wr-hint">{t(lang, "decisionsHint")}</span>
        <div className="wr-head-right">
          {isAdmin && !initiativeId && (
            <div className="wr-seg">
              <button
                type="button"
                className={groupByMode === "task" ? "on" : ""}
                onClick={() => setGroupByMode("task")}
              >
                {t(lang, "decGrpByTask")}
              </button>
              <button
                type="button"
                className={groupByMode === "person" ? "on" : ""}
                onClick={() => setGroupByMode("person")}
              >
                {t(lang, "decGrpByPerson")}
              </button>
            </div>
          )}
          <button className="btn ghost" type="button" disabled={busy} onClick={() => void refresh()}>
            {t(lang, "refresh")}
          </button>
          <button className="btn ghost" type="button" disabled={busy} onClick={() => void sweep()}>
            {t(lang, "sweepExpired")}
          </button>
        </div>
      </div>

      {error && <p style={{ color: "var(--danger, #d33)" }}>{error}</p>}
      {notice && <p className="tiny">{notice}</p>}

      {groupByMode === "person" && isAdmin && !initiativeId ? renderPersonGroups() : renderTaskGroups()}
    </>
  );
}
