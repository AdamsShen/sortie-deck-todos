import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { api } from "../api";
import type { GitCredentialInfo, Lang, UserRepoInfo, WorkspaceRepoInfo } from "../types";

/**
 * 设置中心 · Git 凭据页（设计稿 v2「设置中心」）。
 *
 * 两套凭据两种权限：
 * - 工作区凭据由管理员在「管理 › 仓库」维护，这里只展示元信息，永不提供写入口；
 * - 个人凭据只属于当前用户，可新增、改名、替换 token、删除。
 *
 * 匹配规则与后端一致：workspace 仓库走条目绑定的凭据；custom 仓库按
 * (host, scheme, username) 匹配本人凭据。匹配不到时在执行前的绑定阶段就会拒绝。
 */

const ZH = {
  title: "Git 凭据",
  lede: "两套凭据，两种权限：工作区凭据由管理员维护、全员可用，你只能看；个人凭据只属于你，可增删改。",
  addCred: "添加我的凭据",
  wsTitle: "工作区凭据",
  wsMeta: "ADMIN MANAGED",
  wsReadonly: "只读",
  wsNote: "密钥写入后不可读回；这里只有主机、账号、指纹与最近使用。",
  gotoAdmin: "去「管理 › 仓库」维护",
  mineTitle: "我的凭据",
  mineMeta: "OWNER ONLY",
  mineNote: "按「主机 + 协议 + 用户名」匹配私有仓库；改这三项等于新建一条。明文只写不读。",
  readoutWs: "工作区凭据 · 只读",
  readoutMine: "我的凭据",
  readoutRepos: "可用仓库",
  readoutAttn: "需要处理",
  myRepos: "我的私有仓库",
  myReposNote: "登记后会出现在任务仓库选择器的下拉里；执行时按上面的凭据自动匹配。",
  addRepo: "登记仓库",
  matching: "仓库匹配预览",
  matchingNote: "执行前按这张表解析凭据；匹配不到会在绑定阶段直接拒绝。",
  colRepo: "仓库",
  colSource: "来源",
  colHost: "主机",
  colCred: "将使用的凭据",
  colState: "状态",
  sourceWs: "工作区",
  sourceCustom: "自定义",
  stateOk: "已匹配",
  stateMiss: "缺少凭据",
  emptyMine: "还没有个人凭据。自定义私有仓库会因匹配不到凭据而无法拉取。",
  emptyRepos: "还没有登记私有仓库。",
  addTitle: "添加我的凭据",
  editTitle: "编辑凭据",
  addSub: "加密落库，保存后不再回显明文",
  editSub: "可改名称或替换 Token；匹配键保持不变",
  label: "名称",
  host: "主机",
  scheme: "协议",
  username: "用户名",
  secret: "Token / 密码",
  labelPh: "例如：我的 GitLab",
  hostPh: "gitlab.example.com",
  usernamePh: "git 账号名",
  secretPhAdd: "只写不读",
  secretPhEdit: "留空则不修改",
  hostLocked: "主机是匹配键，不可修改",
  userLocked: "用户名同属匹配键",
  save: "保存",
  cancel: "取消",
  edit: "编辑",
  delete: "删除",
  remove: "移除",
  formNoteAdd: "保存后仅显示指纹末四位",
  formNoteEdit: "替换 Token 后旧值立即失效",
  saved: "凭据已保存",
  deleted: "凭据已删除",
  repoAdded: "仓库已登记",
  repoRemoved: "已移除",
  hostRequired: "主机必填",
  secretRequired: "Token 必填",
  repoUrlRequired: "仓库地址必填",
  deleteConfirm: (label: string) =>
    `删除「${label}」？\n\n使用它的私有仓库会立刻失去访问权限；已开始的拉取不受影响。`,
  repoDeleteConfirm: (name: string) => `从下拉列表移除「${name}」？\n\n不会删除远端仓库。`,
  repoName: "目录名",
  repoUrl: "仓库地址",
  repoBranch: "默认分支",
  repoNamePh: "sortie-deck-todos",
  repoUrlPh: "https://github.com/you/repo.git",
  repoBranchPh: "main",
  repoFormNote: "按主机自动匹配上面的个人凭据。",
  neverUsed: "从未使用",
  boundRepos: "绑定仓库",
  inactive: "已禁用或过期",
  loading: "正在读取凭据…",
  addRepoTitle: "登记私有仓库",
  hostAccount: "账号",
  hostFingerprint: "指纹",
  hostLastUsed: "最近使用",
};

const EN: typeof ZH = {
  title: "Git credentials",
  lede: "Two tiers, two permissions: workspace credentials are maintained by admins and shared with everyone — read-only here. Personal credentials belong to you and can be changed.",
  addCred: "Add my credential",
  wsTitle: "Workspace credentials",
  wsMeta: "ADMIN MANAGED",
  wsReadonly: "Read only",
  wsNote: "Secrets are write-only. Only host, account, fingerprint and last use are shown here.",
  gotoAdmin: "Maintain in Admin › Repos",
  mineTitle: "My credentials",
  mineMeta: "OWNER ONLY",
  mineNote: "Matched to private repos by host + scheme + username; changing those creates a new entry. Secrets are write-only.",
  readoutWs: "Workspace · read only",
  readoutMine: "My credentials",
  readoutRepos: "Available repos",
  readoutAttn: "Needs attention",
  myRepos: "My private repos",
  myReposNote: "Registered repos appear in the mission repo picker and use the credentials above at run time.",
  addRepo: "Register repo",
  matching: "Repo → credential preview",
  matchingNote: "Credentials are resolved from this table before a run; a miss is rejected at bind time.",
  colRepo: "Repo",
  colSource: "Source",
  colHost: "Host",
  colCred: "Credential",
  colState: "State",
  sourceWs: "workspace",
  sourceCustom: "custom",
  stateOk: "Matched",
  stateMiss: "Missing credential",
  emptyMine: "No personal credentials yet. Custom private repos will fail to clone.",
  emptyRepos: "No private repos registered yet.",
  addTitle: "Add my credential",
  editTitle: "Edit credential",
  addSub: "Encrypted at rest; the secret is never shown again",
  editSub: "Rename or replace the token; matching keys stay fixed",
  label: "Label",
  host: "Host",
  scheme: "Scheme",
  username: "Username",
  secret: "Token / password",
  labelPh: "e.g. My GitLab",
  hostPh: "gitlab.example.com",
  usernamePh: "git account",
  secretPhAdd: "write-only",
  secretPhEdit: "leave blank to keep",
  hostLocked: "Host is a matching key and cannot change",
  userLocked: "Username is also a matching key",
  save: "Save",
  cancel: "Cancel",
  edit: "Edit",
  delete: "Delete",
  remove: "Remove",
  formNoteAdd: "Only the last four characters of the fingerprint are kept",
  formNoteEdit: "Replacing the token invalidates the old one immediately",
  saved: "Credential saved",
  deleted: "Credential deleted",
  repoAdded: "Repo registered",
  repoRemoved: "Removed",
  hostRequired: "Host is required",
  secretRequired: "Token is required",
  repoUrlRequired: "Repo URL is required",
  deleteConfirm: (label: string) =>
    `Delete "${label}"?\n\nPrivate repos using it lose access immediately; running clones are not affected.`,
  repoDeleteConfirm: (name: string) => `Remove "${name}" from the picker?\n\nThe remote repo is not deleted.`,
  repoName: "Directory",
  repoUrl: "Repo URL",
  repoBranch: "Default branch",
  repoNamePh: "sortie-deck-todos",
  repoUrlPh: "https://github.com/you/repo.git",
  repoBranchPh: "main",
  repoFormNote: "Matched to your credentials by host.",
  neverUsed: "Never used",
  boundRepos: "Bound repos",
  inactive: "Disabled or expired",
  loading: "Loading credentials…",
  addRepoTitle: "Register private repo",
  hostAccount: "Account",
  hostFingerprint: "Fingerprint",
  hostLastUsed: "Last used",
};

const pad = (n: number) => String(n).padStart(2, "0");

/** 秒级时间戳 → MM-DD HH:mm（本地时区）；null 表示从未使用 */
function fmtWhen(ts: number | null | undefined, neverText: string): string {
  if (!ts) return neverText;
  const d = new Date(ts * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 从 clone URL 解析 host，兼容 https 与 git@host:group/repo.git */
function hostOf(url: string): string {
  const raw = (url || "").trim();
  if (!raw) return "";
  if (raw.startsWith("git@")) {
    const rest = raw.slice(4);
    const idx = rest.indexOf(":");
    return (idx >= 0 ? rest.slice(0, idx) : rest).toLowerCase();
  }
  try {
    return new URL(raw).hostname.toLowerCase();
  } catch {
    return "";
  }
}

/** 个人仓库的协议：git@ / ssh:// 视为 ssh，其余按 https 匹配 */
function schemeOf(url: string): "https" | "ssh" {
  const raw = (url || "").trim().toLowerCase();
  return raw.startsWith("git@") || raw.startsWith("ssh://") ? "ssh" : "https";
}

function isActive(cred: GitCredentialInfo): boolean {
  if (cred.disabled_at) return false;
  if (!cred.expires_at) return true;
  return cred.expires_at * 1000 > Date.now();
}

interface Props {
  token: string;
  lang: Lang;
  isAdmin: boolean;
  onOpenAdmin: () => void;
  onCountChange?: (n: number) => void;
}

export function GitSettingsPanel({ token, lang, isAdmin, onOpenAdmin, onCountChange }: Props) {
  const C = lang === "zh" ? ZH : EN;
  const [wsCreds, setWsCreds] = useState<GitCredentialInfo[]>([]);
  const [myCreds, setMyCreds] = useState<GitCredentialInfo[]>([]);
  const [wsRepos, setWsRepos] = useState<WorkspaceRepoInfo[]>([]);
  const [myRepos, setMyRepos] = useState<UserRepoInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef<number | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [editCred, setEditCred] = useState<GitCredentialInfo | null>(null);
  const [form, setForm] = useState({ label: "", host: "", scheme: "https", username: "", secret: "" });
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);

  const [repoOpen, setRepoOpen] = useState(false);
  const [repoForm, setRepoForm] = useState({ name: "", url: "", default_branch: "" });
  const [repoError, setRepoError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [wc, mc, wr, mr] = await Promise.all([
        api<GitCredentialInfo[]>("/api/git/workspace-credentials", { token }),
        api<GitCredentialInfo[]>("/api/me/git-credentials", { token }),
        api<WorkspaceRepoInfo[]>("/api/git/workspace-repos", { token }),
        api<UserRepoInfo[]>("/api/me/git/repos", { token }),
      ]);
      setWsCreds(wc);
      setMyCreds(mc);
      setWsRepos(wr);
      setMyRepos(mr);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    if (token) void load();
  }, [token, load]);

  useEffect(() => {
    onCountChange?.(wsCreds.length + myCreds.length);
  }, [wsCreds.length, myCreds.length, onCountChange]);

  useEffect(
    () => () => {
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    },
    [],
  );

  function showToast(msg: string) {
    setToast(msg);
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 1800);
  }

  function openAdd() {
    setEditCred(null);
    setForm({ label: "", host: "", scheme: "https", username: "", secret: "" });
    setFormError("");
    setModalOpen(true);
  }

  function openEdit(cred: GitCredentialInfo) {
    setEditCred(cred);
    setForm({
      label: cred.label,
      host: cred.host,
      scheme: cred.scheme,
      username: cred.username,
      secret: "",
    });
    setFormError("");
    setModalOpen(true);
  }

  function closeModal() {
    setModalOpen(false);
    setEditCred(null);
    setFormError("");
  }

  async function saveCredential(e: FormEvent) {
    e.preventDefault();
    const label = form.label.trim();
    const host = form.host.trim();
    if (!host) {
      setFormError(C.hostRequired);
      return;
    }
    if (!editCred && !form.secret) {
      setFormError(C.secretRequired);
      return;
    }
    setSaving(true);
    setFormError("");
    try {
      if (editCred) {
        if (form.secret) {
          // 替换 token 走覆盖写入（同一 host + scheme + username 命中唯一键）
          await api("/api/me/git-credentials", {
            token,
            method: "POST",
            body: JSON.stringify({
              label: label || editCred.label,
              host: editCred.host,
              scheme: editCred.scheme,
              username: editCred.username,
              secret: form.secret,
            }),
          });
        } else {
          // 不改 token 时只改展示名，避免逼用户重新贴一次密钥
          await api(`/api/me/git-credentials/${editCred.id}`, {
            token,
            method: "PATCH",
            body: JSON.stringify({ label: label || editCred.label }),
          });
        }
      } else {
        await api("/api/me/git-credentials", {
          token,
          method: "POST",
          body: JSON.stringify({
            label: label || host,
            host,
            scheme: form.scheme,
            username: form.username.trim(),
            secret: form.secret,
          }),
        });
      }
      closeModal();
      await load();
      showToast(C.saved);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function removeCredential(cred: GitCredentialInfo) {
    if (!window.confirm(C.deleteConfirm(cred.label))) return;
    try {
      await api(`/api/me/git-credentials/${cred.id}`, { token, method: "DELETE" });
      await load();
      showToast(C.deleted);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function addRepo(e: FormEvent) {
    e.preventDefault();
    const url = repoForm.url.trim();
    if (!url) {
      setRepoError(C.repoUrlRequired);
      return;
    }
    const fallbackName = url.split("/").pop()?.replace(/\.git$/, "") || "repo";
    try {
      await api("/api/me/git/repos", {
        token,
        method: "POST",
        body: JSON.stringify({
          name: repoForm.name.trim() || fallbackName,
          url,
          default_branch: repoForm.default_branch.trim(),
        }),
      });
      setRepoForm({ name: "", url: "", default_branch: "" });
      setRepoOpen(false);
      setRepoError("");
      await load();
      showToast(C.repoAdded);
    } catch (err) {
      setRepoError(err instanceof Error ? err.message : String(err));
    }
  }

  async function removeRepo(repo: UserRepoInfo) {
    if (!window.confirm(C.repoDeleteConfirm(repo.name))) return;
    try {
      await api(`/api/me/git/repos/${repo.id}`, { token, method: "DELETE" });
      await load();
      showToast(C.repoRemoved);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  /** 工作区凭据被多少条工作区仓库绑定（只读展示，帮助判断能不能动） */
  const boundCount = useCallback(
    (credId: string) => wsRepos.filter((r) => r.credential_id === credId).length,
    [wsRepos],
  );

  /** 仓库 → 凭据匹配结果；工作区与自定义分开解析，不做优先级回退 */
  const matchRows = useMemo(() => {
    const ws = wsRepos.map((repo) => {
      const cred = wsCreds.find((c) => c.id === repo.credential_id);
      return {
        key: `ws-${repo.id}`,
        name: repo.name,
        source: "workspace" as const,
        host: hostOf(repo.url),
        credential: cred?.label || "",
        ok: Boolean(cred),
      };
    });
    const mine = myRepos.map((repo) => {
      const host = hostOf(repo.url);
      const scheme = schemeOf(repo.url);
      const cred = myCreds.find((c) => c.host === host && c.scheme === scheme && isActive(c));
      return {
        key: `my-${repo.id}`,
        name: repo.name,
        source: "custom" as const,
        host,
        credential: cred?.label || "",
        ok: Boolean(cred),
      };
    });
    return [...ws, ...mine];
  }, [wsRepos, myRepos, wsCreds, myCreds]);

  const attention =
    matchRows.filter((r) => !r.ok).length + myCreds.filter((c) => !isActive(c)).length;

  return (
    <section className="stg">
      <header className="stg-head">
        <div>
          <h2>{C.title}</h2>
          <p>{C.lede}</p>
        </div>
        <button type="button" className="stg-primary" onClick={openAdd}>
          ＋ {C.addCred}
        </button>
      </header>

      {error && <p className="stg-error">{error}</p>}

      {loading ? (
        <div className="stg-loading">{C.loading}</div>
      ) : (
        <>
          <div className="stg-readout">
            <div className="seg">
              <div className="v">{wsCreds.length}</div>
              <div className="k">{C.readoutWs}</div>
            </div>
            <div className="seg">
              <div className="v">{myCreds.length}</div>
              <div className="k">{C.readoutMine}</div>
            </div>
            <div className="seg">
              <div className="v">{matchRows.length}</div>
              <div className="k">{C.readoutRepos}</div>
            </div>
            <div className={`seg${attention ? " attn" : ""}`}>
              <div className="v">{attention}</div>
              <div className="k">{C.readoutAttn}</div>
            </div>
          </div>

          <div className="stg-cols">
            <div>
              <div className="stg-col-head">
                <h3>{C.wsTitle}</h3>
                <span className="meta">{C.wsMeta}</span>
                <span className="sp" />
                <span className="ro">{C.wsReadonly}</span>
              </div>
              <div className="stg-sealed-list">
                {wsCreds.map((cred) => (
                  <article key={cred.id} className="stg-sealed">
                    <span className="stg-stamp">READ ONLY</span>
                    <div className="label">{cred.label}</div>
                    <div className="host">
                      {cred.scheme}://{cred.host}
                    </div>
                    <div className="facts">
                      <div>
                        <span className="k">{C.hostAccount}</span>
                        <span className="v">{cred.username || "—"}</span>
                      </div>
                      <div>
                        <span className="k">{C.hostFingerprint}</span>
                        <span className="v">{cred.fingerprint}</span>
                      </div>
                      <div>
                        <span className="k">{C.hostLastUsed}</span>
                        <span className="v">{fmtWhen(cred.last_used_at, C.neverUsed)}</span>
                      </div>
                      <div>
                        <span className="k">{C.boundRepos}</span>
                        <span className="v">{boundCount(cred.id)}</span>
                      </div>
                    </div>
                  </article>
                ))}
                {!wsCreds.length && <div className="stg-empty">{C.wsNote}</div>}
              </div>
              <p className="stg-note">
                {C.wsNote}
                {isAdmin && (
                  <>
                    {" "}
                    <button type="button" className="stg-link" onClick={onOpenAdmin}>
                      {C.gotoAdmin} →
                    </button>
                  </>
                )}
              </p>
            </div>

            <div>
              <div className="stg-col-head">
                <h3>{C.mineTitle}</h3>
                <span className="meta">{C.mineMeta}</span>
                <span className="sp" />
                <span className="ro">{myCreds.length}</span>
              </div>
              <div className="stg-mine-list">
                {myCreds.map((cred) => (
                  <div key={cred.id} className="stg-mine-row">
                    <div className="body">
                      <div className="name">
                        <span className={`dot${isActive(cred) ? "" : " stale"}`} />
                        {cred.label}
                        {!isActive(cred) && <span className="stg-chip">{C.inactive}</span>}
                      </div>
                      <div className="host">
                        {cred.scheme}://{cred.host}
                      </div>
                      <div className="facts">
                        <span>
                          {C.hostAccount} <b>{cred.username || "—"}</b>
                        </span>
                        <span>
                          {C.hostFingerprint} <b>{cred.fingerprint}</b>
                        </span>
                        <span>
                          {C.hostLastUsed} <b>{fmtWhen(cred.last_used_at, C.neverUsed)}</b>
                        </span>
                      </div>
                    </div>
                    <div className="ops">
                      <button type="button" onClick={() => openEdit(cred)}>
                        {C.edit}
                      </button>
                      <button type="button" className="danger" onClick={() => void removeCredential(cred)}>
                        {C.delete}
                      </button>
                    </div>
                  </div>
                ))}
                {!myCreds.length && <div className="stg-empty">{C.emptyMine}</div>}
              </div>
              <p className="stg-note">{C.mineNote}</p>
            </div>
          </div>

          <section className="stg-sec">
            <div className="stg-sec-head">
              <div>
                <h3>{C.myRepos}</h3>
                <p>{C.myReposNote}</p>
              </div>
              <span className="sp" />
              <button type="button" className="stg-quiet" onClick={() => setRepoOpen((v) => !v)}>
                {repoOpen ? C.cancel : `＋ ${C.addRepo}`}
              </button>
            </div>

            {repoOpen && (
              <form className="stg-repo-form" onSubmit={addRepo}>
                <label>
                  <span>{C.repoName}</span>
                  <input
                    value={repoForm.name}
                    placeholder={C.repoNamePh}
                    onChange={(e) => setRepoForm({ ...repoForm, name: e.target.value })}
                  />
                </label>
                <label className="wide">
                  <span>{C.repoUrl}</span>
                  <input
                    value={repoForm.url}
                    placeholder={C.repoUrlPh}
                    onChange={(e) => setRepoForm({ ...repoForm, url: e.target.value })}
                  />
                </label>
                <label>
                  <span>{C.repoBranch}</span>
                  <input
                    value={repoForm.default_branch}
                    placeholder={C.repoBranchPh}
                    onChange={(e) => setRepoForm({ ...repoForm, default_branch: e.target.value })}
                  />
                </label>
                <button type="submit" className="stg-primary">
                  {C.addRepo}
                </button>
                {repoError && <p className="stg-error inline">{repoError}</p>}
              </form>
            )}

            <div className="stg-repo-list">
              {myRepos.map((repo) => {
                const host = hostOf(repo.url);
                const cred = myCreds.find(
                  (c) => c.host === host && c.scheme === schemeOf(repo.url) && isActive(c),
                );
                return (
                  <div key={repo.id} className="stg-repo-row">
                    <div className="nm">{repo.name}</div>
                    <div className="url">{repo.url}</div>
                    <div className="branch">{repo.default_branch || "—"}</div>
                    <div className={`cred${cred ? "" : " miss"}`}>{cred ? cred.label : "—"}</div>
                    <button type="button" className="danger" onClick={() => void removeRepo(repo)}>
                      {C.remove}
                    </button>
                  </div>
                );
              })}
              {!myRepos.length && <div className="stg-empty">{C.emptyRepos}</div>}
            </div>
          </section>

          <section className="stg-sec">
            <div className="stg-sec-head">
              <div>
                <h3>{C.matching}</h3>
                <p>{C.matchingNote}</p>
              </div>
            </div>
            <table className="stg-table">
              <thead>
                <tr>
                  <th>{C.colRepo}</th>
                  <th>{C.colSource}</th>
                  <th>{C.colHost}</th>
                  <th>{C.colCred}</th>
                  <th>{C.colState}</th>
                </tr>
              </thead>
              <tbody>
                {matchRows.map((row) => (
                  <tr key={row.key}>
                    <td>
                      <span className="repo">{row.name}</span>
                      <span className="src">{row.source}</span>
                    </td>
                    <td className="mono">{row.source === "workspace" ? C.sourceWs : C.sourceCustom}</td>
                    <td className="mono">{row.host || "—"}</td>
                    <td className="mono">{row.credential || "—"}</td>
                    <td>
                      <span className={`state ${row.ok ? "ok" : "miss"}`}>
                        <span className={`dot${row.ok ? "" : " stale"}`} />
                        {row.ok ? C.stateOk : C.stateMiss}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}

      {modalOpen && (
        <div
          className="stg-modal"
          role="dialog"
          aria-modal="true"
          aria-label={editCred ? C.editTitle : C.addTitle}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closeModal();
          }}
        >
          <form className="stg-sheet" onSubmit={saveCredential} noValidate>
            <div className="stg-sheet-head">
              <div>
                <h3>{editCred ? C.editTitle : C.addTitle}</h3>
                <p>{editCred ? C.editSub : C.addSub}</p>
              </div>
              <span className="sp" />
              <button type="button" className="x" onClick={closeModal} aria-label={C.cancel}>
                ×
              </button>
            </div>

            <div className="stg-fields">
              <label>
                <span>{C.label}</span>
                <input
                  value={form.label}
                  placeholder={C.labelPh}
                  autoComplete="off"
                  onChange={(e) => setForm({ ...form, label: e.target.value })}
                />
              </label>
              <label>
                <span>{C.host}</span>
                <input
                  value={form.host}
                  placeholder={C.hostPh}
                  autoComplete="off"
                  disabled={Boolean(editCred)}
                  onChange={(e) => setForm({ ...form, host: e.target.value })}
                />
                {editCred && <em>{C.hostLocked}</em>}
              </label>
              <label>
                <span>{C.scheme}</span>
                <select
                  value={form.scheme}
                  disabled={Boolean(editCred)}
                  onChange={(e) => setForm({ ...form, scheme: e.target.value })}
                >
                  <option value="https">https</option>
                  <option value="ssh">ssh</option>
                </select>
              </label>
              <label>
                <span>{C.username}</span>
                <input
                  value={form.username}
                  placeholder={C.usernamePh}
                  autoComplete="off"
                  disabled={Boolean(editCred)}
                  onChange={(e) => setForm({ ...form, username: e.target.value })}
                />
                {editCred && <em>{C.userLocked}</em>}
              </label>
              <label className="full">
                <span>{C.secret}</span>
                <input
                  type="password"
                  value={form.secret}
                  placeholder={editCred ? C.secretPhEdit : C.secretPhAdd}
                  autoComplete="new-password"
                  onChange={(e) => setForm({ ...form, secret: e.target.value })}
                />
              </label>
            </div>

            {formError && <p className="stg-error inline">{formError}</p>}

            <div className="stg-sheet-foot">
              <span className="note">{editCred ? C.formNoteEdit : C.formNoteAdd}</span>
              <span className="sp" />
              <button type="button" className="stg-quiet" onClick={closeModal}>
                {C.cancel}
              </button>
              <button type="submit" className="stg-primary" disabled={saving}>
                {C.save}
              </button>
            </div>
          </form>
        </div>
      )}

      {toast && <div className="stg-toast">{toast}</div>}
    </section>
  );
}
