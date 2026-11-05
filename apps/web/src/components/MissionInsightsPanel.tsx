import { useEffect, useMemo, useState } from "react";

import { compareInsightMissions, getInsightMission, listInsightMissions } from "../api";
import type {
  InsightMissionDetail,
  InsightMissionSummary,
  InsightTotals,
  Lang,
} from "../types";
import { formatDuration, MissionGantt } from "./MissionGantt";

const MAX_COMPARE = 5;

type Props = {
  token: string;
  lang: Lang;
};

/**
 * 设置 › 数据 › 耗时分析。
 *
 * 权限严格按任务创建者：后端三个接口都会校验 created_by，这里只负责展示。
 * 勾选 1 个任务看单任务甘特图，勾选 2-5 个进入对比视图。
 */
export function MissionInsightsPanel({ token, lang }: Props) {
  const zh = lang === "zh";
  const [missions, setMissions] = useState<InsightMissionSummary[]>([]);
  const [totals, setTotals] = useState<InsightTotals | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [details, setDetails] = useState<InsightMissionDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

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

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  function toggle(id: string) {
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= MAX_COMPARE) return prev;
      return [...prev, id];
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

      {error && <p className="stg-note danger">{error}</p>}

      <div className="insights-list">
        <div className="insights-row insights-head-row">
          <span />
          <span>{zh ? "任务" : "Mission"}</span>
          <span>{zh ? "状态" : "Status"}</span>
          <span>{zh ? "阶段" : "Stages"}</span>
          <span>{zh ? "耗时" : "Time"}</span>
          <span>{zh ? "工具" : "Tools"}</span>
          <span>{zh ? "自审" : "Reviews"}</span>
        </div>

        {!missions.length && (
          <p className="insights-empty">
            {zh ? "还没有可展示的任务。" : "No missions yet."}
          </p>
        )}

        {missions.map((m) => {
          const on = selectedSet.has(m.id);
          const disabled = !on && selected.length >= MAX_COMPARE;
          return (
            <label className={`insights-row${on ? " on" : ""}${disabled ? " muted" : ""}`} key={m.id}>
              <input
                type="checkbox"
                checked={on}
                disabled={disabled}
                onChange={() => toggle(m.id)}
              />
              <span className="insights-title" title={m.title}>
                {m.title}
              </span>
              <span className={`insights-status s-${m.status}`}>{m.status}</span>
              <span>{m.has_metrics ? m.stage_count : "—"}</span>
              <span>{m.has_metrics ? formatDuration(m.total_duration_ms) : "—"}</span>
              <span>{m.has_metrics ? m.tool_calls : "—"}</span>
              <span>{m.has_metrics ? m.review_rounds : "—"}</span>
            </label>
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
    </div>
  );
}
