import { useMemo, useRef, useState, type DragEvent } from "react";
import { updateInitiativeMaterials } from "../api";
import { playSfx } from "../sfx";
import type { I18nKey } from "../i18n";
import type { ExternalMaterial, Lang } from "../types";
import { MissionBriefingChat } from "./MissionBriefingChat";
import type { ComposerAttachment } from "./ChatComposer";

type Props = {
  token: string;
  lang: Lang;
  tr: (key: I18nKey) => string;
  busy: boolean;
  setBusy: (v: boolean) => void;
  onCancel: () => void;
  onCreated: (id: string) => void;
  onError: (msg: string) => void;
};

/** Local draft row — carries the decoded text excerpt that the API receives. */
type DraftMaterial = ExternalMaterial & { uid: string };

const KIND_ORDER = ["prd", "plan", "design", "data", "other"] as const;
type MaterialKind = (typeof KIND_ORDER)[number];

const KIND_LABEL: Record<MaterialKind, { zh: string; en: string }> = {
  prd: { zh: "PRD", en: "PRD" },
  plan: { zh: "计划", en: "Plan" },
  design: { zh: "设计稿", en: "Design" },
  data: { zh: "数据", en: "Data" },
  other: { zh: "其他", en: "Other" },
};

const DEFAULT_ACCEPT =
  "image/*,.md,.txt,.json,.csv,.yaml,.yml,.log,.pdf,.doc,.docx,.fig,text/plain,text/markdown,application/json";
const TEXT_MIME = /^(text\/|application\/(json|yaml|x-yaml|xml))/i;
const TEXT_EXT = /\.(md|txt|json|csv|ya?ml|log|xml)$/i;
const MAX_FILES = 12;
const MAX_BYTES = 8 * 1024 * 1024;

function isTextLike(file: File): boolean {
  return TEXT_MIME.test(file.type) || TEXT_EXT.test(file.name);
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsText(file);
  });
}

/** Mirrors `sortie_deck.materials.estimate_tokens` (CJK ≈ 1, latin ≈ ¼). */
export function estimateTokens(text: string | undefined): number {
  if (!text) return 0;
  let cjk = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0) || 0;
    if (code >= 0x4e00 && code <= 0x9fff) cjk += 1;
  }
  const other = text.length - cjk;
  return cjk + Math.max(0, other) / 4;
}

function detectKind(name: string): MaterialKind {
  const n = name.toLowerCase();
  if (/(^|[^a-z])(prd|需求|requirement)/.test(n)) return "prd";
  if (/(计划|方案|plan|roadmap|design[_-]?doc|tech[_-]?design)/.test(n)) return "plan";
  if (/\.(fig|sketch|psd|ai|xd)$/.test(n) || /(设计稿|design|mockup|ui)/.test(n)) return "design";
  if (/\.(csv|xlsx?|json|log|tsv)$/.test(n) || /(数据|dataset|report|指标)/.test(n)) return "data";
  return "other";
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function uid(): string {
  return `mat_${Math.random().toString(36).slice(2, 10)}`;
}

let seq = 0;

function defaultPinned(kind: MaterialKind): boolean {
  // Text docs default to pinned; binaries (design mockups) default to archived.
  return kind === "prd" || kind === "plan" || kind === "data";
}

async function toDraft(file: File): Promise<DraftMaterial> {
  const kind = detectKind(file.name);
  let textExcerpt = "";
  if (isTextLike(file)) {
    try {
      textExcerpt = (await readAsText(file)).slice(0, 24_000);
    } catch {
      textExcerpt = "";
    }
  }
  seq += 1;
  return {
    uid: `${Date.now()}-${seq}`,
    id: uid(),
    name: file.name || (kind === "design" ? "design.png" : "file"),
    kind,
    mime: file.type || "application/octet-stream",
    size: file.size,
    pinned: defaultPinned(kind),
    text_excerpt: textExcerpt,
  };
}

/** Chat-composer attachments land in the external-materials panel (single intake). */
function fromAttachment(a: ComposerAttachment): DraftMaterial | null {
  if ((a.size || 0) > MAX_BYTES) return null;
  const kind: MaterialKind = a.kind === "image" ? "design" : detectKind(a.name);
  seq += 1;
  return {
    uid: `${Date.now()}-${seq}`,
    id: uid(),
    name: a.name || "file",
    kind,
    mime: a.mime || "application/octet-stream",
    size: a.size || 0,
    pinned: defaultPinned(kind),
    text_excerpt: a.textExcerpt || "",
  };
}

export function NewTaskPage({ token, lang, tr, busy, setBusy, onCancel, onCreated, onError }: Props) {
  const [materials, setMaterials] = useState<DraftMaterial[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const pinned = useMemo(() => materials.filter((m) => m.pinned), [materials]);
  const pinnedTokens = useMemo(
    () => Math.round(pinned.reduce((sum, m) => sum + estimateTokens(m.text_excerpt), 0)),
    [pinned],
  );

  async function addFiles(list: FileList | null) {
    if (!list || !list.length) return;
    const room = MAX_FILES - materials.length;
    if (room <= 0) {
      onError(lang === "zh" ? `最多 ${MAX_FILES} 份资料` : `Up to ${MAX_FILES} materials`);
      return;
    }
    const picked = Array.from(list).slice(0, room);
    const drafts: DraftMaterial[] = [];
    for (const file of picked) {
      if (file.size > MAX_BYTES) {
        onError(
          lang === "zh"
            ? `${file.name} 超过 ${formatSize(MAX_BYTES)}，已跳过`
            : `${file.name} exceeds ${formatSize(MAX_BYTES)}, skipped`,
        );
        continue;
      }
      drafts.push(await toDraft(file));
    }
    if (drafts.length) {
      playSfx("toggle");
      setMaterials((prev) => [...prev, ...drafts]);
    }
  }

  function addAttachments(atts: ComposerAttachment[]) {
    const room = MAX_FILES - materials.length;
    if (room <= 0) {
      onError(lang === "zh" ? `最多 ${MAX_FILES} 份资料` : `Up to ${MAX_FILES} materials`);
      return;
    }
    const drafts: DraftMaterial[] = [];
    for (const att of atts.slice(0, room)) {
      const draft = fromAttachment(att);
      if (!draft) {
        onError(
          lang === "zh"
            ? `${att.name} 超过 ${formatSize(MAX_BYTES)}，已跳过`
            : `${att.name} exceeds ${formatSize(MAX_BYTES)}, skipped`,
        );
        continue;
      }
      drafts.push(draft);
    }
    if (drafts.length) {
      playSfx("toggle");
      setMaterials((prev) => [...prev, ...drafts]);
    }
  }

  function cycleKind(uidKey: string) {
    setMaterials((prev) =>
      prev.map((m) =>
        m.uid === uidKey
          ? { ...m, kind: KIND_ORDER[(KIND_ORDER.indexOf(m.kind as MaterialKind) + 1) % KIND_ORDER.length] }
          : m,
      ),
    );
  }

  function togglePin(uidKey: string) {
    playSfx("toggle");
    setMaterials((prev) => prev.map((m) => (m.uid === uidKey ? { ...m, pinned: !m.pinned } : m)));
  }

  function removeAt(uidKey: string) {
    setMaterials((prev) => prev.filter((m) => m.uid !== uidKey));
  }

  const materialsPayload = useMemo(
    () => materials.map(({ uid: _uid, ...rest }) => rest),
    [materials],
  );

  /** Attach imported materials to a planner-created mission, then navigate. */
  async function handlePlannerCreated(id: string) {
    if (materialsPayload.length) {
      try {
        await updateInitiativeMaterials(token, id, materialsPayload);
      } catch (err) {
        onError(err instanceof Error ? err.message : String(err));
      }
    }
    onCreated(id);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    void addFiles(e.dataTransfer?.files || null);
  }

  const zh = lang === "zh";

  const plannerLabels = useMemo(
    () => ({
      title: zh ? "建队规划官" : "Squad planner",
      placeholder: zh ? "描述任务目标…" : "Describe the mission…",
      hint: zh
        ? "多轮对话 · 附件请从右侧外部资料导入 · /confirm 确认"
        : "Multimodal chat · attach via external materials · /confirm",
      send: tr("send"),
      confirm: zh ? "确认组建" : "Confirm",
      reject: zh ? "驳回重来" : "Reject",
      restart: zh ? "新会话" : "New chat",
      waterfall: zh ? "规划瀑布流" : "Planning waterfall",
    }),
    [lang, tr],
  );

  return (
    <main className="main page-pad intake-page">
      <div className="intake-page-inner">
        <div className="eyebrow">MISSION INTAKE</div>
        <h2 className="page-title">{tr("newTask")}</h2>
        <p className="hero-copy">
          {zh
            ? "与建队规划官多轮对话组建小队；导入的外部资料全部参与规划讨论，被钉住的将在任务创建后持续注入每轮对话的上下文。"
            : "Chat with the squad planner to form the squad; all imported materials join the planning discussion, and pinned ones keep feeding every squad turn after creation."}
        </p>

        <div className="intake-grid">
          <div className="intake-col intake-chat-col">
            <section className="panel intake-chat-panel">
              <MissionBriefingChat
                token={token}
                lang={lang}
                busy={busy}
                setBusy={setBusy}
                onError={onError}
                onCreated={handlePlannerCreated}
                labels={plannerLabels}
                materials={materialsPayload}
                onImportFiles={addAttachments}
                onMaterialsLoaded={(mats) =>
                  setMaterials(mats.map((m) => ({ ...m, uid: `rst_${m.id}` })))
                }
                onReset={() => setMaterials([])}
              />
            </section>
          </div>

          <div className="intake-col">
            <section className="panel">
              <div className="panel-label">
                {tr("intakeMaterials")}
                <span className="aux">
                  {materials.length} {zh ? "份" : "files"} · {tr("intakeKindHint")}
                </span>
              </div>
              <div
                className={`dropzone${dragging ? " hot" : ""}`}
                onClick={() => fileRef.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
              >
                <div className="dz-main">{tr("intakeDrop")}</div>
                <div className="dz-hint">
                  {zh ? "支持 PRD · 计划文档 · 设计稿 · 数据 等多格式，可批量" : "PRD · plan · design · data — multi-select"}
                </div>
                <input
                  ref={fileRef}
                  type="file"
                  multiple
                  accept={DEFAULT_ACCEPT}
                  style={{ display: "none" }}
                  onChange={(e) => {
                    void addFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
              </div>

              <div className="file-list">
                {materials.map((m) => (
                  <div key={m.uid} className={`file-card${m.pinned ? " pinned" : ""}`}>
                    <button
                      type="button"
                      className={`type-chip t-${m.kind}`}
                      title={tr("intakeKindHint")}
                      onClick={() => cycleKind(m.uid)}
                    >
                      {KIND_LABEL[(m.kind as MaterialKind) in KIND_LABEL ? (m.kind as MaterialKind) : "other"][lang]}
                    </button>
                    <div className="file-info">
                      <div className="file-name" title={m.name}>
                        {m.name}
                      </div>
                      <div className="file-meta">
                        {formatSize(m.size || 0)}
                        <span className="dot">·</span>
                        {m.text_excerpt
                          ? `≈${Math.round(estimateTokens(m.text_excerpt))} tok`
                          : zh
                            ? "非文本 · 供检索"
                            : "non-text"}
                      </div>
                    </div>
                    <button
                      type="button"
                      className={`ctx-toggle${m.pinned ? " on" : ""}`}
                      onClick={() => togglePin(m.uid)}
                    >
                      <span className="tgl-label">{m.pinned ? tr("intakePinned") : tr("intakeArchived")}</span>
                      <span className="switch" />
                    </button>
                    <button type="button" className="file-remove" title={tr("delete")} onClick={() => removeAt(m.uid)}>
                      ×
                    </button>
                  </div>
                ))}
                {!materials.length && (
                  <div className="empty-files">
                    {zh ? "── 尚未导入资料 · 支持一次多选 ──" : "── no materials yet ──"}
                  </div>
                )}
              </div>
            </section>

            <section className="panel">
              <div className="panel-label">
                {tr("intakeCtxPreview")}
                <span className="aux">CTX PREVIEW</span>
              </div>
              <div className="ctx-list">
                {pinned.map((m) => (
                  <div key={m.uid} className="ctx-item">
                    <span className="pin">◉</span>
                    <span className="ctx-name" title={m.name}>
                      {m.name}
                    </span>
                    <span className="ctx-tok">
                      {m.text_excerpt ? `≈${Math.round(estimateTokens(m.text_excerpt))} tok` : tr("intakeRetrieval")}
                    </span>
                  </div>
                ))}
                {!pinned.length && <div className="empty-files">{tr("intakeNoPinned")}</div>}
              </div>
              <div className="ctx-total">
                <span>{tr("intakeEstimate")}</span>
                <b>{pinnedTokens ? `≈ ${pinnedTokens.toLocaleString()} tokens` : "0 tokens"}</b>
              </div>
              <p className="ctx-note">{tr("intakeCtxNote")}</p>
            </section>

            <div className="actions">
              <button type="button" className="btn" onClick={onCancel}>
                {tr("cancel")}
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
