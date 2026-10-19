import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

export type ComposerOption = {
  id: string;
  label: string;
  hint?: string;
};

export type MentionOption = ComposerOption;

export type ComposerAttachment = {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: "image" | "file";
  /** data: URL for images / binary; omitted for pure-text excerpts */
  dataUrl?: string;
  /** Decoded text for text/* / markdown / json */
  textExcerpt?: string;
};

export type ComposerPayload = {
  text: string;
  attachments: ComposerAttachment[];
};

type Props = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (e?: FormEvent | KeyboardEvent, payload?: ComposerPayload) => void;
  placeholder: string;
  hint: string;
  sendLabel: string;
  disabled?: boolean;
  mentions: ComposerOption[];
  commands?: ComposerOption[];
  /** Enable image / file pick, paste, and drag-drop */
  attachmentsEnabled?: boolean;
  attachLabel?: string;
  accept?: string;
  maxAttachments?: number;
  maxBytes?: number;
  /** When true the send button becomes a stop button invoking onStop. */
  stopping?: boolean;
  stopLabel?: string;
  onStop?: () => void;
  /** 「增强提示词」：传入则渲染 ✨ 按钮，父组件调 LLM 优化并返回新文本（null 表示失败/放弃） */
  onEnhance?: (text: string) => Promise<string | null>;
  enhanceLabel?: string;
  enhanceUndoLabel?: string;
  /** 优化进行中浮层文案（如「增强中…」） */
  enhanceLoadingLabel?: string;
};

type Trigger = {
  kind: "mention" | "command";
  start: number;
  query: string;
};

const DEFAULT_ACCEPT =
  "image/*,.md,.txt,.json,.csv,.yaml,.yml,.log,.pdf,text/plain,text/markdown,application/json";

const TEXT_MIME = /^(text\/|application\/(json|yaml|x-yaml|xml))/i;
const TEXT_EXT = /\.(md|txt|json|csv|ya?ml|log|xml)$/i;

function activeTrigger(value: string, caret: number): Trigger | null {
  const before = value.slice(0, caret);
  const at = before.match(/(^|[\s])@([\w.-]*)$/);
  if (at) {
    return { kind: "mention", start: before.lastIndexOf("@"), query: at[2] ?? "" };
  }
  const slash = before.match(/(^|[\s])\/([\w.-]*)$/);
  if (slash) {
    return { kind: "command", start: before.lastIndexOf("/"), query: slash[2] ?? "" };
  }
  return null;
}

function filterOptions(options: ComposerOption[], query: string): ComposerOption[] {
  const q = query.toLowerCase();
  return options.filter(
    (m) =>
      !q ||
      m.id.toLowerCase().includes(q) ||
      m.label.toLowerCase().includes(q) ||
      (m.hint || "").toLowerCase().includes(q),
  );
}

function uid() {
  return `att_${Math.random().toString(36).slice(2, 10)}`;
}

function isTextLike(file: File): boolean {
  return TEXT_MIME.test(file.type) || TEXT_EXT.test(file.name);
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsText(file);
  });
}

async function fileToAttachment(file: File): Promise<ComposerAttachment> {
  const kind: "image" | "file" = file.type.startsWith("image/") ? "image" : "file";
  const base: ComposerAttachment = {
    id: uid(),
    name: file.name || (kind === "image" ? "paste.png" : "file"),
    mime: file.type || (kind === "image" ? "image/png" : "application/octet-stream"),
    size: file.size,
    kind,
  };
  if (kind === "image" || !isTextLike(file)) {
    base.dataUrl = await readAsDataUrl(file);
  }
  if (isTextLike(file)) {
    const text = await readAsText(file);
    base.textExcerpt = text.slice(0, 24_000);
    if (!base.dataUrl) {
      base.dataUrl = await readAsDataUrl(file);
    }
  }
  return base;
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** ✨ 线条风 sparkle 图标（增强提示词按钮）：四角星 + 右上加号 + 左下圆点 */
export function SparkleIcon() {
  return (
    <svg
      className="sparkle-icon"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {/* 大四角星（凹边） */}
      <path d="M10.5 4c.7 3.9 2.5 5.7 6.4 6.4-3.9.7-5.7 2.5-6.4 6.4-.7-3.9-2.5-5.7-6.4-6.4 3.9-.7 5.7-2.5 6.4-6.4z" />
      {/* 右上加号 */}
      <path d="M18.2 3.2v4.4M16 5.4h4.4" />
      {/* 左下小点 */}
      <path d="M5 18.6h.01" />
    </svg>
  );
}

export function ChatComposer({
  value,
  onChange,
  onSubmit,
  placeholder,
  hint,
  sendLabel,
  disabled,
  mentions,
  commands = [],
  attachmentsEnabled = false,
  attachLabel = "附件",
  accept = DEFAULT_ACCEPT,
  maxAttachments = 6,
  maxBytes = 4 * 1024 * 1024,
  stopping = false,
  stopLabel = "停止",
  onStop,
  onEnhance,
  enhanceLabel = "增强提示词",
  enhanceUndoLabel = "撤销优化",
  enhanceLoadingLabel = "增强中…",
}: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [caret, setCaret] = useState(0);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  // 增强提示词：优化中 spinner；成功覆盖后保留原文供一键撤销
  const [enhancing, setEnhancing] = useState(false);
  const [enhancePrev, setEnhancePrev] = useState<string | null>(null);

  const trigger = useMemo(() => activeTrigger(value, caret), [value, caret]);
  const filtered = useMemo(() => {
    if (!trigger) return [];
    const source = trigger.kind === "mention" ? mentions : commands;
    return filterOptions(source, trigger.query);
  }, [trigger, mentions, commands]);

  useEffect(() => {
    setOpen(Boolean(trigger && filtered.length));
    setActive(0);
  }, [trigger, filtered.length]);

  function syncCaret() {
    const el = taRef.current;
    if (el) setCaret(el.selectionStart ?? value.length);
  }

  function insertOption(opt: ComposerOption) {
    if (!trigger) return;
    const el = taRef.current;
    const prefix = trigger.kind === "mention" ? "@" : "/";
    const needsArgSpace = trigger.kind === "command" && opt.id === "rewrite";
    const suffix = needsArgSpace ? " " : " ";
    const next = `${value.slice(0, trigger.start)}${prefix}${opt.id}${suffix}${value.slice(caret)}`;
    onChange(next);
    setOpen(false);
    requestAnimationFrame(() => {
      if (!el) return;
      const pos = trigger.start + prefix.length + opt.id.length + suffix.length;
      el.focus();
      el.setSelectionRange(pos, pos);
      setCaret(pos);
    });
  }

  async function addFiles(files: FileList | File[] | null) {
    if (!attachmentsEnabled || !files || disabled) return;
    const list = Array.from(files);
    if (!list.length) return;
    setAttachError(null);
    const next = [...attachments];
    for (const file of list) {
      if (next.length >= maxAttachments) {
        setAttachError(`最多 ${maxAttachments} 个附件`);
        break;
      }
      if (file.size > maxBytes) {
        setAttachError(`${file.name} 超过 ${formatSize(maxBytes)}`);
        continue;
      }
      try {
        next.push(await fileToAttachment(file));
      } catch {
        setAttachError(`${file.name} 读取失败`);
      }
    }
    setAttachments(next);
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  // 点击「增强提示词」：父组件调 LLM 优化，返回的文本直接覆盖输入框
  async function doEnhance() {
    if (!onEnhance || disabled || enhancing || !value.trim()) return;
    const original = value;
    setEnhancing(true);
    try {
      const next = await onEnhance(original);
      if (next && next !== original) {
        setEnhancePrev(original);
        onChange(next);
      }
    } finally {
      setEnhancing(false);
    }
  }

  function undoEnhance() {
    if (enhancePrev === null) return;
    onChange(enhancePrev);
    setEnhancePrev(null);
  }

  function submit(e?: FormEvent | KeyboardEvent) {
    e?.preventDefault?.();
    if (disabled) return;
    if (!value.trim() && !attachments.length) return;
    const payload: ComposerPayload = { text: value, attachments: [...attachments] };
    onSubmit(e, payload);
    setAttachments([]);
    setAttachError(null);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (open && filtered.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => (i + 1) % filtered.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i - 1 + filtered.length) % filtered.length);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        insertOption(filtered[active] ?? filtered[0]);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit(e);
    }
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    if (!attachmentsEnabled || disabled) return;
    const items = e.clipboardData?.items;
    if (!items?.length) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      void addFiles(files);
    }
  }

  function onDragOver(e: DragEvent) {
    if (!attachmentsEnabled || disabled) return;
    e.preventDefault();
    e.stopPropagation();
    setDragging(true);
  }

  function onDragLeave(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
  }

  function onDrop(e: DragEvent) {
    if (!attachmentsEnabled || disabled) return;
    e.preventDefault();
    e.stopPropagation();
    setDragging(false);
    void addFiles(e.dataTransfer?.files || null);
  }

  const marker = trigger?.kind === "command" ? "/" : "@";
  const canSend = Boolean(value.trim() || attachments.length);

  return (
    <form
      className={`chat-compose${attachmentsEnabled ? " multimodal" : ""}${dragging ? " dragging" : ""}`}
      onSubmit={(e) => submit(e)}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <p className="chat-cmd-hint">{hint}</p>
      {attachmentsEnabled && attachments.length > 0 ? (
        <ul className="composer-attachments" aria-label="attachments">
          {attachments.map((a) => (
            <li key={a.id} className={`composer-att kind-${a.kind}`}>
              {a.kind === "image" && a.dataUrl ? (
                <img src={a.dataUrl} alt={a.name} />
              ) : (
                <span className="composer-att-icon" aria-hidden>
                  ⌗
                </span>
              )}
              <div className="composer-att-meta">
                <strong title={a.name}>{a.name}</strong>
                <em>{formatSize(a.size)}</em>
              </div>
              <button
                type="button"
                className="composer-att-remove"
                disabled={disabled}
                onClick={() => removeAttachment(a.id)}
                aria-label={`remove ${a.name}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {attachError ? <p className="composer-att-error">{attachError}</p> : null}
      <div className="chat-compose-field">
        {open ? (
          <ul className="mention-menu" role="listbox" data-kind={trigger?.kind}>
            {filtered.map((m, i) => (
              <li key={`${trigger?.kind}-${m.id}`}>
                <button
                  type="button"
                  className={i === active ? "active" : undefined}
                  onMouseDown={(ev) => {
                    ev.preventDefault();
                    insertOption(m);
                  }}
                >
                  <strong>
                    {marker}
                    {m.id}
                  </strong>
                  <span>{m.label}</span>
                  {m.hint ? <em>{m.hint}</em> : null}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <textarea
          ref={taRef}
          placeholder={placeholder}
          value={value}
          disabled={disabled}
          onChange={(e) => {
            onChange(e.target.value);
            setCaret(e.target.selectionStart);
          }}
          onClick={syncCaret}
          onKeyUp={syncCaret}
          onSelect={syncCaret}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        {/* 增强中浮层：覆盖在输入框右下角，提示 LLM 优化进行中 */}
        {enhancing ? (
          <span className="composer-enhance-toast" role="status">
            <span className="mini-spinner" aria-hidden />
            {enhanceLoadingLabel}
          </span>
        ) : null}
        {dragging ? (
          <div className="composer-drop-mask" aria-hidden>
            放下以添加附件
          </div>
        ) : null}
      </div>
      <div className="chat-compose-actions">
        {attachmentsEnabled ? (
          <>
            <input
              ref={fileRef}
              type="file"
              accept={accept}
              multiple
              hidden
              onChange={(e) => {
                void addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              className="btn ghost composer-attach-btn"
              disabled={disabled || attachments.length >= maxAttachments}
              onClick={() => fileRef.current?.click()}
              title={attachLabel}
            >
              {attachLabel}
            </button>
          </>
        ) : null}
        {onEnhance ? (
          enhancePrev !== null && !enhancing ? (
            // 优化成功后短暂提供撤销，恢复覆盖前的原文
            <button
              type="button"
              className="btn ghost composer-enhance-btn composer-enhance-icon"
              onClick={undoEnhance}
              title={enhanceUndoLabel}
              aria-label={enhanceUndoLabel}
            >
              ↺
            </button>
          ) : (
            <button
              type="button"
              className="btn ghost composer-enhance-btn composer-enhance-icon"
              disabled={disabled || !value.trim() || enhancing}
              onClick={() => void doEnhance()}
              title={enhanceLabel}
              aria-label={enhanceLabel}
            >
              {enhancing ? <span className="mini-spinner" aria-hidden /> : <SparkleIcon />}
            </button>
          )
        ) : null}
        {stopping && onStop ? (
          <button className="btn primary composer-stop" type="button" onClick={onStop}>
            {stopLabel}
          </button>
        ) : (
          <button className="btn primary" type="submit" disabled={disabled || !canSend}>
            {sendLabel}
          </button>
        )}
      </div>
    </form>
  );
}
