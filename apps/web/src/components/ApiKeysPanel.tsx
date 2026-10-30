import { useCallback, useEffect, useState } from "react";

import { createApiKey, listApiKeys, revokeApiKey } from "../api";
import type { I18nKey } from "../i18n";
import type { ApiKeyCreated, ApiKeyRecord } from "../types";

/**
 * 账号级 API Keys：本地 Claude Code / CLI 访问本服务的唯一凭据。
 *
 * 明文只在创建那一刻返回，之后只能看到前缀——所以创建成功后直接给出
 * 可复制的 `.mcp.json` 片段与 `claude mcp add` 命令，用户不需要理解协议。
 */
export function ApiKeysPanel({
  token,
  tr,
}: {
  token: string;
  tr: (key: I18nKey | string) => string;
}) {
  const [open, setOpen] = useState(false);
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");

  const refresh = useCallback(async () => {
    try {
      setKeys(await listApiKeys(token));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [token]);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = await createApiKey(token, name.trim() || tr("apiKeyName"));
      setCreated(body);
      setName("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    if (!confirm(tr("apiKeyRevokeConfirm"))) return;
    try {
      await revokeApiKey(token, id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function copy(text: string, tag: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(tag);
      setTimeout(() => setCopied(""), 1500);
    } catch {
      setCopied("");
    }
  }

  return (
    <>
      <button type="button" className="op-chip" onClick={() => setOpen(true)}>
        {tr("apiKeys")}
      </button>
      {open && (
        <div
          className="mem-overlay"
          onClick={(e) => e.target === e.currentTarget && setOpen(false)}
        >
          <div className="panel mem-modal kb-modal">
            <div className="kb-modal-head">
              <strong>{tr("apiKeys")}</strong>
              <button type="button" className="op-chip" onClick={() => setOpen(false)}>
                {tr("apiKeyClose")}
              </button>
            </div>

            <div className="kb-modal-body">
              <p className="muted">{tr("apiKeysHint")}</p>

              <form className="row" onSubmit={submit}>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={tr("apiKeyNamePlaceholder")}
                />
                <button type="submit" className="primary" disabled={busy}>
                  {tr("apiKeyCreate")}
                </button>
              </form>

              {error && <p className="error">{error}</p>}

              {created && (
                <div className="panel" style={{ marginTop: 12 }}>
                  <p>
                    <strong>{tr("apiKeyCreated")}</strong>
                  </p>
                  <p>
                    {tr("apiKeyPlaintext")}:{" "}
                    <code>{created.plaintext}</code>{" "}
                    <button
                      type="button"
                      className="op-chip"
                      onClick={() => copy(created.plaintext, "plain")}
                    >
                      {copied === "plain" ? tr("apiKeyCopied") : tr("apiKeyCopy")}
                    </button>
                  </p>
                  <p>{tr("apiKeyMcpConfig")}</p>
                  <pre className="code-block">{JSON.stringify(created.mcp.json, null, 2)}</pre>
                  <button
                    type="button"
                    className="op-chip"
                    onClick={() => copy(JSON.stringify(created.mcp.json, null, 2), "json")}
                  >
                    {copied === "json" ? tr("apiKeyCopied") : tr("apiKeyCopy")}
                  </button>
                  <p>{tr("apiKeyCommand")}</p>
                  <pre className="code-block">{created.mcp.command}</pre>
                  <button
                    type="button"
                    className="op-chip"
                    onClick={() => copy(created.mcp.command, "cmd")}
                  >
                    {copied === "cmd" ? tr("apiKeyCopied") : tr("apiKeyCopy")}
                  </button>
                </div>
              )}

              <table className="table" style={{ marginTop: 12 }}>
                <thead>
                  <tr>
                    <th>{tr("apiKeyName")}</th>
                    <th>{tr("apiKeyPrefix")}</th>
                    <th>{tr("apiKeyCreatedAt")}</th>
                    <th>{tr("apiKeyLastUsed")}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {keys.length === 0 && (
                    <tr>
                      <td colSpan={5} className="muted">
                        {tr("apiKeyEmpty")}
                      </td>
                    </tr>
                  )}
                  {keys.map((key) => (
                    <tr key={key.id} style={{ opacity: key.revoked_at ? 0.5 : 1 }}>
                      <td>{key.name}</td>
                      <td>
                        <code>{key.prefix}…</code>
                      </td>
                      <td>{new Date(key.created_at * 1000).toLocaleString()}</td>
                      <td>
                        {key.last_used_at
                          ? new Date(key.last_used_at * 1000).toLocaleString()
                          : tr("apiKeyNeverUsed")}
                      </td>
                      <td>
                        {!key.revoked_at && (
                          <button
                            type="button"
                            className="op-chip"
                            onClick={() => revoke(key.id)}
                          >
                            {tr("apiKeyRevoke")}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
