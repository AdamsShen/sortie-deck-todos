import { useCallback, useEffect, useRef, useState } from "react";

import { createApiKey, deleteApiKey, listApiKeys } from "../api";
import type { I18nKey } from "../i18n";
import type { ApiKeyCreated, ApiKeyRecord, Lang, McpClientSnippet } from "../types";

/**
 * API 密钥整页（设计稿 v2「通行证」）。
 *
 * 一把 Key 就是一张有期限的通行证：主体放持证人信息，右侧票根只放到期日、
 * 剩余天数与「作废」。签发台带实时预览，选 1/3/6/12 个月时证件日期立刻变化。
 * 明文只在签发响应里出现一次，之后列表里永远只有前缀。
 */

const TTL_CHOICES = [1, 3, 6, 12] as const;
type TtlMonths = (typeof TTL_CHOICES)[number];

const DAY_MS = 86_400_000;

const pad = (n: number) => String(n).padStart(2, "0");

/** 秒级时间戳 → YYYY-MM-DD（本地时区） */
function fmtDate(ts: number | null): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 按日历月推进，月末对齐（1 月 31 日 + 1 个月 = 2 月最后一天） */
function addMonths(base: Date, months: number): Date {
  const d = new Date(base.getTime());
  const day = d.getDate();
  d.setMonth(d.getMonth() + months);
  if (d.getDate() < day) d.setDate(0);
  return d;
}

/** 剩余天数：null 表示长期有效；负数表示已过期 */
function daysLeft(expiresAt: number | null): number | null {
  if (!expiresAt) return null;
  const end = new Date(expiresAt * 1000);
  end.setHours(0, 0, 0, 0);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((end.getTime() - today.getTime()) / DAY_MS);
}

type KeyStatus = "active" | "warn" | "expired" | "revoked";

function statusOf(key: ApiKeyRecord): KeyStatus {
  // 历史数据可能有软吊销行：它未到期但已失效，不能与「时间到期」混为一谈
  if (key.revoked_at) return "revoked";
  const left = daysLeft(key.expires_at);
  if (left === null) return "active";
  if (left < 0) return "expired";
  if (left <= 7) return "warn";
  return "active";
}

export function ApiKeysPage({
  token,
  lang,
  tr,
}: {
  token: string;
  lang: Lang;
  tr: (key: I18nKey | string) => string;
}) {
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sheetOpen, setSheetOpen] = useState(false);
  const [name, setName] = useState("");
  const [ttl, setTtl] = useState<TtlMonths>(1);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [activeClient, setActiveClient] = useState("");
  const [claudeMode, setClaudeMode] = useState<"file" | "cmd">("file");
  const [copied, setCopied] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      setKeys(await listApiKeys(token));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 弹层打开时聚焦名称输入；Escape 关闭
  useEffect(() => {
    if (!sheetOpen) return;
    const timer = window.setTimeout(() => nameRef.current?.focus(), 30);
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") closeSheet();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
    // closeSheet 只依赖 state，不需要进依赖表
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sheetOpen]);

  function openSheet() {
    setCreated(null);
    setName("");
    setTtl(1);
    setActiveClient("");
    setClaudeMode("file");
    setError("");
    setSheetOpen(true);
  }

  function closeSheet() {
    setSheetOpen(false);
    setCreated(null);
    setName("");
    setError("");
  }

  async function issue() {
    setCreating(true);
    setError("");
    try {
      const body = await createApiKey(token, {
        name: name.trim() || tr("keysNoName"),
        expiresInMonths: ttl,
      });
      setCreated(body);
      setActiveClient(body.mcp.clients[0]?.id ?? "");
      setKeys((prev) => [body.key, ...prev]);
      setName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  async function remove(keyId: string) {
    try {
      await deleteApiKey(token, keyId);
      setKeys((prev) => prev.filter((k) => k.id !== keyId));
      setDeletingId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function copy(text: string, tag: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(tag);
      window.setTimeout(() => setCopied(""), 1500);
    } catch {
      setCopied("");
    }
  }

  const active = keys.filter((k) => statusOf(k) === "active");
  const soon = keys.filter((k) => statusOf(k) === "warn").length;
  // 已失效 = 到期 + 历史软吊销，两者都不再接受请求
  const inactive = keys.filter((k) => {
    const s = statusOf(k);
    return s === "expired" || s === "revoked";
  }).length;

  /** 剩余天数的中英文案（i18n 不支持下钻插值，这里按语言拼） */
  function leftText(status: KeyStatus, key: ApiKeyRecord): string {
    if (status === "revoked") return tr("keysStatusRevoked");
    const left = daysLeft(key.expires_at);
    if (left === null) return lang === "zh" ? "长期有效" : "No expiry";
    if (status === "expired") {
      return lang === "zh" ? `已过期 ${Math.abs(left)} 天` : `Expired ${Math.abs(left)}d ago`;
    }
    return lang === "zh" ? `剩 ${left} 天` : `${left} days left`;
  }

  function statusText(status: KeyStatus): string {
    if (status === "revoked") return tr("keysStatusRevoked");
    if (status === "expired") return tr("keysStatusExpired");
    if (status === "warn") return tr("keysStatusExpiring");
    return tr("keysStatusActive");
  }

  const previewDate = fmtDate(addMonths(new Date(), ttl).getTime() / 1000);
  const activeSnippet: McpClientSnippet | undefined =
    created?.mcp.clients.find((c) => c.id === activeClient) ?? created?.mcp.clients[0];
  const showCommand = Boolean(activeSnippet?.command) && claudeMode === "cmd";

  return (
    <section className="akeys">
      <header className="akeys-head">
        <div>
          <div className="akeys-kicker">{tr("keysKicker")}</div>
          <h1>{tr("apiKeys")}</h1>
          <p className="akeys-lede">{tr("keysLede")}</p>
        </div>
        <button type="button" className="akeys-primary" onClick={openSheet}>
          {tr("keysIssue")}
        </button>
      </header>

      <div className="akeys-readline">
        <span>
          <b>{active.length}</b> {tr("keysActiveShort")}
        </span>
        <i />
        <span>
          <b>{soon}</b> {tr("keysSoonShort")}
        </span>
        <i />
        <span>
          <b>{inactive}</b> {tr("keysInactiveShort")}
        </span>
      </div>

      {error && !sheetOpen && <p className="akeys-error">{error}</p>}

      <div className="akeys-passes">
        {keys.map((key) => {
          const status = statusOf(key);
          const md = key.expires_at
            ? fmtDate(key.expires_at).slice(5).replace("-", ".")
            : "--.--";
          const year = key.expires_at ? fmtDate(key.expires_at).slice(0, 4) : "----";
          return (
            <article
              key={key.id}
              className={`akeys-pass${
                status === "expired" || status === "revoked"
                  ? " dead"
                  : status === "warn"
                    ? " warn"
                    : ""
              }`}
            >
              <div className="akeys-tear" aria-hidden>
                <span>SORTIE DECK · AGENT PASS</span>
              </div>
              <div className="akeys-pass-body">
                <div className="akeys-toprow">
                  <span>AGENT PASS</span>
                  <span className="akeys-chip">{key.id}</span>
                  <span className="akeys-chip">{statusText(status)}</span>
                </div>
                <h2>{key.name}</h2>
                <div className="akeys-serial">{key.prefix}…</div>
                <div className="akeys-facts">
                  <div>
                    <span className="k">{tr("keysIssuedAt")}</span>
                    <span className="v mono">{fmtDate(key.created_at)}</span>
                  </div>
                  <div>
                    <span className="k">{tr("keysLastUsed")}</span>
                    <span className="v">
                      {key.last_used_at ? fmtDate(key.last_used_at) : tr("apiKeyNeverUsed")}
                    </span>
                  </div>
                  <div>
                    <span className="k">{tr("keysScope")}</span>
                    <span className="v">{tr("keysScopeValue")}</span>
                  </div>
                </div>
              </div>
              <div className="akeys-perf" aria-hidden />
              <div className="akeys-stub">
                <div className="k">{tr("keysExpiresOn")}</div>
                <div className="date">
                  {key.expires_at ? (
                    <>
                      {md}
                      <small> / {year}</small>
                    </>
                  ) : (
                    <span className="never">{tr("keysNoExpiry")}</span>
                  )}
                </div>
                <div
                  className={`left${
                    status === "expired" || status === "revoked"
                      ? " dead"
                      : status === "warn"
                        ? " warn"
                        : ""
                  }`}
                >
                  {leftText(status, key)}
                </div>
                <div className="sp" />
                <button
                  type="button"
                  className="akeys-revoke"
                  onClick={() => setDeletingId(key.id)}
                >
                  {tr("keysRevoke")}
                </button>
              </div>
              {(status === "expired" || status === "revoked") && (
                <span className="akeys-watermark" aria-hidden>
                  {statusText(status)}
                </span>
              )}
              {deletingId === key.id && (
                <div className="akeys-confirm">
                  <p>{tr("keysRevokeWarning")}</p>
                  <button type="button" className="no" onClick={() => setDeletingId(null)}>
                    {tr("keysKeep")}
                  </button>
                  <button type="button" className="yes" onClick={() => void remove(key.id)}>
                    {tr("keysConfirmRevoke")}
                  </button>
                </div>
              )}
            </article>
          );
        })}
      </div>

      {!loading && keys.length === 0 && (
        <div className="akeys-empty">
          <b>{tr("keysEmptyTitle")}</b>
          <p>{tr("keysEmptyBody")}</p>
          <button type="button" className="akeys-primary" onClick={openSheet}>
            {tr("keysIssueFirst")}
          </button>
        </div>
      )}

      {sheetOpen && (
        <div
          className="akeys-overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closeSheet();
          }}
        >
          <div className="akeys-sheet" role="dialog" aria-modal="true" aria-labelledby="akeys-sheet-title">
            {created ? (
              <>
                <div className="akeys-sheet-bar">
                  <h2 id="akeys-sheet-title">
                    {tr("keysIssued")} · {created.key.name}
                  </h2>
                  <button type="button" className="x" onClick={closeSheet} aria-label={tr("apiKeyClose")}>
                    ×
                  </button>
                </div>

                <div className="akeys-keycard">
                  <div className="tag">{tr("keysOnlyOnce")}</div>
                  <div className="line">
                    <code>{created.plaintext}</code>
                    <button
                      type="button"
                      className={`akeys-copy${copied === "plain" ? " done" : ""}`}
                      onClick={() => void copy(created.plaintext, "plain")}
                    >
                      {copied === "plain" ? tr("apiKeyCopied") : tr("apiKeyCopy")}
                    </button>
                  </div>
                </div>

                <div className="akeys-tabs" role="tablist">
                  {created.mcp.clients.map((client) => (
                    <button
                      key={client.id}
                      type="button"
                      role="tab"
                      aria-selected={activeSnippet?.id === client.id}
                      className={activeSnippet?.id === client.id ? "on" : ""}
                      onClick={() => {
                        setActiveClient(client.id);
                        setClaudeMode("file");
                      }}
                    >
                      {client.label}
                    </button>
                  ))}
                </div>

                {activeSnippet && (
                  <div className="akeys-snippet">
                    <div className="akeys-target">
                      <span>
                        {tr("keysTargetPrefix")} <b>{activeSnippet.target}</b>
                      </span>
                      <button
                        type="button"
                        className={`akeys-copy${copied === "code" ? " done" : ""}`}
                        onClick={() =>
                          void copy(
                            showCommand && activeSnippet.command
                              ? activeSnippet.command
                              : activeSnippet.code,
                            "code",
                          )
                        }
                      >
                        {copied === "code" ? tr("apiKeyCopied") : tr("keysCopyConfig")}
                      </button>
                    </div>
                    {activeSnippet.command && (
                      <div className="akeys-subtoggle">
                        <button
                          type="button"
                          className={claudeMode === "file" ? "on" : ""}
                          onClick={() => setClaudeMode("file")}
                        >
                          {tr("keysConfigFile")}
                        </button>
                        <button
                          type="button"
                          className={claudeMode === "cmd" ? "on" : ""}
                          onClick={() => setClaudeMode("cmd")}
                        >
                          {tr("keysCommand")}
                        </button>
                      </div>
                    )}
                    {showCommand && activeSnippet.command ? (
                      <pre className="akeys-code akeys-cmd">{activeSnippet.command}</pre>
                    ) : (
                      <pre className="akeys-code">{activeSnippet.code}</pre>
                    )}
                  </div>
                )}

                <div className="akeys-done">
                  <button type="button" className="akeys-primary" onClick={closeSheet}>
                    {tr("keysDone")}
                  </button>
                  <span>{tr("keysDoneHint")}</span>
                </div>
              </>
            ) : (
              <>
                <div className="akeys-sheet-bar">
                  <h2 id="akeys-sheet-title">{tr("keysSheetTitle")}</h2>
                  <button type="button" className="x" onClick={closeSheet} aria-label={tr("apiKeyClose")}>
                    ×
                  </button>
                </div>

                <div className="akeys-issue-grid">
                  <div className="akeys-issue-form">
                    <label htmlFor="akeys-name">{tr("keysHolder")}</label>
                    <input
                      id="akeys-name"
                      ref={nameRef}
                      value={name}
                      maxLength={40}
                      autoComplete="off"
                      placeholder={tr("apiKeyNamePlaceholder")}
                      onChange={(e) => setName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !creating) void issue();
                      }}
                    />

                    <div className="akeys-field-label">{tr("keysTtl")}</div>
                    <div className="akeys-ttls">
                      {TTL_CHOICES.map((months) => (
                        <button
                          key={months}
                          type="button"
                          className={ttl === months ? "on" : ""}
                          onClick={() => setTtl(months)}
                        >
                          <b>{tr(`keysTtl${months}`)}</b>
                          <span>{tr(`keysTtlHint${months}`)}</span>
                        </button>
                      ))}
                    </div>
                  </div>

                  <aside className="akeys-preview">
                    <div className="cap">PREVIEW</div>
                    <div className="akeys-preview-card">
                      <div className="band">
                        <span>SORTIE DECK</span>
                        <span>AGENT PASS</span>
                      </div>
                      <div className="pc-body">
                        <div className={`pc-name${name.trim() ? "" : " empty"}`}>
                          {name.trim() || tr("keysPreviewEmpty")}
                        </div>
                        <div className="pc-serial">sk_sortie_••••••••••••</div>
                        <div className="pc-foot">
                          <div>
                            <div className="k">{tr("keysExpiresOn")}</div>
                            <div className="d">{previewDate}</div>
                          </div>
                          <div style={{ textAlign: "right" }}>
                            <div className="k">STATUS</div>
                            <div className="d" style={{ fontSize: 12 }}>{tr("keysPending")}</div>
                          </div>
                        </div>
                      </div>
                    </div>
                    <p className="tip">{tr("keysPreviewTip")}</p>
                  </aside>
                </div>

                {error && <p className="akeys-error in-sheet">{error}</p>}

                <div className="akeys-sheet-foot">
                  <span className="hint">{tr("keysSigner")}</span>
                  <span className="sp" />
                  <button type="button" className="cancel" onClick={closeSheet}>
                    {tr("cancel")}
                  </button>
                  <button type="button" className="akeys-primary" disabled={creating} onClick={() => void issue()}>
                    {tr("keysIssue")}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
