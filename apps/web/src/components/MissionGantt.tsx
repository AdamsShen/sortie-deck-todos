import type { InsightMissionDetail, InsightStage, Lang } from "../types";

type Props = {
  missions: InsightMissionDetail[];
  lang: Lang;
};

const STATUS_LABEL: Record<string, { zh: string; en: string }> = {
  ok: { zh: "成功", en: "ok" },
  fail: { zh: "失败", en: "failed" },
  needs_hitl: { zh: "待确认", en: "waiting" },
  error: { zh: "异常", en: "error" },
};

/** 毫秒 → 人类可读时长（1.2s / 3m 05s / 1h 02m）。 */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  if (total < 1000) return `${total}ms`;
  const seconds = Math.floor(total / 1000);
  if (seconds < 60) return `${(total / 1000).toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/**
 * epoch 秒（阶段指标）或 ISO 字符串（任务创建时间）→ 毫秒时间戳。
 * 后端一律存 UTC / epoch，这里统一收口后按浏览器本地时区渲染，避免两套时间口径混用导致时区偏差。
 */
export function toMs(value: number | string | null | undefined): number | null {
  if (value == null) return null;
  const ms = typeof value === "number" ? value * 1000 : new Date(value).getTime();
  return Number.isFinite(ms) && ms > 0 ? ms : null;
}

/** 毫秒 → MM-DD HH:mm，用于列表列与阶段明细表。 */
export function formatClock(ms: number | null): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 毫秒 → YYYY-MM-DD HH:mm:ss，用于 title 悬浮展示完整时间。 */
export function formatClockFull(ms: number | null): string {
  if (ms == null) return "";
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * 执行区间：同一天只显示时刻，跨天补日期，避免窄列被完整日期撑爆；完整时间由 title 提供。
 */
export function formatRange(startMs: number | null, endMs: number | null): string {
  if (startMs == null || endMs == null) return "—";
  const s = new Date(startMs);
  const e = new Date(endMs);
  const clock = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (s.toDateString() === e.toDateString()) return `${clock(s)} → ${clock(e)}`;
  const md = (d: Date) => `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return `${md(s)} ${clock(s)} → ${md(e)} ${clock(e)}`;
}

function pct(value: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, (value / total) * 100));
}

/**
 * 阶段甘特图：每个阶段一行，条形按「相对任务起点」定位。
 *
 * 多任务对比时共用最大总时长作为横轴，各任务都以自己的首阶段为 0 点，
 * 因此能直接比较「同一阶段谁更慢」，而不是被绝对时间戳拉开。
 */
export function MissionGantt({ missions, lang }: Props) {
  const zh = lang === "zh";
  const total = Math.max(1, ...missions.map((m) => m.span?.duration_ms ?? 0));

  return (
    <div className="gantt">
      {missions.map((detail) => {
        const title = detail.mission.title;
        const span = detail.span?.duration_ms ?? 0;
        return (
          <section className="gantt-mission" key={detail.mission.id}>
            <header className="gantt-mission-head">
              <b>{title}</b>
              <span>
                {zh ? "总耗时" : "Total"} {formatDuration(span)}
              </span>
            </header>

            {!detail.stages.length ? (
              <p className="gantt-empty">
                {zh ? "该任务无耗时指标（功能上线前创建）" : "No metrics (created before the feature shipped)"}
              </p>
            ) : (
              <div className="gantt-body">
                <div className="gantt-ticks" aria-hidden="true">
                  <span>0%</span>
                  <span>25%</span>
                  <span>50%</span>
                  <span>75%</span>
                  <span>100%</span>
                </div>
                {detail.stages.map((stage) => {
                  const status = STATUS_LABEL[stage.status] || {
                    zh: stage.status || "未知",
                    en: stage.status || "unknown",
                  };
                  const tip = [
                    stage.stage,
                    stage.attempt > 1 ? `#${stage.attempt}` : "",
                    formatDuration(stage.duration_ms),
                    `${stage.tool_calls} ${zh ? "次工具" : "tools"}`,
                    stage.review_rounds
                      ? `${stage.review_rounds} ${zh ? "轮自审" : "reviews"}`
                      : "",
                    zh ? status.zh : status.en,
                  ]
                    .filter(Boolean)
                    .join(" · ");
                  return (
                    <div className="gantt-row" key={`${stage.stage}-${stage.attempt}`}>
                      <div className="gantt-label">
                        <span>{stage.stage}</span>
                        {stage.attempt > 1 && <em>#{stage.attempt}</em>}
                      </div>
                      <div className="gantt-track">
                        <div
                          className={`gantt-bar s-${stage.status || "ok"}`}
                          style={{
                            left: `${pct(stage.offset_ms, total)}%`,
                            width: `${Math.max(0.6, pct(stage.duration_ms, total))}%`,
                          }}
                          title={tip}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

type StageTableProps = {
  stages: InsightStage[];
  lang: Lang;
};

/**
 * 阶段耗时明细表：每阶段一行，给出精确起止时刻、耗时与占比。
 *
 * 为什么与甘特图并存：甘特图看相对位置与间隙，明细表给可读数字（Airflow Task Instances /
 * Langfuse observations 也都同时提供两者）。耗时最长的行高亮并标占比，但这是「最慢阶段」
 * 而非关键路径 —— stage_metrics 没有依赖边，并行波次下最长单阶段不等于决定总时长的链路。
 */
export function MissionStageTable({ stages, lang }: StageTableProps) {
  const zh = lang === "zh";
  const total = stages.reduce((sum, s) => sum + s.duration_ms, 0);
  const slowest = stages.reduce((max, s) => Math.max(max, s.duration_ms), 0);
  return (
    <div className="stage-table">
      <div className="stage-table-row stage-table-head">
        <span>{zh ? "阶段" : "Stage"}</span>
        <span>{zh ? "状态" : "Status"}</span>
        <span>{zh ? "开始" : "Start"}</span>
        <span>{zh ? "结束" : "End"}</span>
        <span>{zh ? "耗时 / 占比" : "Duration / share"}</span>
        <span>{zh ? "工具" : "Tools"}</span>
        <span>{zh ? "自审" : "Reviews"}</span>
      </div>
      {stages.map((stage) => {
        const share = total > 0 ? Math.round((stage.duration_ms / total) * 100) : 0;
        const status = STATUS_LABEL[stage.status] || {
          zh: stage.status || "未知",
          en: stage.status || "unknown",
        };
        const startMs = toMs(stage.started_at);
        const endMs = toMs(stage.ended_at);
        return (
          <div
            className={`stage-table-row${slowest > 0 && stage.duration_ms === slowest ? " slowest" : ""}`}
            key={`${stage.stage}-${stage.attempt}`}
          >
            <span className="stage-table-name">
              {stage.stage}
              {stage.attempt > 1 && <em>#{stage.attempt}</em>}
            </span>
            <span className={`stage-table-status s-${stage.status || "ok"}`}>
              {zh ? status.zh : status.en}
            </span>
            <span className="insights-time" title={formatClockFull(startMs)}>
              {formatClock(startMs)}
            </span>
            <span className="insights-time" title={formatClockFull(endMs)}>
              {formatClock(endMs)}
            </span>
            <span className="stage-table-duration">
              <b>{formatDuration(stage.duration_ms)}</b>
              <i className="stage-table-bar">
                <i style={{ width: `${share}%` }} />
              </i>
              <em>{share}%</em>
            </span>
            <span>{stage.tool_calls}</span>
            <span>{stage.review_rounds}</span>
          </div>
        );
      })}
    </div>
  );
}
