import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { GitCredentialInfo } from "../types";

/** 个人 Git 凭据：只写不读，列表只显示指纹；仅本人可见。 */
export function GitCredentialsPanel({ token, lang }: { token: string; lang: "zh" | "en" }) {
  const zh = lang === "zh";
  const [rows, setRows] = useState<GitCredentialInfo[]>([]);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState({ label: "", host: "", scheme: "https", username: "", secret: "" });

  const load = useCallback(async () => {
    try {
      setRows(await api<GitCredentialInfo[]>("/api/me/git-credentials", { token }));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  useEffect(() => {
    if (token) void load();
  }, [token, load]);

  async function save() {
    try {
      await api("/api/me/git-credentials", {
        token,
        method: "POST",
        body: JSON.stringify({ ...form, label: form.label || form.host }),
      });
      setForm({ label: "", host: "", scheme: "https", username: "", secret: "" });
      setMsg(zh ? "已保存（明文不再显示）" : "Saved (secret not shown again)");
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-label">{zh ? "Git 凭据（个人）" : "Git credentials (personal)"}</div>
      <p className="tiny">
        {zh
          ? "供自定义私有仓库使用；工作区仓库由管理员配凭据。密钥只写不读。"
          : "For your custom private repos. Workspace repos use admin credentials. Secrets are write-only."}
      </p>
      {rows.map((c) => (
        <div key={c.id} className="briefing-repo-row">
          <span className="tiny">{`${c.label} · ${c.host} · ${c.fingerprint}`}</span>
          <span className="sp" />
          <button
            type="button"
            className="btn linkish"
            onClick={() =>
              void api(`/api/me/git-credentials/${c.id}`, { token, method: "DELETE" }).then(load)
            }
          >
            ×
          </button>
        </div>
      ))}
      <div className="briefing-repo-row" style={{ flexWrap: "wrap", marginTop: 8 }}>
        <input value={form.label} placeholder={zh ? "名称" : "label"} onChange={(e) => setForm({ ...form, label: e.target.value })} />
        <input value={form.host} placeholder="gitlab.example.com" onChange={(e) => setForm({ ...form, host: e.target.value })} />
        <select value={form.scheme} onChange={(e) => setForm({ ...form, scheme: e.target.value })}>
          <option value="https">https</option>
          <option value="ssh">ssh</option>
        </select>
        <input value={form.username} placeholder={zh ? "用户名" : "username"} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input type="password" value={form.secret} placeholder={zh ? "Token（只写）" : "token"} onChange={(e) => setForm({ ...form, secret: e.target.value })} />
        <button type="button" className="btn ghost tiny" disabled={!form.host || !form.secret} onClick={() => void save()}>
          {zh ? "保存" : "Save"}
        </button>
      </div>
      {msg && <p className="tiny">{msg}</p>}
    </section>
  );
}
