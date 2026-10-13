import type { I18nKey } from "../i18n";
import type { Initiative, Lang, PipelineStageInfo } from "../types";

export function PipelineEditor({
  selected,
  lang,
  tr,
  busy,
  pipelineEditing,
  setPipelineEditing,
  pipelineDraft,
  setPipelineDraft,
  stageCatalog,
  onMoveStage,
  onRemoveStage,
  onToggleHitl,
  onAddStage,
  onSaveDraft,
}: {
  selected: Initiative;
  lang: Lang;
  tr: (key: I18nKey | string) => string;
  busy: boolean;
  pipelineEditing: boolean;
  setPipelineEditing: (v: boolean) => void;
  pipelineDraft: PipelineStageInfo[];
  setPipelineDraft: (stages: PipelineStageInfo[]) => void;
  stageCatalog: PipelineStageInfo[];
  onMoveStage: (idx: number, dir: -1 | 1) => void;
  onRemoveStage: (idx: number) => void;
  onToggleHitl: (idx: number) => void;
  onAddStage: (id: string) => void;
  onSaveDraft: (confirm: boolean) => void;
}) {
  const displayStages = pipelineEditing
    ? pipelineDraft
    : (selected.meta?.pipeline_specs as PipelineStageInfo[]) || pipelineDraft;

  return (
    <section className="panel pipeline-editor">
      <div className="panel-label">
        {selected.meta?.pipeline_confirmed ? tr("pipelineConfirmed") : tr("pipelinePending")}
        {selected.meta?.pipeline_proposal && typeof selected.meta.pipeline_proposal === "object"
          ? ` · ${(selected.meta.pipeline_proposal as { agent_title?: string }).agent_title || "planner"}`
          : ""}
      </div>
      <p className="loadout-hint">
        {String(
          (selected.meta?.pipeline_proposal as { rationale?: string } | undefined)?.rationale ||
            selected.meta?.track_reason ||
            tr("pipelineHint")
        )}
      </p>
      <div className="pipeline-stage-list">
        <div className="pipeline-stage-head" aria-hidden="true">
          <span className="pipeline-stage-id" title="阶段唯一标识（stage id），取自阶段目录">
            阶段 ID
          </span>
          <strong title="阶段显示名称，按语言切换中/英文">名称</strong>
          <span className="muted" title="负责该阶段的角色 Agent">角色</span>
          <span className="muted" title="该阶段结束后是否需要人工审批（HITL 门禁）">
            门禁
          </span>
        </div>
        {displayStages.map((s, idx, arr) => (
          <div key={`${s.id}-${idx}`} className="pipeline-stage-row">
            <span className="pipeline-stage-id">{s.id}</span>
            <strong>{lang === "en" ? s.label_en || s.label : s.label}</strong>
            <span className="muted">{s.role}</span>
            {s.parallel_group ? <span className="st gate">∥ {s.parallel_group}</span> : null}
            {s.branch ? <span className="st gate">⤴ {s.branch}</span> : null}
            {s.depends_on?.length ? (
              <span
                className="muted"
                title={`依赖前置阶段（需全部完成后才开始）：${s.depends_on
                  .map((id) => (displayStages.find((x) => x.id === id)?.label || id))
                  .join("、")}`}
              >
                ←{s.depends_on.join(",")}
              </span>
            ) : null}
            {s.hitl_after ? <span className="st gate">{tr("pipelineHitl")}</span> : null}
            {pipelineEditing && (
              <span className="pipeline-stage-actions">
                <button type="button" className="btn linkish" onClick={() => onMoveStage(idx, -1)} disabled={idx <= 1}>
                  {tr("pipelineMoveUp")}
                </button>
                <button
                  type="button"
                  className="btn linkish"
                  onClick={() => onMoveStage(idx, 1)}
                  disabled={idx >= arr.length - 2}
                >
                  {tr("pipelineMoveDown")}
                </button>
                <button type="button" className="btn linkish" onClick={() => onToggleHitl(idx)}>
                  {tr("pipelineHitl")}
                </button>
                <button type="button" className="btn linkish" onClick={() => onRemoveStage(idx)}>
                  {tr("pipelineRemove")}
                </button>
              </span>
            )}
          </div>
        ))}
      </div>
      {pipelineEditing && (
        <div className="loadout-slot-actions" style={{ marginTop: "0.5rem", flexWrap: "wrap" }}>
          <span className="muted">{tr("pipelineAdd")}</span>
          {stageCatalog
            .filter((c) => !pipelineDraft.some((d) => d.id === c.id))
            .map((c) => (
              <button key={c.id} type="button" className="toolkit-chip" onClick={() => onAddStage(c.id)}>
                + {lang === "en" ? c.label_en || c.label : c.label}
              </button>
            ))}
        </div>
      )}
      <div className="kb-actions" style={{ marginTop: "0.75rem" }}>
        {!pipelineEditing ? (
          <button
            className="btn ghost"
            type="button"
            disabled={busy}
            onClick={() => {
              const specs = (selected.meta?.pipeline_specs as PipelineStageInfo[]) || [];
              setPipelineDraft(specs.map((s) => ({ ...s })));
              setPipelineEditing(true);
            }}
          >
            {tr("pipelineEdit")}
          </button>
        ) : (
          <>
            <button className="btn ghost" type="button" disabled={busy} onClick={() => onSaveDraft(false)}>
              {tr("pipelineSave")}
            </button>
            <button className="btn primary" type="button" disabled={busy} onClick={() => onSaveDraft(true)}>
              {tr("pipelineConfirm")}
            </button>
            <button
              className="btn ghost"
              type="button"
              onClick={() => {
                setPipelineEditing(false);
                const specs = (selected.meta?.pipeline_specs as PipelineStageInfo[]) || [];
                setPipelineDraft(specs.map((s) => ({ ...s })));
              }}
            >
              {tr("cancel")}
            </button>
          </>
        )}
      </div>
    </section>
  );
}
