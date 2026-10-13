import { useEffect, useId, useMemo, useRef, useState } from "react";
import { RoleAvatar } from "./RoleAvatar";
import { StageUnlockFX } from "./StageUnlockFX";
import { playSfx } from "../sfx";
import type { Initiative, Lang } from "../types";

export type RailStage = {
  id: string;
  label: string;
  role?: string;
  avatar?: string | null;
  title?: string;
  parallel_group?: string | null;
  branch?: string | null;
  depends_on?: string[] | null;
};

type RailColumn = {
  key: string;
  parallel: boolean;
  group?: string;
  step?: number;
  stages: RailStage[];
};

type NodePos = {
  id: string;
  col: number;
  lane: number;
  x: number;
  y: number;
  stage: RailStage;
  parallel: boolean;
  branch?: string;
};

const DEFAULT_STAGES: RailStage[] = [
  { id: "intake", label: "INTAKE" },
  { id: "product_prd", label: "PRD" },
  { id: "design_ui", label: "DESIGN" },
  { id: "eng_implement", label: "ENG" },
  { id: "qa_verify", label: "QA" },
  { id: "deploy_preview", label: "DEPLOY" },
  { id: "done", label: "DONE" },
];

const EDGE_PCT = 0.04;
const SPAN_PCT = 1 - EDGE_PCT * 2;
const NODE_W = 108;
const LANE_GAP = 124;
/** Vertical offset from node top to avatar center (edge endpoint). */
const AVATAR_CY = 30;
const NODE_BLOCK = 92;
const AVATAR_SIZE = 60;
const AVATAR_SIZE_BRANCH = 56;

function branchKey(s: RailStage): string {
  return (s.branch || "").trim() || s.id;
}

/**
 * Collapse consecutive same parallel_group into a region, then expand into
 * one column per branch step so Web开发→Web自测 stays on one lane.
 */
function groupRailColumns(rail: RailStage[]): RailColumn[] {
  const cols: RailColumn[] = [];
  let i = 0;
  while (i < rail.length) {
    const g = (rail[i].parallel_group || "").trim();
    if (!g) {
      cols.push({ key: rail[i].id, parallel: false, stages: [rail[i]] });
      i += 1;
      continue;
    }
    const members: RailStage[] = [rail[i]];
    let j = i + 1;
    while (j < rail.length && (rail[j].parallel_group || "").trim() === g) {
      members.push(rail[j]);
      j += 1;
    }
    // Order branches by first appearance; keep stage order within each branch
    const branchOrder: string[] = [];
    const buckets = new Map<string, RailStage[]>();
    for (const m of members) {
      const bid = branchKey(m);
      if (!buckets.has(bid)) {
        branchOrder.push(bid);
        buckets.set(bid, []);
      }
      buckets.get(bid)!.push(m);
    }
    const branches = branchOrder.map((id) => buckets.get(id)!);
    const maxSteps = Math.max(1, ...branches.map((b) => b.length));
    for (let step = 0; step < maxSteps; step++) {
      const stagesAtStep: RailStage[] = [];
      for (const br of branches) {
        if (step < br.length) stagesAtStep.push(br[step]);
      }
      cols.push({
        key: `pg:${g}:step${step}`,
        parallel: branches.length > 1 || maxSteps > 1,
        group: g,
        step,
        stages: stagesAtStep,
      });
    }
    i = j;
  }
  return cols;
}

function columnIndexForStage(stage: string, cols: RailColumn[]): number {
  const idx = cols.findIndex((c) => c.stages.some((s) => s.id === stage));
  return idx >= 0 ? idx : 0;
}

function layoutNodes(cols: RailColumn[]): { nodes: NodePos[]; height: number } {
  const denom = Math.max(cols.length - 1, 1);
  const nodes: NodePos[] = [];

  // lane 按并行区独立编号：连续同 group 的列算一个区，新区从 lane 0 重新开始。
  // 若全局累计，第二个并行区会被排到很高的 lane，超出画布高度盖住下方内容。
  const laneOf = new Map<string, number>();
  let nextLane = 0;
  let maxLanes = 1;
  cols.forEach((c, idx) => {
    if (!c.parallel) return;
    const prev = idx > 0 ? cols[idx - 1] : undefined;
    const contiguous = Boolean(prev && prev.parallel && prev.group === c.group);
    if (!contiguous) nextLane = 0;
    for (const s of c.stages) {
      const bid = branchKey(s);
      if (!laneOf.has(bid)) {
        laneOf.set(bid, nextLane);
        nextLane += 1;
      }
    }
    maxLanes = Math.max(maxLanes, nextLane);
  });

  const laneCount = maxLanes;
  const height =
    laneCount <= 1
      ? 148
      : Math.max(220, 40 + (laneCount - 1) * LANE_GAP + NODE_BLOCK + 56);
  const stackH = Math.max(0, laneCount - 1) * LANE_GAP;
  const startY = Math.max(16, (height - stackH - NODE_BLOCK) / 2);

  for (let col = 0; col < cols.length; col++) {
    const c = cols[col];
    const x = (EDGE_PCT + (col / denom) * SPAN_PCT) * 100;
    if (!c.parallel) {
      nodes.push({
        id: c.stages[0].id,
        col,
        lane: 0,
        x,
        y: startY + (laneCount > 1 ? stackH / 2 : 0),
        stage: c.stages[0],
        parallel: false,
      });
      continue;
    }
    c.stages.forEach((stage) => {
      const bid = branchKey(stage);
      const lane = laneOf.get(bid) ?? 0;
      nodes.push({
        id: stage.id,
        col,
        lane,
        x,
        y: startY + lane * LANE_GAP,
        stage,
        parallel: true,
        branch: bid,
      });
    });
  }
  return { nodes, height };
}

type Edge = {
  key: string;
  d: string;
  active: boolean;
  live: boolean;
  kind: "trunk" | "branch" | "merge";
};

function cy(n: NodePos): number {
  return n.y + AVATAR_CY;
}

function buildEdges(cols: RailColumn[], nodes: NodePos[], activeCol: number, allDone = false): Edge[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges: Edge[] = [];
  const denom = Math.max(cols.length - 1, 1);
  const colX = (col: number) => (EDGE_PCT + (col / denom) * SPAN_PCT) * 100;

  const push = (edge: Omit<Edge, "live"> & { live?: boolean }) => {
    edges.push({ live: false, ...edge });
  };

  for (let i = 0; i < cols.length - 1; i++) {
    const a = cols[i];
    const b = cols[i + 1];
    const active = allDone || i < activeCol;
    // Frontier into the live column, or within an active parallel region
    const live =
      !allDone &&
      (i === activeCol - 1 || (i === activeCol && a.parallel && b.parallel && a.group === b.group));
    const x0 = colX(i);
    const x1 = colX(i + 1);
    const mid = (x0 + x1) / 2;
    const sameRegion = Boolean(a.group && a.group === b.group);

    if (!a.parallel && !b.parallel) {
      const na = byId.get(a.stages[0].id)!;
      const nb = byId.get(b.stages[0].id)!;
      push({
        key: `${na.id}->${nb.id}`,
        kind: "trunk",
        active,
        live,
        d: `M ${na.x} ${cy(na)} L ${nb.x} ${cy(nb)}`,
      });
    } else if (!a.parallel && b.parallel) {
      const src = byId.get(a.stages[0].id)!;
      const sy = cy(src);
      push({
        key: `split-${a.stages[0].id}`,
        kind: "branch",
        active,
        live,
        d: `M ${src.x} ${sy} L ${mid} ${sy}`,
      });
      for (const st of b.stages) {
        const dst = byId.get(st.id)!;
        const dy = cy(dst);
        push({
          key: `${src.id}->${st.id}`,
          kind: "branch",
          active,
          live,
          d: `M ${mid} ${sy} C ${mid + (x1 - mid) * 0.4} ${sy}, ${mid + (x1 - mid) * 0.4} ${dy}, ${dst.x} ${dy}`,
        });
      }
    } else if (a.parallel && !b.parallel) {
      const dst = byId.get(b.stages[0].id)!;
      const dy = cy(dst);
      for (const st of a.stages) {
        const src = byId.get(st.id)!;
        const sy = cy(src);
        push({
          key: `${st.id}->${dst.id}`,
          kind: "merge",
          active,
          live,
          d: `M ${src.x} ${sy} C ${mid - (mid - x0) * 0.4} ${sy}, ${mid - (mid - x0) * 0.4} ${dy}, ${mid} ${dy}`,
        });
      }
      push({
        key: `join-${dst.id}`,
        kind: "merge",
        active,
        live,
        d: `M ${mid} ${dy} L ${dst.x} ${dy}`,
      });
    } else if (sameRegion) {
      for (const st of a.stages) {
        const bid = branchKey(st);
        const nxt = b.stages.find((s) => branchKey(s) === bid);
        if (!nxt) continue;
        const na = byId.get(st.id)!;
        const nb = byId.get(nxt.id)!;
        push({
          key: `${st.id}->${nxt.id}`,
          kind: "trunk",
          active,
          live,
          d: `M ${na.x} ${cy(na)} L ${nb.x} ${cy(nb)}`,
        });
      }
    } else {
      const n = Math.max(a.stages.length, b.stages.length);
      for (let k = 0; k < n; k++) {
        const sa = a.stages[Math.min(k, a.stages.length - 1)];
        const sb = b.stages[Math.min(k, b.stages.length - 1)];
        const na = byId.get(sa.id)!;
        const nb = byId.get(sb.id)!;
        push({
          key: `${sa.id}->${sb.id}-${k}`,
          kind: "trunk",
          active,
          live,
          d: `M ${na.x} ${cy(na)} L ${nb.x} ${cy(nb)}`,
        });
      }
    }
  }
  return edges;
}

export function railFromInitiative(ini: Initiative | null | undefined, lang: Lang): RailStage[] {
  const meta = ini?.meta || {};
  const specs =
    (meta.pipeline_specs as {
      id: string;
      label?: string;
      label_en?: string;
      role?: string;
      parallel_group?: string;
      branch?: string;
      depends_on?: string[];
    }[]) || [];
  const rail = meta.pipeline_rail as
    | {
        id: string;
        label: string;
        label_en?: string;
        role?: string;
        parallel_group?: string;
        branch?: string;
        depends_on?: string[];
      }[]
    | undefined;
  const source = Array.isArray(rail) && rail.length ? rail : specs;
  if (Array.isArray(source) && source.length) {
    return source.map((s) => {
      const roleId = s.role || specs.find((x) => x.id === s.id)?.role;
      const agent = ini?.roles?.find((r) => r.role === roleId);
      const pg = s.parallel_group || specs.find((x) => x.id === s.id)?.parallel_group || null;
      const br = s.branch || specs.find((x) => x.id === s.id)?.branch || null;
      const deps = s.depends_on || specs.find((x) => x.id === s.id)?.depends_on || null;
      return {
        id: s.id,
        label:
          lang === "en"
            ? (s as { label_en?: string }).label_en || s.label || s.id
            : s.label || s.id,
        role: roleId,
        avatar: agent?.avatar,
        title: agent?.title,
        parallel_group: pg,
        branch: br,
        depends_on: deps,
      };
    });
  }
  const ids = meta.pipeline_stages as string[] | undefined;
  if (Array.isArray(ids) && ids.length) {
    return ids.map((id) => ({ id, label: id }));
  }
  return [...DEFAULT_STAGES];
}

export function PipelineRail({
  stage,
  status,
  stages,
  selectedId,
  onSelect,
  speakingRoles,
  lang = "zh",
  missionKey,
  activeParallel,
}: {
  stage: string;
  status: string;
  stages?: RailStage[];
  selectedId?: string | null;
  onSelect?: (id: string, label: string) => void;
  speakingRoles?: Set<string>;
  lang?: Lang;
  missionKey?: string;
  activeParallel?: string[];
}) {
  const rail: RailStage[] = stages && stages.length ? stages : [...DEFAULT_STAGES];
  const columns = useMemo(() => groupRailColumns(rail), [rail]);
  const activeCol = columnIndexForStage(stage, columns);
  const { nodes, height } = useMemo(() => layoutNodes(columns), [columns]);
  const edges = useMemo(
    () => buildEdges(columns, nodes, activeCol, status === "done"),
    [columns, nodes, activeCol, status],
  );
  const parallelSet = useMemo(() => new Set(activeParallel || []), [activeParallel]);
  const gid = useId().replace(/:/g, "");
  const chromaId = `rail-chroma-${gid}`;
  const chromaFastId = `rail-chroma-fast-${gid}`;
  const glowId = `rail-chroma-glow-${gid}`;

  const prevStageRef = useRef<string | null>(null);
  const prevMissionRef = useRef<string | undefined>(missionKey);
  const [unlock, setUnlock] = useState<{ xPct: number; label: string; key: number } | null>(null);
  const [pulseCol, setPulseCol] = useState<number | null>(null);

  useEffect(() => {
    if (prevMissionRef.current !== missionKey) {
      prevMissionRef.current = missionKey;
      prevStageRef.current = stage;
      setUnlock(null);
      setPulseCol(null);
      return;
    }
    const prev = prevStageRef.current;
    prevStageRef.current = stage;
    if (prev == null || prev === stage) return;
    const prevIdx = columnIndexForStage(prev, columns);
    if (activeCol <= prevIdx) return;
    const node = nodes.find((n) => n.id === stage) || nodes.find((n) => n.col === activeCol);
    const xPct = node?.x ?? 50;
    const label = columns[activeCol]?.stages.map((s) => s.label).join(" ∥ ") || stage;
    setUnlock({ xPct, label, key: Date.now() });
    setPulseCol(activeCol);
    playSfx("unlock");
    const timer = window.setTimeout(() => setPulseCol(null), 1400);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, missionKey, activeCol]);

  const hasBranch = columns.some((c) => c.parallel);

  return (
    <div
      className={`rail rail-cast rail-graph${hasBranch ? " rail-branched" : ""}`}
      style={{ height }}
      aria-label="pipeline graph"
    >
      <svg
        className="rail-edges"
        viewBox={`0 0 100 ${height}`}
        preserveAspectRatio="none"
        aria-hidden
      >
        <defs>
          <linearGradient
            id={chromaId}
            gradientUnits="userSpaceOnUse"
            x1="0"
            y1="0"
            x2="36"
            y2="0"
          >
            {/* 渐变色取 500/600 系深色调：浅色主题下细线也够对比 */}
            <stop offset="0%" stopColor="#f0367e" />
            <stop offset="18%" stopColor="#9333ea" />
            <stop offset="36%" stopColor="#0284c7" />
            <stop offset="54%" stopColor="#0d9488" />
            <stop offset="72%" stopColor="#65a30d" />
            <stop offset="88%" stopColor="#d97706" />
            <stop offset="100%" stopColor="#f0367e" />
            <animateTransform
              attributeName="gradientTransform"
              type="translate"
              from="0 0"
              to="36 0"
              dur="2.4s"
              repeatCount="indefinite"
            />
          </linearGradient>
          <linearGradient
            id={chromaFastId}
            gradientUnits="userSpaceOnUse"
            x1="0"
            y1="0"
            x2="28"
            y2="0"
          >
            <stop offset="0%" stopColor="#ec4899" />
            <stop offset="25%" stopColor="#c026d3" />
            <stop offset="50%" stopColor="#06b6d4" />
            <stop offset="75%" stopColor="#4ade80" />
            <stop offset="100%" stopColor="#ec4899" />
            <animateTransform
              attributeName="gradientTransform"
              type="translate"
              from="0 0"
              to="28 0"
              dur="1.35s"
              repeatCount="indefinite"
            />
          </linearGradient>
          <filter id={glowId} x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur stdDeviation="1.2" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        {edges.map((e) => (
          <g key={e.key} className="rail-edge-group">
            {(e.active || e.live) && (
              <path
                d={e.d}
                className={`rail-edge-halo${e.live ? " live" : " on"}`}
                style={{ stroke: `url(#${e.live ? chromaFastId : chromaId})` }}
                vectorEffect="non-scaling-stroke"
                fill="none"
              />
            )}
            <path
              d={e.d}
              className={`rail-edge rail-edge-${e.kind}${e.active ? " active" : ""}${e.live ? " live" : ""}`}
              style={
                e.active || e.live
                  ? { stroke: `url(#${e.live ? chromaFastId : chromaId})` }
                  : undefined
              }
              vectorEffect="non-scaling-stroke"
              fill="none"
              filter={e.active || e.live ? `url(#${glowId})` : undefined}
            />
            {(e.active || e.live) && (
              <path
                d={e.d}
                className={`rail-edge-bead${e.live ? " live" : " active"}`}
                vectorEffect="non-scaling-stroke"
                fill="none"
              />
            )}
          </g>
        ))}
      </svg>

      {nodes.map((n) => {
        const colIdx = n.col;
        let nodeState: "done" | "live" | "gate" | "idle" = "idle";
        if (status === "done" || colIdx < activeCol) nodeState = "done";
        else if (colIdx === activeCol) {
          if (n.parallel) {
            const waveLive = parallelSet.size === 0 || parallelSet.has(n.id);
            nodeState = waveLive
              ? status === "waiting_hitl" && n.id === stage
                ? "gate"
                : "live"
              : "idle";
          } else {
            nodeState = status === "waiting_hitl" ? "gate" : "live";
          }
        }
        const selected = selectedId === n.id;
        const speaking = Boolean(n.stage.role && speakingRoles?.has(n.stage.role));
        const unlocking = pulseCol === colIdx;
        return (
          <button
            key={n.id}
            type="button"
            className={`rail-node ${nodeState}${selected ? " selected" : ""}${speaking ? " speaking" : ""}${unlocking ? " unlocking" : ""}${n.parallel ? " branched" : ""}`}
            style={{
              left: `${n.x}%`,
              top: n.y,
              width: NODE_W,
            }}
            onClick={() => onSelect?.(n.id, n.stage.label)}
            title={
              n.parallel
                ? `${n.stage.label} · ${lang === "en" ? "parallel branch" : "并行分支"}`
                : n.stage.label
            }
          >
            {n.stage.role || n.stage.avatar ? (
              <RoleAvatar
                avatar={n.stage.avatar}
                role={n.stage.role}
                title={n.stage.title || n.stage.label}
                size={n.parallel ? AVATAR_SIZE_BRANCH : AVATAR_SIZE}
                live={nodeState === "live" || nodeState === "gate"}
                speaking={speaking}
                className="rail-avatar"
              />
            ) : (
              <span className="rail-dot" />
            )}
            <span className="rail-label">{n.stage.label}</span>
            {speaking ? (
              <span className="rail-bubble" aria-hidden>
                …
              </span>
            ) : null}
          </button>
        );
      })}

      {unlock ? (
        <StageUnlockFX
          key={unlock.key}
          xPct={unlock.xPct}
          label={unlock.label}
          lang={lang === "en" ? "en" : "zh"}
          onDone={() => setUnlock(null)}
        />
      ) : null}
    </div>
  );
}
