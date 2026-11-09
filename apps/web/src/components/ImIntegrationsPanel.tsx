import { useCallback, useEffect, useRef, useState } from "react";

import {
  createImConnection,
  createImLinkCode,
  deleteImConnection,
  deleteImIdentity,
  listImConnections,
  listImIdentities,
  listImProviders,
  pollWechatQr,
  probeImConnection,
  startWechatQr,
  updateImConnection,
} from "../api";
import type {
  ImConnection,
  ImIdentity,
  ImLinkCode,
  ImMode,
  ImProvider,
  ImQrSession,
  Lang,
} from "../types";

/**
 * IM 集成管理页（设置 → 接入 → IM 集成）。
 *
 * 形态对齐 CodeBuddy 的「Claw 设置 → 集成」：每个渠道一张卡片，微信扫码、
 * 飞书/钉钉/企微填凭据并选择「长连接 / URL 回调」，接入后显示连接状态。
 *
 * 两条必须让用户看见的信息：
 * 1. **长连接与回调是平台侧互斥配置**——本地选了 stream，平台后台也得改，
 *    否则会"连上了但收不到事件"（用 events_seen_at 提示）。
 * 2. **凭据只写不读**——接口只回 fingerprint，页面不提供"查看密钥"。
 */

const ZH = {
  title: "IM 集成",
  lede: "把任务门禁与结果通知接到企业 IM。工作区级绑定：每个渠道一条连接。",
  refresh: "刷新",
  loading: "加载中…",
  configure: "配置",
  reconfigure: "重新配置",
  probe: "探活",
  probing: "探活中…",
  remove: "删除",
  cancel: "取消",
  submit: "注册",
  submitting: "注册中…",
  guide: "配置指南",
  statusUnconfigured: "未配置",
  statusConnecting: "连接中",
  statusConnected: "已连接",
  statusError: "失败",
  statusExpired: "已过期",
  statusDisabled: "已禁用",
  beta: "BETA",
  recommended: "推荐",
  modeStream: "长连接",
  modeWebhook: "URL 回调",
  modeQr: "微信扫码",
  appIdLabel: "App ID",
  appSecretLabel: "App Secret",
  botIdLabel: "Bot ID",
  botSecretLabel: "Secret",
  appKeyLabel: "App Key",
  botTokenLabel: "Bot Token",
  signingSecretLabel: "Signing Secret",
  wechatScan: "打开微信扫描此二维码",
  wechatExpire: "秒后过期",
  wechatScaned: "已扫码，请在手机上确认",
  wechatNeedVerify: "该账号需要验证码，请在微信内完成验证后重新扫码",
  wechatExpired: "二维码已过期，请重新获取",
  wechatRefresh: "重新获取",
  wechatConfirmed: "已连接微信",
  copyLink: "图片打不开？复制链接",
  identityTitle: "身份绑定",
  identityLede:
    "把你的 IM 账号绑定到当前工作台账号。绑定后可以直接在 IM 里建任务、审批门禁、查询任务，不再需要在设置页绑定具体任务。",
  generateCode: "生成绑定码",
  generating: "生成中…",
  identityTip: "把上面这条命令发到 IM 里（{ttl} 分钟内有效，只能用一次）",
  identityEmpty: "还没有绑定任何 IM 账号。",
  identityUser: "工作台账号",
  copy: "复制",
  copied: "已复制",
  bindingTitle: "会话绑定",
  bindingLede: "把一个 IM 会话绑定到任务后，群里的消息与审批才会落到该任务上。",
  bindingProvider: "渠道",
  bindingChannel: "会话 ID",
  bindingMission: "任务 ID",
  bindingSubmit: "绑定",
  bindingEmpty: "还没有绑定任何会话。",
  bindingChannelHint: "群聊用 conversationId / chat_id；微信单聊填对方 userid。",
  modeHintStream:
    "请到平台后台把订阅方式同步改为「使用长连接」，否则会连上但收不到事件。",
  modeHintWebhook: "需要公网可达的回调地址，且平台后台要选「URL 回调」。",
  deleteConfirm: "删除后该渠道立即断开，已发送的卡片按钮会失效。确认删除？",
  noSecret: "未保存密钥",
  lastError: "最近错误",
  neverSeen: "尚未收到事件",
  seenAt: "最近事件",
};

const EN: typeof ZH = {
  title: "IM integrations",
  lede: "Route gate approvals and mission updates into your IM. Workspace-scoped: one connection per channel.",
  refresh: "Refresh",
  loading: "Loading…",
  configure: "Configure",
  reconfigure: "Reconfigure",
  probe: "Test",
  probing: "Testing…",
  remove: "Remove",
  cancel: "Cancel",
  submit: "Register",
  submitting: "Registering…",
  guide: "Setup guide",
  statusUnconfigured: "Not configured",
  statusConnecting: "Connecting",
  statusConnected: "Connected",
  statusError: "Failed",
  statusExpired: "Expired",
  statusDisabled: "Disabled",
  beta: "BETA",
  recommended: "Recommended",
  modeStream: "Long connection",
  modeWebhook: "URL callback",
  modeQr: "Scan QR code",
  appIdLabel: "App ID",
  appSecretLabel: "App Secret",
  botIdLabel: "Bot ID",
  botSecretLabel: "Secret",
  appKeyLabel: "App Key",
  botTokenLabel: "Bot Token",
  signingSecretLabel: "Signing Secret",
  wechatScan: "Scan this QR code with WeChat",
  wechatExpire: "s until expiry",
  wechatScaned: "Scanned — confirm on your phone",
  wechatNeedVerify: "This account needs a verification code; finish it in WeChat and scan again",
  wechatExpired: "QR code expired, please request a new one",
  wechatRefresh: "Get a new code",
  wechatConfirmed: "WeChat connected",
  copyLink: "Image not loading? Copy the link",
  identityTitle: "Identity binding",
  identityLede:
    "Bind your IM account to this workspace account. After that you can create missions, approve gates and list missions directly from IM — no per-mission chat binding needed.",
  generateCode: "Generate code",
  generating: "Generating…",
  identityTip: "Send this command in IM (valid {ttl} minutes, single use)",
  identityEmpty: "No IM account bound yet.",
  identityUser: "Workspace account",
  copy: "Copy",
  copied: "Copied",
  bindingTitle: "Chat binding",
  bindingLede: "Bind an IM chat to a mission so messages and approvals land on that mission.",
  bindingProvider: "Channel",
  bindingChannel: "Chat ID",
  bindingMission: "Mission ID",
  bindingSubmit: "Bind",
  bindingEmpty: "No chat bound yet.",
  bindingChannelHint: "Group chats use conversationId / chat_id; WeChat DM uses the peer userid.",
  modeHintStream:
    "Also switch the platform console to long connection, otherwise it connects but receives no events.",
  modeHintWebhook: "Needs a publicly reachable callback URL, and the console must use URL callback.",
  deleteConfirm: "Deleting disconnects this channel immediately and invalidates card buttons. Continue?",
  noSecret: "No secret stored",
  lastError: "Last error",
  neverSeen: "No events received yet",
  seenAt: "Last event",
};

interface Props {
  token: string;
  lang: Lang;
}

const STATUS_CLASS: Record<string, string> = {
  connected: "ok",
  connecting: "wait",
  error: "bad",
  expired: "bad",
  disabled: "mute",
  unconfigured: "mute",
};

function statusText(C: typeof ZH, status: string): string {
  const map: Record<string, string> = {
    unconfigured: C.statusUnconfigured,
    connecting: C.statusConnecting,
    connected: C.statusConnected,
    error: C.statusError,
    expired: C.statusExpired,
    disabled: C.statusDisabled,
  };
  return map[status] || status;
}

export function ImIntegrationsPanel({ token, lang }: Props) {
  const C = lang === "zh" ? ZH : EN;
  const [providers, setProviders] = useState<ImProvider[]>([]);
  const [connections, setConnections] = useState<ImConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [active, setActive] = useState<ImProvider | null>(null);
  const [busyId, setBusyId] = useState("");

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextProviders, nextConnections] = await Promise.all([
        listImProviders(token),
        listImConnections(token),
      ]);
      setProviders(nextProviders);
      setConnections(nextConnections);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const byProvider = new Map(connections.map((c) => [c.provider, c]));

  async function onProbe(conn: ImConnection) {
    setBusyId(conn.id);
    try {
      const updated = await probeImConnection(token, conn.id);
      setConnections((rows) => rows.map((r) => (r.id === updated.id ? updated : r)));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId("");
    }
  }

  async function onDelete(conn: ImConnection) {
    if (!window.confirm(C.deleteConfirm)) return;
    setBusyId(conn.id);
    try {
      await deleteImConnection(token, conn.id);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId("");
    }
  }

  if (loading) return <div className="im-empty">{C.loading}</div>;

  return (
    <div className="im">
      <header className="stg-head">
        <div>
          <h2>{C.title}</h2>
          <p>{C.lede}</p>
        </div>
        <div className="stg-head-actions">
          <button type="button" className="stg-quiet" onClick={() => void reload()}>
            {C.refresh}
          </button>
        </div>
      </header>

      {error && <p className="im-error">{error}</p>}

      <div className="im-list">
        {providers.map((provider) => {
          const conn = byProvider.get(provider.provider);
          const status = conn?.status ?? "unconfigured";
          return (
            <article className="im-card" key={provider.provider}>
              <div className="im-card-main">
                <div className="im-card-title">
                  <b>{provider.name}</b>
                  {provider.recommended && <em className="im-flag">{C.recommended}</em>}
                  {provider.beta && <em className="im-flag beta">{C.beta}</em>}
                  <span className={`im-badge ${STATUS_CLASS[status] || "mute"}`}>
                    {statusText(C, status)}
                  </span>
                </div>
                <p className="im-card-hint">{provider.hint}</p>
                {conn?.last_error && (
                  <p className="im-card-err">
                    {C.lastError}: {conn.last_error}
                  </p>
                )}
                {conn && (
                  <p className="im-card-meta">
                    {conn.mode === "qr" ? C.modeQr : conn.mode === "stream" ? C.modeStream : C.modeWebhook}
                    {" · "}
                    {conn.app_id || "—"}
                    {" · "}
                    {conn.has_secret ? `fp ${conn.secret_fingerprint.slice(0, 8)}` : C.noSecret}
                    {" · "}
                    {conn.events_seen_at
                      ? `${C.seenAt} ${new Date(conn.events_seen_at * 1000).toLocaleString()}`
                      : C.neverSeen}
                  </p>
                )}
              </div>
              <div className="im-card-actions">
                {conn && conn.status !== "unconfigured" && (
                  <button
                    type="button"
                    className="stg-quiet"
                    disabled={busyId === conn.id}
                    onClick={() => void onProbe(conn)}
                  >
                    {busyId === conn.id ? C.probing : C.probe}
                  </button>
                )}
                {conn && (
                  <button
                    type="button"
                    className="stg-quiet danger"
                    disabled={busyId === conn.id}
                    onClick={() => void onDelete(conn)}
                  >
                    {C.remove}
                  </button>
                )}
                <button type="button" className="stg-quiet" onClick={() => setActive(provider)}>
                  {conn ? C.reconfigure : C.configure}
                </button>
              </div>
            </article>
          );
        })}
      </div>

      <IdentityBinding token={token} C={C} />

      {active && (
        <ConnectDialog
          token={token}
          provider={active}
          conn={byProvider.get(active.provider)}
          C={C}
          lang={lang}
          onClose={() => setActive(null)}
          onDone={async () => {
            setActive(null);
            await reload();
          }}
        />
      )}
    </div>
  );
}

// --- 配置弹窗 ---------------------------------------------------------------

interface DialogProps {
  token: string;
  provider: ImProvider;
  conn?: ImConnection;
  C: typeof ZH;
  lang: Lang;
  onClose: () => void;
  onDone: () => Promise<void> | void;
}

function ConnectDialog({ token, provider, conn, C, lang, onClose, onDone }: DialogProps) {
  const [mode, setMode] = useState<ImMode>(conn?.mode ?? provider.modes[0]);
  const [appId, setAppId] = useState(conn?.app_id ?? "");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [qr, setQr] = useState<ImQrSession | null>(null);
  const [left, setLeft] = useState(0);
  const timerRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);

  const isQr = mode === "qr";

  const stopTimers = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopTimers, [stopTimers]);

  const requestQr = useCallback(async () => {
    setError("");
    setBusy(true);
    try {
      const session = await startWechatQr(token);
      setQr(session);
      setLeft(session.expires_in);
      stopTimers();
      // 倒计时：纯展示，不决定状态（状态以后端轮询结果为准）
      timerRef.current = window.setInterval(() => {
        setLeft((v) => (v > 0 ? v - 1 : 0));
      }, 1000);
      // 轮询：扫码是异步的，2s 一次足够且不压服务端
      pollRef.current = window.setInterval(async () => {
        try {
          const next = await pollWechatQr(token, session.ticket);
          setQr(next);
          if (next.status === "confirmed") {
            stopTimers();
            await onDone();
          } else if (next.status === "expired" || next.status === "error") {
            stopTimers();
          }
        } catch {
          stopTimers();
        }
      }, 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [token, stopTimers, onDone]);

  useEffect(() => {
    if (isQr && !conn?.has_secret) void requestQr();
    // 仅在打开即扫码时自动申请一次；重新获取走按钮
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isQr]);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      if (conn) {
        // 已有记录：换 mode / 换凭据走 PATCH（换 mode 会把状态打回未配置）
        await updateImConnection(token, conn.id, {
          mode,
          app_id: appId.trim(),
          secret: secret.trim() || undefined,
        });
      } else {
        await createImConnection(token, {
          provider: provider.provider,
          mode,
          app_id: appId.trim(),
          secret: secret.trim(),
        });
      }
      await onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const fieldLabels: Record<string, string> = {
    app_id: C.appIdLabel,
    app_secret: C.appSecretLabel,
    bot_id: C.botIdLabel,
    secret: C.botSecretLabel,
    app_key: C.appKeyLabel,
    bot_token: C.botTokenLabel,
    signing_secret: C.signingSecretLabel,
  };

  return (
    <div className="im-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="im-dialog" role="dialog" aria-modal="true">
        <header>
          <div className="im-dialog-head">
            <h3>{lang === "zh" ? `注册${provider.name}通道` : `Register ${provider.name}`}</h3>
            {provider.lede && <p className="im-lede">{provider.lede}</p>}
            {provider.guide_url && (
              <a className="im-guide" href={provider.guide_url} target="_blank" rel="noreferrer">
                {C.guide}
              </a>
            )}
          </div>
          <button type="button" className="im-x" onClick={onClose} aria-label={C.cancel}>
            ×
          </button>
        </header>

        {isQr ? (
          <div className="im-qr-wrap">
            <p className="im-qr-title">{C.wechatScan}</p>
            {qr?.status === "confirmed" ? (
              <p className="im-qr-ok">{C.wechatConfirmed}</p>
            ) : qr?.qr_image ? (
              <img className="im-qr" src={qr.qr_image} alt="WeChat QR" />
            ) : qr?.qr_content ? (
              <div className="im-qr placeholder">
                <a href={qr.qr_content} target="_blank" rel="noreferrer">
                  {lang === "zh" ? "打开二维码链接" : "Open QR link"}
                </a>
              </div>
            ) : (
              <div className="im-qr placeholder">{busy ? "…" : "—"}</div>
            )}
            {qr && qr.status !== "confirmed" && (
              <p className="im-qr-tip">
                {qr.status === "scaned"
                  ? C.wechatScaned
                  : qr.status === "need_verifycode"
                    ? C.wechatNeedVerify
                    : qr.status === "expired"
                      ? C.wechatExpired
                      : `${left} ${C.wechatExpire}`}
              </p>
            )}
            {qr?.qr_content && qr.status !== "confirmed" && (
              <p className="im-qr-fallback">
                <a href={qr.qr_content} target="_blank" rel="noreferrer">
                  {C.copyLink}
                </a>
              </p>
            )}
            {(qr?.status === "expired" || qr?.status === "error" || !qr) && (
              <button type="button" className="stg-quiet" disabled={busy} onClick={() => void requestQr()}>
                {C.wechatRefresh}
              </button>
            )}
            <p className="im-qr-note">
              {lang === "zh"
                ? "微信个人号仅支持单聊、没有卡片；审批按钮请用飞书/钉钉/企微。"
                : "WeChat DM only, no cards. Use Feishu/DingTalk/WeCom for approval buttons."}
            </p>
          </div>
        ) : (
          <>
            <div className="im-mode-group">
              {provider.modes.map((m) => (
                <label key={m}>
                  <input
                    type="radio"
                    name="im-mode"
                    checked={mode === m}
                    onChange={() => setMode(m)}
                  />
                  <span>{m === "stream" ? C.modeStream : C.modeWebhook}</span>
                </label>
              ))}
            </div>
            <p className="im-mode-hint">{mode === "stream" ? C.modeHintStream : C.modeHintWebhook}</p>

            <div className="im-fields">
              {provider.fields.map((field) => (
                <label key={field}>
                  <span>{fieldLabels[field] || field}</span>
                  <input
                    value={field === "app_id" || field === "bot_id" || field === "app_key" ? appId : secret}
                    onChange={(e) =>
                      field === "app_id" || field === "bot_id" || field === "app_key"
                        ? setAppId(e.target.value)
                        : setSecret(e.target.value)
                    }
                    placeholder={field}
                    autoComplete="off"
                  />
                </label>
              ))}
            </div>

            {error && <p className="im-error">{error}</p>}

            <footer className="im-dialog-foot">
              <button type="button" className="im-btn ghost" onClick={onClose}>
                {C.cancel}
              </button>
              <button
                type="button"
                className="im-btn primary"
                disabled={busy}
                onClick={() => void submit()}
              >
                {busy ? C.submitting : C.submit}
              </button>
            </footer>
          </>
        )}

        {error && isQr && <p className="im-error">{error}</p>}
      </div>
    </div>
  );
}

// --- 身份绑定 ---------------------------------------------------------------

interface IdentityProps {
  token: string;
  C: typeof ZH;
}

/**
 * 把 IM 账号绑到工作台账号。
 *
 * 绑定码在工作台生成（此时身份可信），用户在 IM 里发 `/bind ABC123` 兑换——
 * 反过来（IM 里点一下就绑成某人）不可行，那等于谁都能冒充审批人。
 */
function IdentityBinding({ token, C }: IdentityProps) {
  const [link, setLink] = useState<ImLinkCode | null>(null);
  const [rows, setRows] = useState<ImIdentity[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const reload = useCallback(async () => {
    try {
      setRows(await listImIdentities(token));
    } catch {
      setRows([]);
    }
  }, [token]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function generate() {
    setBusy(true);
    setError("");
    setCopied(false);
    try {
      setLink(await createImLinkCode(token));
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function unbind(row: ImIdentity) {
    setBusy(true);
    setError("");
    try {
      await deleteImIdentity(token, row.provider, row.sender_id);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const command = link ? `/bind ${link.code}` : "";

  async function copy() {
    if (!command) return;
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      // 剪贴板不可用（非 https / 权限拒绝）时用户仍可手动选中复制
    }
  }

  return (
    <section className="im-binding">
      <header className="stg-head">
        <div>
          <h2>{C.identityTitle}</h2>
          <p>{C.identityLede}</p>
        </div>
      </header>

      <div className="im-link-row">
        <button type="button" className="im-btn primary" disabled={busy} onClick={() => void generate()}>
          {busy ? C.generating : C.generateCode}
        </button>
        {link && (
          <div className="im-link-code">
            <code>{command}</code>
            <button type="button" className="im-btn ghost" onClick={() => void copy()}>
              {copied ? C.copied : C.copy}
            </button>
            <span className="im-link-tip">
              {C.identityTip.replace("{ttl}", String(Math.round(link.ttl / 60)))}
            </span>
          </div>
        )}
      </div>
      {error && <p className="im-error">{error}</p>}

      {rows.length === 0 ? (
        <p className="im-empty">{C.identityEmpty}</p>
      ) : (
        <table className="im-bind-table">
          <thead>
            <tr>
              <th>{C.bindingProvider}</th>
              <th>IM ID</th>
              <th>{C.identityUser}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.provider}:${row.sender_id}`}>
                <td>{row.provider}</td>
                <td>{row.sender_id}</td>
                <td>{row.username}</td>
                <td>
                  <button
                    type="button"
                    className="stg-quiet danger"
                    disabled={busy}
                    onClick={() => void unbind(row)}
                  >
                    {C.remove}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
