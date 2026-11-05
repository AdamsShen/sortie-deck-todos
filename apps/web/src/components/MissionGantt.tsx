import type { InsightMissionDetail, Lang } from "../types";

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
