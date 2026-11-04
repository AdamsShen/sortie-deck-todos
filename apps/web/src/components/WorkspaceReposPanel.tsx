import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { WorkspaceRepoInfo } from "../types";

/** 管理端：工作区仓库（全员可见可选用，仅管理员可改，凭据只写不读）。 */
export function WorkspaceReposPanel({ token, lang }: { token: string; lang: "zh" | "en" }) {
  const zh = lang === "zh";
  const [rows, setRows] = useState<WorkspaceRepoInfo[]>([]);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState({
    name: "",
    url: "",
    default_branch: "main",
    role_slots: "eng",
    host: "",
    username: "",
    secret: "",
  });
  const [rotate, setRotate] = useState({ repo_id: "", host: "", username: "", secret: "" });

  const load = useCallback(async () => {
    try {
      const all = await api<WorkspaceRepoInfo[]>("/api/git/workspace-repos", { token });
      setRows(all);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  useEffect(() => {
    if (token) void load();
  }, [token, load]);

  async function create() {
    try {
      await api("/api/admin/git/workspace-repos", {
        token,
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          url: form.url,
          default_branch: form.default_branch,
          role_slots: form.role_slots.split(",").map((s) => s.trim()).filter(Boolean),
          credential: {
            label: form.host,
            host: form.host,
            scheme: "https",
            username: form.username,
            secret: form.secret,
          },
        }),
      });
      setForm({ ...form, name: "", url: "", host: "", username: "", secret: "" });
      setMsg(zh ? "工作区仓库已创建" : "Workspace repo created");
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function toggle(row: WorkspaceRepoInfo) {
    try {
      await api(`/api/admin/git/workspace-repos/${row.id}`, {
        token,
        method: "PATCH",
        body: JSON.stringify({ enabled: !row.enabled }),
      });
      await load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function rotateCredential() {
    if (!rotate.repo_id) return;
    try {
      await api(`/api/admin/git/workspace-repos/${rotate.repo_id}/credential`, {
        token,
        method: "PUT",
        body: JSON.stringify({
          label: rotate.host,
          host: rotate.host,
          scheme: "https",
          username: rotate.username,
          secret: rotate.secret,
        }),
      });
      setRotate({ repo_id: "", host: "", username: "", secret: "" });
      setMsg(zh ? "凭据已覆盖（明文不再显示）" : "Credential rotated");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-label">{zh ? "工作区仓库（管理员配置，全员可用）" : "Workspace repos (admin)"}</div>
      <p className="tiny">
        {zh
          ? "这里配置的仓库会出现在所有人建任务时的下拉里；凭据只写不读，任务执行时自动使用。"
          : "Repos listed here are selectable by all users; credentials are injected at run time and never shown."}
      </p>
      {rows.map((r) => (
        <div key={r.id} className="briefing-repo-row">
          <span className="tiny">{`${r.name} → ${r.url}${r.default_branch ? ` (${r.default_branch})` : ""}${r.enabled ? "" : " · disabled"}`}</span>
          <span className="sp" />
          <button type="button" className="btn ghost tiny" onClick={() => void toggle(r)}>
            {r.enabled ? (zh ? "停用" : "Disable") : zh ? "启用" : "Enable"}
          </button>
        </div>
      ))}
      <div className="briefing-repo-row" style={{ flexWrap: "wrap", marginTop: 8 }}>
        <input value={form.name} placeholder={zh ? "目录名" : "name"} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input value={form.url} placeholder="https://gitlab.example.com/group/repo.git" onChange={(e) => setForm({ ...form, url: e.target.value })} />
        <input value={form.default_branch} placeholder={zh ? "目标分支" : "branch"} onChange={(e) => setForm({ ...form, default_branch: e.target.value })} />
        <input value={form.role_slots} placeholder="eng,qa" onChange={(e) => setForm({ ...form, role_slots: e.target.value })} />
        <input value={form.host} placeholder="gitlab.example.com" onChange={(e) => setForm({ ...form, host: e.target.value })} />
        <input value={form.username} placeholder={zh ? "账号（可选）" : "username"} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input type="password" value={form.secret} placeholder={zh ? "Token（只写）" : "token"} onChange={(e) => setForm({ ...form, secret: e.target.value })} />
        <button type="button" className="btn ghost tiny" disabled={!form.url || !form.host || !form.secret} onClick={() => void create()}>
          {zh ? "新增仓库" : "Add"}
        </button>
      </div>
      <div className="briefing-repo-row" style={{ flexWrap: "wrap", marginTop: 8 }}>
        <select value={rotate.repo_id} onChange={(e) => setRotate({ ...rotate, repo_id: e.target.value })}>
          <option value="">{zh ? "选择仓库换凭据" : "pick repo to rotate"}</option>
          {rows.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </select>
        <input value={rotate.host} placeholder="host" onChange={(e) => setRotate({ ...rotate, host: e.target.value })} />
        <input value={rotate.username} placeholder="username" onChange={(e) => setRotate({ ...rotate, username: e.target.value })} />
        <input type="password" value={rotate.secret} placeholder="token" onChange={(e) => setRotate({ ...rotate, secret: e.target.value })} />
        <button type="button" className="btn ghost tiny" disabled={!rotate.repo_id || !rotate.host || !rotate.secret} onClick={() => void rotateCredential()}>
          {zh ? "覆盖凭据" : "Rotate"}
        </button>
      </div>
      {msg && <p className="tiny">{msg}</p>}
    </section>
  );
}
