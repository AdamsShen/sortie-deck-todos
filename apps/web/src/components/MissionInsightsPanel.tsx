import { useEffect, useMemo, useRef, useState } from "react";

import { compareInsightMissions, getInsightMission, listInsightMissions } from "../api";
import type {
  InsightMissionDetail,
  InsightMissionSummary,
  InsightTotals,
  Lang,
} from "../types";
import {
  formatClock,
  formatClockFull,
  formatDuration,
  formatRange,
  MissionGantt,
  MissionStageTable,
  toMs,
} from "./MissionGantt";

const MAX_COMPARE = 5;

type Props = {
  token: string;
  lang: Lang;
};

/** 设置中心唯一滚动容器（styles.css 的 .settings-body overflow:auto），下钻返回后需恢复它的位置。 */
function settingsScrollBody(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".settings-body");
}

/**
 * 复制文本。以 http://<公网 IP> 访问时不是安全上下文，navigator.clipboard 不可用，
 * 沿用 App.tsx 的既有做法降级到临时 textarea + execCommand；两者都失败返回 false。
 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    console.error("剪贴板写入失败，尝试降级", err);
  }
  try {
    const el = document.createElement("textarea");
    el.value = text;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    const ok = document.execCommand("copy");
    el.remove();
    return ok;
  } catch (err) {
    console.error("任务 ID 复制降级失败", err);
    return false;
  }
}

/**
 * 可点击复制的任务 ID，自带成功/失败反馈（按钮尾部图标切换）。
 * 列表整行可下钻，按钮必须阻止冒泡，否则复制会误打开详情。
 */
function CopyableId({
  id,
  zh,
  className,
}: {
  id: string;
  zh: boolean;
  className: string;
}) {
  const [status, setStatus] = useState<"" | "ok" | "fail">("");
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  async function copyId() {
    const ok = await copyText(id);
    if (!ok) console.error("任务 ID 复制失败", id);
    setStatus(ok ? "ok" : "fail");
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setStatus(""), 1500);
  }

  const title = status
    ? status === "ok"
      ? zh
        ? "已复制"
        : "Copied"
      : zh
        ? "复制失败，请手动选中"
        : "Copy failed — select manually"
    : zh
      ? `点击复制 ${id}`
      : `Click to copy ${id}`;

  return (
    <button
      type="button"
      className={`${className}${status ? ` copy-${status}` : ""}`}
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        void copyId();
      }}
    >
      <span>{id}</span>
    </button>
  );
}

/**
 * 单任务耗时详情：元信息 + 阶段明细表 + 甘特图。
 *
 * 执行跨度（span）与阶段耗时合计口径不同，差值即阶段间隙与人工 HITL 等待；
 * 显式展示这个差值，避免把「执行时间几点到几点」与「累计耗时」混为一谈。
 */
function DetailBody({ detail, lang }: { detail: InsightMissionDetail; lang: Lang }) {
  const zh = lang === "zh";
  const stages = detail.stages;
  const hasStages = stages.length > 0;
  const spanMs = detail.span?.duration_ms ?? 0;
  const stageMs = stages.reduce((sum, s) => sum + s.duration_ms, 0);
  const gapMs = Math.max(0, spanMs - stageMs);
  const toolCalls = stages.reduce((sum, s) => sum + s.tool_calls, 0);
  const reviews = stages.reduce((sum, s) => sum + s.review_rounds, 0);
  const startMs = toMs(detail.span?.started_at ?? null);
  const endMs = toMs(detail.span?.ended_at ?? null);
  const createdMs = toMs(detail.mission.created_at);

  const meta = [
    {
      label: zh ? "创建时间" : "Created",
      value: formatClock(createdMs),
      title: formatClockFull(createdMs),
    },
    {
      label: zh ? "执行时间" : "Executed",
      value: formatRange(startMs, endMs),
      title: `${formatClockFull(startMs)} → ${formatClockFull(endMs)}`,
    },
    { label: zh ? "执行跨度" : "Wall time", value: hasStages ? formatDuration(spanMs) : "—", title: "" },
    { label: zh ? "阶段数" : "Stages", value: hasStages ? String(stages.length) : "—", title: "" },
    {
      label: zh ? "阶段耗时合计" : "Stage sum",
      value: hasStages ? formatDuration(stageMs) : "—",
      title: "",
    },
    {
      label: zh ? "间隙与等待" : "Gaps / wait",
      value: hasStages ? formatDuration(gapMs) : "—",
      title: "",
    },
    { label: zh ? "工具步数" : "Tool calls", value: hasStages ? String(toolCalls) : "—", title: "" },
    { label: zh ? "自审轮次" : "Review rounds", value: hasStages ? String(reviews) : "—", title: "" },
  ];

  return (
    <>
      <header className="insight-detail-head">
        <div>
          <h3>{detail.mission.title}</h3>
          <CopyableId id={detail.mission.id} zh={zh} className="insight-id" />
          <span className={`insights-status s-${detail.mission.status}`}>
            {detail.mission.status}
          </span>
        </div>
        <span className="insight-hint">{zh ? "Esc 返回列表" : "Esc to go back"}</span>
      </header>

      <div className="insight-meta-grid">
        {meta.map((item) => (
          <div className="insight-meta" key={item.label}>
            <span>{item.label}</span>
            <b title={item.title || undefined}>{item.value}</b>
          </div>
        ))}
      </div>

      {!hasStages ? (
        <p className="insights-empty">
          {zh
            ? "该任务无耗时指标（功能上线前创建）。"
            : "No metrics (created before the feature shipped)."}
        </p>
      ) : (
        <>
          <p className="insight-gap-note">
            {zh
              ? "累计耗时只累加各阶段执行时间，执行跨度还包含阶段间隙与人工确认等待，两者之差即 "
              : "Stage sum counts execution only; wall time also includes gaps and human approval waits: "}
            <b>{formatDuration(gapMs)}</b>
            {zh
              ? "。明细表高亮的是最慢阶段，不是关键路径（阶段记录不含依赖边）。"
              : " The slowest stage is highlighted, not a critical path (stage records have no dependency edges)."}
          </p>
          <MissionStageTable stages={stages} lang={lang} />
          <div className="insights-chart">
            <div className="insights-chart-head">
              <b>{zh ? "阶段时间线" : "Stage timeline"}</b>
              <span>
                {zh
                  ? "相对起点对齐，条形长度 = 阶段耗时"
                  : "Aligned at start · bar = stage duration"}
              </span>
            </div>
            <MissionGantt missions={[detail]} lang={lang} />
          </div>
        </>
      )}
    </>
  );
}

/**
 * 设置 › 数据 › 耗时分析。
 *
 * 权限严格按任务创建者：后端三个接口都会校验 created_by，这里只负责展示。
 * 列表视图支持即时搜索与勾选 2-5 个任务对比；点击任务进入页面内下钻详情，
 * 两种视图的状态互不干扰，返回列表不重置搜索词与勾选、不重新请求。
 */
export function MissionInsightsPanel({ token, lang }: Props) {
  const zh = lang === "zh";
  const [missions, setMissions] = useState<InsightMissionSummary[]>([]);
  const [totals, setTotals] = useState<InsightTotals | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [details, setDetails] = useState<InsightMissionDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeDetail, setActiveDetail] = useState<InsightMissionDetail | null>(null);
  const [activeLoading, setActiveLoading] = useState(false);
  const [activeError, setActiveError] = useState("");
  const listScrollRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await listInsightMissions(token);
        if (cancelled) return;
        setMissions(data.missions || []);
        setTotals(data.totals || null);
      } catch (err) {
        console.error("加载耗时列表失败", err);
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    if (!selected.length) {
      setDetails([]);
      return () => {
        cancelled = true;
      };
    }
    setLoading(true);
    void (async () => {
      try {
        const data =
          selected.length === 1
            ? [await getInsightMission(token, selected[0])]
            : (await compareInsightMissions(token, selected)).missions;
        if (!cancelled) {
          setDetails(data);
          setError("");
        }
      } catch (err) {
        console.error("加载耗时详情失败", err);
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected, token]);

  useEffect(() => {
    if (!activeId) {
      setActiveDetail(null);
      return;
    }
    let cancelled = false;
    setActiveLoading(true);
    void (async () => {
      try {
        const data = await getInsightMission(token, activeId);
        if (!cancelled) {
          setActiveDetail(data);
          setActiveError("");
        }
      } catch (err) {
        console.error("加载任务耗时详情失败", err);
        if (!cancelled) setActiveError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setActiveLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId, token]);

  // Esc 返回列表并恢复滚动位置；列表与详情切换时父容器高度变化会丢滚动位置
  useEffect(() => {
    if (!activeId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setActiveId(null);
      const top = listScrollRef.current;
      requestAnimationFrame(() => {
        const body = settingsScrollBody();
        if (body) body.scrollTop = top;
      });
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeId]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  // TODO: 前端搜索只覆盖 listInsightMissions 的 300 条上限，任务数增长后需服务端 q 参数过滤。
  const filteredMissions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return missions;
    return missions.filter(
      (m) =>
        m.title.toLowerCase().includes(q) ||
        m.id.toLowerCase().includes(q) ||
        m.status.toLowerCase().includes(q),
    );
  }, [missions, query]);

  function toggle(id: string) {
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= MAX_COMPARE) return prev;
      return [...prev, id];
    });
  }

  function openDetail(id: string) {
    listScrollRef.current = settingsScrollBody()?.scrollTop ?? 0;
    setActiveId(id);
  }

  function backToList() {
    setActiveId(null);
    const top = listScrollRef.current;
    requestAnimationFrame(() => {
      const body = settingsScrollBody();
      if (body) body.scrollTop = top;
    });
  }

  const stats = [
    { key: "missions", label: zh ? "任务数" : "Missions", value: String(totals?.mission_count ?? missions.length) },
    { key: "duration", label: zh ? "累计耗时" : "Total time", value: formatDuration(totals?.total_duration_ms ?? 0) },
    { key: "tools", label: zh ? "工具步数" : "Tool calls", value: String(totals?.tool_calls ?? 0) },
    { key: "reviews", label: zh ? "自审轮次" : "Review rounds", value: String(totals?.review_rounds ?? 0) },
  ];

  return (
    <div className="stg insights">
      <header className="stg-head">
        <div>
          <h2>{zh ? "耗时分析" : "Duration insights"}</h2>
          <p>
            {zh
              ? "每个阶段一行：耗时、工具步数与自审轮次。只统计你自己创建的任务，管理员也不例外。"
              : "One row per stage: duration, tool calls and review rounds. Only missions you created are counted — admins included."}
          </p>
        </div>
      </header>

      <div className="insights-stats">
        {stats.map((item) => (
          <div className="insights-stat" key={item.key}>
            <span>{item.label}</span>
            <b>{item.value}</b>
          </div>
        ))}
      </div>

      {error && !activeId && <p className="stg-note danger">{error}</p>}

      {activeId ? (
        <section className="insight-detail">
          <button type="button" className="insight-back" onClick={backToList}>
            ← {zh ? "返回列表" : "Back to list"}
          </button>
          {activeLoading && <p className="insights-empty">{zh ? "加载中…" : "Loading…"}</p>}
          {activeError && <p className="stg-note danger">{activeError}</p>}
          {!activeLoading && !activeError && activeDetail && (
            <DetailBody detail={activeDetail} lang={lang} />
          )}
        </section>
      ) : (
        <>
          <div className="insights-toolbar">
            <input
              type="search"
              className="insights-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={zh ? "搜索任务名称、ID 或状态…" : "Search mission, ID or status…"}
              aria-label={zh ? "搜索任务" : "Search missions"}
            />
            <span className="insights-count">
              {zh
                ? `${filteredMissions.length} / ${missions.length} 个任务`
                : `${filteredMissions.length} / ${missions.length} missions`}
            </span>
          </div>

          <div className="insights-list">
            <div className="insights-row insights-head-row">
              <span />
              <span>{zh ? "任务" : "Mission"}</span>
              <span>{zh ? "任务 ID" : "Mission ID"}</span>
              <span>{zh ? "状态" : "Status"}</span>
              <span>{zh ? "阶段" : "Stages"}</span>
              <span>{zh ? "耗时" : "Time"}</span>
              <span>{zh ? "工具" : "Tools"}</span>
              <span>{zh ? "自审" : "Reviews"}</span>
              <span>{zh ? "执行时间" : "Executed"}</span>
              <span>{zh ? "创建时间" : "Created"}</span>
            </div>

            {!missions.length && (
              <p className="insights-empty">
                {zh ? "还没有可展示的任务。" : "No missions yet."}
              </p>
            )}
            {missions.length > 0 && !filteredMissions.length && (
              <p className="insights-empty">
                {zh ? `没有匹配「${query}」的任务。` : `No missions matching “${query}”.`}
              </p>
            )}

            {filteredMissions.map((m) => {
              const on = selectedSet.has(m.id);
              const disabled = !on && selected.length >= MAX_COMPARE;
              const startMs = toMs(m.first_started_at);
              const endMs = toMs(m.last_ended_at);
              const createdMs = toMs(m.created_at);
              return (
                <div
                  className={`insights-row${on ? " on" : ""}`}
                  key={m.id}
                  onClick={() => openDetail(m.id)}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={disabled}
                    className={disabled ? "muted" : ""}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggle(m.id)}
                    aria-label={m.title}
                  />
                  <button type="button" className="insights-title" title={m.title}>
                    {m.title}
                  </button>
                  <CopyableId id={m.id} zh={zh} className="insights-id" />
                  <span className={`insights-status s-${m.status}`}>{m.status}</span>
                  <span>{m.has_metrics ? m.stage_count : "—"}</span>
                  <span>{m.has_metrics ? formatDuration(m.total_duration_ms) : "—"}</span>
                  <span>{m.has_metrics ? m.tool_calls : "—"}</span>
                  <span>{m.has_metrics ? m.review_rounds : "—"}</span>
                  <span
                    className="insights-time"
                    title={`${formatClockFull(startMs)} → ${formatClockFull(endMs)}`}
                  >
                    {formatRange(startMs, endMs)}
                  </span>
                  <span className="insights-time" title={formatClockFull(createdMs)}>
                    {formatClock(createdMs)}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="insights-chart">
            {!selected.length && (
              <p className="insights-empty">
                {zh ? "勾选任务查看阶段耗时（最多 5 个）。" : "Select missions to inspect stage timing (up to 5)."}
              </p>
            )}
            {selected.length > 0 && loading && (
              <p className="insights-empty">{zh ? "加载中…" : "Loading…"}</p>
            )}
            {selected.length > 0 && !loading && details.length > 0 && (
              <>
                <div className="insights-chart-head">
                  <b>
                    {selected.length === 1
                      ? zh
                        ? "单任务阶段甘特图"
                        : "Stage timeline"
                      : zh
                        ? `对比 ${selected.length} 个任务`
                        : `Comparing ${selected.length} missions`}
                  </b>
                  <span>
                    {zh ? "相对起点对齐，条形长度 = 阶段耗时" : "Aligned at start · bar = stage duration"}
                  </span>
                </div>
                <MissionGantt missions={details} lang={lang} />
              </>
            )}
            {selected.length > 0 && !loading && details.length === 0 && !error && (
              <p className="insights-empty">
                {zh ? "该任务无耗时指标（功能上线前创建）。" : "No metrics for these missions."}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
