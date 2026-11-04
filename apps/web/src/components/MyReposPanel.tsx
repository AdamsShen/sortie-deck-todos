import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { UserRepoInfo } from "../types";

/** 个人私有仓库登记：仅用于「我的私有仓库」下拉，凭据按 host 另外匹配。 */
export function MyReposPanel({ token, lang }: { token: string; lang: "zh" | "en" }) {
  const zh = lang === "zh";
  const [rows, setRows] = useState<UserRepoInfo[]>([]);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState({ name: "", url: "", default_branch: "" });

  const load = useCallback(async () => {
    try {
      setRows(await api<UserRepoInfo[]>("/api/me/git/repos", { token }));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  useEffect(() => {
    if (token) void load();
  }, [token, load]);

  async function add() {
    try {
      await api("/api/me/git/repos", { token, method: "POST", body: JSON.stringify(form) });
      setForm({ name: "", url: "", default_branch: "" });
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-label">{zh ? "我的私有仓库" : "My private repos"}</div>
      <p className="tiny">{zh ? "登记后会出现在任务仓库选择器的下拉里。" : "Listed in the mission repo picker."}</p>
      {rows.map((r) => (
        <div key={r.id} className="briefing-repo-row">
          <span className="tiny">{`${r.name} → ${r.url}${r.default_branch ? ` (${r.default_branch})` : ""}`}</span>
          <span className="sp" />
          <button
            type="button"
            className="btn linkish"
            onClick={() => void api(`/api/me/git/repos/${r.id}`, { token, method: "DELETE" }).then(load)}
          >
            ×
          </button>
        </div>
      ))}
      <div className="briefing-repo-row" style={{ flexWrap: "wrap", marginTop: 8 }}>
        <input value={form.name} placeholder={zh ? "目录名" : "name"} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input value={form.url} placeholder="https://github.com/you/repo.git" onChange={(e) => setForm({ ...form, url: e.target.value })} />
        <input value={form.default_branch} placeholder={zh ? "默认分支" : "default branch"} onChange={(e) => setForm({ ...form, default_branch: e.target.value })} />
        <button type="button" className="btn ghost tiny" disabled={!form.url} onClick={() => void add()}>
          {zh ? "登记" : "Add"}
        </button>
      </div>
      {msg && <p className="tiny">{msg}</p>}
    </section>
  );
}
