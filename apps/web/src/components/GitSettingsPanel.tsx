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
  addWsCred: "添加工作区凭据",
  wsAdminWritable: "管理员可管理",
  wsCredAddTitle: "添加工作区凭据",
  wsCredEditTitle: "编辑工作区凭据",
  wsCredDelete: "停用",
  wsCredDeleteConfirm: (label: string) =>
    `停用「${label}」？\n\n所有工作区仓库会立刻失去该凭据；已开始的拉取不受影响。`,
  wsCredDisabled: "工作区凭据已停用",
  wsReposTitle: "工作区仓库",
  wsReposNote: "管理员维护，全员可在任务里选择；执行时使用绑定的工作区凭据。",
  addWsRepo: "添加工作区仓库",
  wsRepoCred: "绑定凭据",
  wsRepoAdded: "工作区仓库已添加",
  wsRepoRemoved: "工作区仓库已停用",
  wsRepoDeleteConfirm: (name: string) =>
    `停用「${name}」？\n\n任务下拉里不再出现，历史任务引用仍可解析。`,
  matchingEmpty:
    "还没有仓库。登记工作区仓库或个人私有仓库后，这里会显示执行时实际使用的凭据；缺凭据会被标记出来。",
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
  myReposNote: "登记后会出现在任务仓库选择器的下拉里；执行时使用每条仓库绑定的凭据。",
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
  repoFormNote: "仓库必须绑定凭据；工作区凭据仅可用于同主机仓库。",
  repoCred: "绑定凭据",
  repoCredRequired: "请选择绑定凭据",
  repoNoCred: "该主机还没有可用凭据，请先添加",
  credHostMismatch: "主机不一致",
  repoEdit: "编辑",
  repoUpdated: "仓库已更新",
  repoUnbound: "未绑定",
  repoAddTitle: "登记私有仓库",
  repoEditTitle: "编辑私有仓库",
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
  addWsCred: "Add workspace credential",
  wsAdminWritable: "Admin managed",
  wsCredAddTitle: "Add workspace credential",
  wsCredEditTitle: "Edit workspace credential",
  wsCredDelete: "Disable",
  wsCredDeleteConfirm: (label: string) =>
    `Disable "${label}"?\n\nAll workspace repos will lose this credential immediately; running clones are unaffected.`,
  wsCredDisabled: "Workspace credential disabled",
  wsReposTitle: "Workspace repos",
  wsReposNote: "Maintained by admins and selectable by everyone; runs use the bound workspace credential.",
  addWsRepo: "Add workspace repo",
  wsRepoCred: "Credential",
  wsRepoAdded: "Workspace repo added",
  wsRepoRemoved: "Workspace repo disabled",
  wsRepoDeleteConfirm: (name: string) =>
    `Disable "${name}"?\n\nIt disappears from the mission picker; existing missions still resolve.`,
  matchingEmpty:
    "No repos yet. Once a workspace or personal repo is registered, this table shows which credential a run will use; missing credentials are flagged.",
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
  myReposNote: "Registered repos appear in the mission picker and use the credential bound to each repo at run time.",
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
  repoFormNote: "Every repo must bind a credential; workspace credentials only work on the same host.",
  repoCred: "Credential",
  repoCredRequired: "Pick a credential",
  repoNoCred: "No credential for this host yet — add one first",
  credHostMismatch: "host mismatch",
  repoEdit: "Edit",
  repoUpdated: "Repo updated",
  repoUnbound: "Unbound",
  repoAddTitle: "Register private repo",
  repoEditTitle: "Edit private repo",
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

/** 从 clone URL 解析 host，兼容 https 与任意 user@host:group/repo.git（scp 形式） */
function hostOf(url: string): string {
  const raw = (url || "").trim();
  if (!raw) return "";
  if (raw.includes("://")) {
    try {
      return new URL(raw).hostname.toLowerCase();
    } catch {
      return "";
    }
  }
  // scp 形式：git@host:path / yulidev@host:path，用户名不一定是 git
  const match = raw.match(/^(?:[^@/]+@)?([^:/]+):/);
  return match ? match[1].toLowerCase() : "";
}

/** 协议：http(s) 为 https，ssh:// 与任意 scp 形式（user@host:path）为 ssh */
function schemeOf(url: string): "https" | "ssh" {
  const raw = (url || "").trim().toLowerCase();
  if (raw.startsWith("https://") || raw.startsWith("http://")) return "https";
  if (raw.startsWith("ssh://")) return "ssh";
  return raw.includes(":") ? "ssh" : "https";
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
  onCountChange?: (n: number) => void;
}

export function GitSettingsPanel({ token, lang, isAdmin, onCountChange }: Props) {
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
  // 同一个弹层服务「我的凭据」与「工作区凭据」，用 scope 区分提交端点
  const [credScope, setCredScope] = useState<"mine" | "workspace">("mine");
  const [editCred, setEditCred] = useState<GitCredentialInfo | null>(null);
  const [form, setForm] = useState({ label: "", host: "", scheme: "https", username: "", secret: "" });
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);

  const [repoOpen, setRepoOpen] = useState(false);
  const [editRepoId, setEditRepoId] = useState<string | null>(null);
  const [repoForm, setRepoForm] = useState({
    name: "",
    url: "",
    default_branch: "",
    credential_id: "",
  });
  const [repoError, setRepoError] = useState("");
  // 工作区仓库（管理员）
  const [wsRepoOpen, setWsRepoOpen] = useState(false);
  const [wsRepoForm, setWsRepoForm] = useState({
    name: "",
    url: "",
    default_branch: "",
    credential_id: "",
  });
  const [wsRepoError, setWsRepoError] = useState("");

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

  function openAdd(scope: "mine" | "workspace" = "mine") {
    setCredScope(scope);
    setEditCred(null);
    setForm({ label: "", host: "", scheme: "https", username: "", secret: "" });
    setFormError("");
    setModalOpen(true);
  }

  function openEdit(cred: GitCredentialInfo, scope: "mine" | "workspace" = "mine") {
    setCredScope(scope);
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
    // 工作区凭据走管理员端点；个人凭据走 /api/me（权限由后端再校验一次）
    const base =
      credScope === "workspace"
        ? "/api/admin/git/workspace-credentials"
        : "/api/me/git-credentials";
    try {
      if (editCred) {
        if (form.secret) {
          // 替换 token 走覆盖写入（同一 host + scheme + username 命中唯一键）
          await api(base, {
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
          await api(`${base}/${editCred.id}`, {
            token,
            method: "PATCH",
            body: JSON.stringify({ label: label || editCred.label }),
          });
        }
      } else {
        await api(base, {
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

  async function removeCredential(cred: GitCredentialInfo, scope: "mine" | "workspace" = "mine") {
    const confirmText =
      scope === "workspace" ? C.wsCredDeleteConfirm(cred.label) : C.deleteConfirm(cred.label);
    if (!window.confirm(confirmText)) return;
    const url =
      scope === "workspace"
        ? `/api/admin/git/workspace-credentials/${cred.id}`
        : `/api/me/git-credentials/${cred.id}`;
    try {
      await api(url, { token, method: "DELETE" });
      await load();
      showToast(scope === "workspace" ? C.wsCredDisabled : C.deleted);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  /** 管理员登记工作区仓库：仓库与工作区凭据是两张表，这里只选已存在的凭据 */
  async function addWsRepo(e: FormEvent) {
    e.preventDefault();
    const url = wsRepoForm.url.trim();
    if (!url) {
      setWsRepoError(C.repoUrlRequired);
      return;
    }
    if (!wsRepoForm.credential_id) {
      setWsRepoError(C.wsRepoCred);
      return;
    }
    const fallbackName = url.split("/").pop()?.replace(/\.git$/, "") || "repo";
    try {
      await api("/api/admin/git/workspace-repos", {
        token,
        method: "POST",
        body: JSON.stringify({
          name: wsRepoForm.name.trim() || fallbackName,
          url,
          default_branch: wsRepoForm.default_branch.trim(),
          role_slots: [],
          credential_id: wsRepoForm.credential_id,
        }),
      });
      setWsRepoForm({ name: "", url: "", default_branch: "", credential_id: "" });
      setWsRepoOpen(false);
      setWsRepoError("");
      await load();
      showToast(C.wsRepoAdded);
    } catch (err) {
      setWsRepoError(err instanceof Error ? err.message : String(err));
    }
  }

  async function removeWsRepo(repo: WorkspaceRepoInfo) {
    if (!window.confirm(C.wsRepoDeleteConfirm(repo.name))) return;
    try {
      await api(`/api/admin/git/workspace-repos/${repo.id}`, { token, method: "DELETE" });
      await load();
      showToast(C.wsRepoRemoved);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function saveRepo(e: FormEvent) {
    e.preventDefault();
    const url = repoForm.url.trim();
    if (!url) {
      setRepoError(C.repoUrlRequired);
      return;
    }
    if (!repoForm.credential_id) {
      setRepoError(C.repoCredRequired);
      return;
    }
    const fallbackName = url.split("/").pop()?.replace(/\.git$/, "") || "repo";
    const body = {
      name: repoForm.name.trim() || fallbackName,
      url,
      default_branch: repoForm.default_branch.trim(),
      credential_id: repoForm.credential_id,
    };
    try {
      if (editRepoId) {
        await api(`/api/me/git/repos/${editRepoId}`, {
          token,
          method: "PATCH",
          body: JSON.stringify(body),
        });
      } else {
        await api("/api/me/git/repos", { token, method: "POST", body: JSON.stringify(body) });
      }
      setEditRepoId(null);
      setRepoForm({ name: "", url: "", default_branch: "", credential_id: "" });
      setRepoOpen(false);
      setRepoError("");
      await load();
      showToast(editRepoId ? C.repoUpdated : C.repoAdded);
    } catch (err) {
      setRepoError(err instanceof Error ? err.message : String(err));
    }
  }

  function openRepoEdit(repo: UserRepoInfo) {
    setEditRepoId(repo.id);
    setRepoForm({
      name: repo.name,
      url: repo.url,
      default_branch: repo.default_branch,
      credential_id: repo.credential_id || "",
    });
    setRepoError("");
    setRepoOpen(true);
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

  /** 列出全部凭据并标注是否匹配；不匹配的置灰，避免「凭空消失」 */
  const withMatch = useCallback(
    (list: GitCredentialInfo[], url: string) => {
      const host = hostOf(url);
      const scheme = schemeOf(url);
      return list
        .filter(isActive)
        .map((cred) => ({
          cred,
          match: Boolean(host) && cred.host === host && cred.scheme === scheme,
        }));
    },
    [],
  );

  const wsRepoCredOptions = useMemo(
    () => withMatch(wsCreds, wsRepoForm.url),
    [withMatch, wsCreds, wsRepoForm.url],
  );

  const repoCredOptions = useMemo(
    () => withMatch([...wsCreds, ...myCreds], repoForm.url),
    [withMatch, wsCreds, myCreds, repoForm.url],
  );

  /** 仓库 → 凭据匹配结果；工作区与自定义分开解析，不做优先级回退 */
  const matchRows = useMemo(() => {
    const allCreds = [...wsCreds, ...myCreds];
    /** 按仓库绑定的 credential_id 计算，且必须 host/scheme 一致（与执行期同规则） */
    const build = (repo: WorkspaceRepoInfo | UserRepoInfo, source: "workspace" | "custom") => {
      const host = hostOf(repo.url);
      const scheme = schemeOf(repo.url);
      const cred = allCreds.find((c) => c.id === repo.credential_id && isActive(c));
      const ok = Boolean(cred && cred.host === host && cred.scheme === scheme);
      return {
        key: `${source}-${repo.id}`,
        name: repo.name,
        source,
        host,
        credential: cred?.label || "",
        ok,
      };
    };
    return [
      ...wsRepos.map((repo) => build(repo, "workspace" as const)),
      ...myRepos.map((repo) => build(repo, "custom" as const)),
    ];
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
        <div className="stg-head-actions">
          {isAdmin && (
            <button type="button" className="stg-quiet" onClick={() => openAdd("workspace")}>
              ＋ {C.addWsCred}
            </button>
          )}
          <button type="button" className="stg-primary" onClick={() => openAdd("mine")}>
            ＋ {C.addCred}
          </button>
        </div>
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
                <span className="ro">{isAdmin ? C.wsAdminWritable : C.wsReadonly}</span>
              </div>
              <div className="stg-sealed-list">
                {wsCreds.map((cred) => (
                  <article key={cred.id} className="stg-sealed">
                    <span className="stg-stamp">{isAdmin ? "ADMIN" : "READ ONLY"}</span>
                    {isAdmin && (
                      <div className="stg-sealed-ops">
                        <button type="button" onClick={() => openEdit(cred, "workspace")}>
                          {C.edit}
                        </button>
                        <button
                          type="button"
                          className="danger"
                          onClick={() => void removeCredential(cred, "workspace")}
                        >
                          {C.wsCredDelete}
                        </button>
                      </div>
                    )}
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
              <p className="stg-note">{C.wsNote}</p>
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
              <button
                type="button"
                className="stg-quiet"
                onClick={() => {
                  if (repoOpen) {
                    setRepoOpen(false);
                    setEditRepoId(null);
                    setRepoForm({ name: "", url: "", default_branch: "", credential_id: "" });
                  } else {
                    setRepoOpen(true);
                  }
                }}
              >
                {repoOpen ? C.cancel : `＋ ${C.addRepo}`}
              </button>
            </div>

            {repoOpen && (
              <form className="stg-repo-form" onSubmit={saveRepo}>
                <p className="stg-form-title">
                  {editRepoId ? C.repoEditTitle : C.repoAddTitle}
                </p>
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
                <label>
                  <span>{C.repoCred}</span>
                  <select
                    value={repoForm.credential_id}
                    onChange={(e) =>
                      setRepoForm({ ...repoForm, credential_id: e.target.value })
                    }
                  >
                    <option value="">—</option>
                    <optgroup label={lang === "zh" ? "工作区凭据" : "Workspace credentials"}>
                      {repoCredOptions
                        .filter((o) => o.cred.scope === "workspace")
                        .map(({ cred, match }) => (
                          <option key={cred.id} value={cred.id} disabled={!match}>
                            {cred.label} · {cred.host}
                            {match ? "" : ` · ${C.credHostMismatch}`}
                          </option>
                        ))}
                    </optgroup>
                    <optgroup label={lang === "zh" ? "我的凭据" : "My credentials"}>
                      {repoCredOptions
                        .filter((o) => o.cred.scope === "user")
                        .map(({ cred, match }) => (
                          <option key={cred.id} value={cred.id} disabled={!match}>
                            {cred.label} · {cred.host}
                            {match ? "" : ` · ${C.credHostMismatch}`}
                          </option>
                        ))}
                    </optgroup>
                  </select>
                  {repoForm.url.trim() && !repoCredOptions.some((o) => o.match) && (
                    <em>{C.repoNoCred}</em>
                  )}
                </label>
                <button type="submit" className="stg-primary">
                  {editRepoId ? C.save : C.addRepo}
                </button>
                {repoError && <p className="stg-error inline">{repoError}</p>}
              </form>
            )}

            <div className="stg-repo-list">
              {myRepos.map((repo) => {
                const cred = [...wsCreds, ...myCreds].find(
                  (c) => c.id === repo.credential_id && isActive(c),
                );
                return (
                  <div key={repo.id} className="stg-repo-row">
                    <div className="nm">{repo.name}</div>
                    <div className="url">{repo.url}</div>
                    <div className="branch">{repo.default_branch || "—"}</div>
                    <div className={`cred${cred ? "" : " miss"}`}>
                      {cred ? cred.label : C.repoUnbound}
                    </div>
                    <button type="button" className="stg-link" onClick={() => openRepoEdit(repo)}>
                      {C.repoEdit}
                    </button>
                    <button type="button" className="danger" onClick={() => void removeRepo(repo)}>
                      {C.remove}
                    </button>
                  </div>
                );
              })}
              {!myRepos.length && <div className="stg-empty">{C.emptyRepos}</div>}
            </div>
          </section>

          {isAdmin && (
            <section className="stg-sec">
              <div className="stg-sec-head">
                <div>
                  <h3>{C.wsReposTitle}</h3>
                  <p>{C.wsReposNote}</p>
                </div>
                <span className="sp" />
                <button
                  type="button"
                  className="stg-quiet"
                  onClick={() => setWsRepoOpen((v) => !v)}
                >
                  {wsRepoOpen ? C.cancel : `＋ ${C.addWsRepo}`}
                </button>
              </div>

              {wsRepoOpen && (
                <form className="stg-repo-form ws-repo-form" onSubmit={addWsRepo}>
                  <label>
                    <span>{C.repoName}</span>
                    <input
                      value={wsRepoForm.name}
                      placeholder={C.repoNamePh}
                      onChange={(e) => setWsRepoForm({ ...wsRepoForm, name: e.target.value })}
                    />
                  </label>
                  <label className="wide">
                    <span>{C.repoUrl}</span>
                    <input
                      value={wsRepoForm.url}
                      placeholder={C.repoUrlPh}
                      onChange={(e) => setWsRepoForm({ ...wsRepoForm, url: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>{C.repoBranch}</span>
                    <input
                      value={wsRepoForm.default_branch}
                      placeholder={C.repoBranchPh}
                      onChange={(e) =>
                        setWsRepoForm({ ...wsRepoForm, default_branch: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    <span>{C.wsRepoCred}</span>
                    <select
                      value={wsRepoForm.credential_id}
                      onChange={(e) =>
                        setWsRepoForm({ ...wsRepoForm, credential_id: e.target.value })
                      }
                    >
                      <option value="">—</option>
                      {wsRepoCredOptions.map(({ cred, match }) => (
                        <option key={cred.id} value={cred.id} disabled={!match}>
                          {cred.label} · {cred.host}
                          {match ? "" : ` · ${C.credHostMismatch}`}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button type="submit" className="stg-primary">
                    {C.addWsRepo}
                  </button>
                  {wsRepoError && <p className="stg-error inline">{wsRepoError}</p>}
                </form>
              )}

              <div className="stg-repo-list">
                {wsRepos.map((repo) => {
                  const cred = wsCreds.find((c) => c.id === repo.credential_id);
                  return (
                    <div key={repo.id} className="stg-repo-row">
                      <div className="nm">{repo.name}</div>
                      <div className="url">{repo.url}</div>
                      <div className="branch">{repo.default_branch || "—"}</div>
                      <div className={`cred${cred ? "" : " miss"}`}>
                        {cred?.label || "—"}
                      </div>
                      <button
                        type="button"
                        className="danger"
                        onClick={() => void removeWsRepo(repo)}
                      >
                        {C.wsCredDelete}
                      </button>
                    </div>
                  );
                })}
                {!wsRepos.length && <div className="stg-empty">{C.wsReposNote}</div>}
              </div>
            </section>
          )}

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
                {!matchRows.length && (
                  <tr>
                    <td
                      colSpan={5}
                      style={{ padding: "18px 12px", color: "var(--muted)", fontSize: 12 }}
                    >
                      {C.matchingEmpty}
                    </td>
                  </tr>
                )}
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
          aria-label={
            credScope === "workspace"
              ? editCred
                ? C.wsCredEditTitle
                : C.wsCredAddTitle
              : editCred
                ? C.editTitle
                : C.addTitle
          }
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closeModal();
          }}
        >
          <form className="stg-sheet" onSubmit={saveCredential} noValidate>
            <div className="stg-sheet-head">
              <div>
                <h3>
                  {credScope === "workspace"
                    ? editCred
                      ? C.wsCredEditTitle
                      : C.wsCredAddTitle
                    : editCred
                      ? C.editTitle
                      : C.addTitle}
                </h3>
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
