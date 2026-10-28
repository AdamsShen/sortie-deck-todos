import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  adminUpdateUser,
  api,
  createKnowledgeBase,
  deleteKnowledgeBase,
  deleteOwnAccount,
  importKnowledgeFile,
  listHarnesses,
  listKnowledgeBases,
  refreshKnowledgeParseStatus,
  listMissionShares,
  listSubRooms,
  registerUser,
  updateInitiativeHarness,
} from "./api";
import { OperatorChip } from "./components/OperatorChip";
import { PipelineEditor } from "./components/PipelineEditor";
import { ChatComposer, SparkleIcon, type MentionOption } from "./components/ChatComposer";
import { MarkdownPreview, looksLikeMarkdown } from "./components/MarkdownPreview";
import { NewTaskPage } from "./components/NewTaskPage";
import { RoleAvatar, AVATAR_PRESET_OPTIONS } from "./components/RoleAvatar";
import { StageInspect } from "./components/StageInspect";
import { PipelineRail, railFromInitiative } from "./components/PipelineRail";
import { ForgeReview } from "./components/ForgeReview";
import { PersonaEditor, type PersonaMode } from "./components/PersonaEditor";
import { SubRoomPanel } from "./components/SubRoomPanel";
import { ShareManager } from "./components/ShareManager";
import { DecisionCenter } from "./components/DecisionCenter";
import { unlockSfx, playSfx, loadSfxMuted, setSfxMuted, loadBgmMuted, setBgmMuted, startBgm } from "./sfx";
import { categoryLabel, statusLabel, t, type I18nKey } from "./i18n";
import { emptyPersonaSpec, parsePersonaText } from "./persona";
import { lsGet, lsRemove, lsSet } from "./storage";
import {
  BG_STORAGE_KEY,
  THEMES,
  THEME_STORAGE_KEY,
  loadBackground,
  loadTheme,
  type ThemeId,
} from "./themes";
import type {
  AgentSlot,
  Dashboard,
  HarnessInfo,
  Initiative,
  InitiativeStatus,
  KnowledgeBaseInfo,
  KnowledgeBaseScope,
  KnowledgeDoc,
  Lang,
  LoginResponse,
  MemoryEntry,
  MemoryKind,
  MemoryScope,
  MissionShare,
  MissionWorkspace,
  PipelineTrackInfo,
  PipelineStageInfo,
  PersonaSpecShape,
  PublishedAgent,
  PublicUser,
  RoomMessage,
  ToolkitItem,
  ToolkitKind,
  ToolRuntime,
  McpTransport,
  SkillScript,
  MarketSkill,
  McpTestResult,
  SkillDraft,
  UserRole,
  View,
  Visibility,
} from "./types";
import { ApiError, TOOLKIT_CATEGORIES } from "./types";

const CATEGORY_I18N: Record<string, I18nKey> = {
  engineering: "catEngineering",
  product: "catProduct",
  qa: "catQa",
  ops: "catOps",
  knowledge: "catKnowledge",
  browser: "catBrowser",
  office: "catOffice",
};

// 知识库文件导入：与后端白名单一致的扩展名集合与 accept 串
const KB_IMPORT_EXTS = new Set([
  "md", "markdown", "txt", "csv", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "pdf",
]);
const KB_IMPORT_ACCEPT = KB_IMPORT_EXTS.size
  ? Array.from(KB_IMPORT_EXTS).map((x) => `.${x}`).join(",")
  : "";

const AGENT_SLOTS: AgentSlot[] = [
  "product",
  "design",
  "eng",
  "eng_ios",
  "eng_android",
  "eng_web",
  "eng_backend",
  "eng_agent",
  "qa",
  "deploy",
];

const SLOT_I18N: Record<AgentSlot, I18nKey> = {
  product: "slotProduct",
  design: "slotDesign",
  eng: "slotEng",
  eng_ios: "slotEngIos",
  eng_android: "slotEngAndroid",
  eng_web: "slotEngWeb",
  eng_backend: "slotEngBackend",
  eng_agent: "slotEngAgent",
  qa: "slotQa",
  deploy: "slotDeploy",
};

function errMsg(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

// 用户管理页头像底色：按用户名哈希从固定色板取色，同一用户颜色稳定
const AVATAR_PALETTE = ["#3b5bdb", "#0e9f6e", "#8a5a2b", "#7a3b8a", "#b3542e", "#133a5e", "#2b6a8a", "#5a6b3a"];
function avatarColor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i += 1) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[h % AVATAR_PALETTE.length]!;
}

function displayTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}

export default function App() {
  const [lang, setLang] = useState<Lang>(() => (lsGet("sortie_lang") as Lang) || "zh");
  const [token, setToken] = useState(() => lsGet("sortie_token") || "");
  const [user, setUser] = useState<PublicUser | null>(null);
  const [view, setView] = useState<View>("home");
  const [items, setItems] = useState<Initiative[]>([]);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Initiative | null>(null);
  const [harnesses, setHarnesses] = useState<HarnessInfo[]>([]);
  const [harnessPick, setHarnessPick] = useState("");
  const [messages, setMessages] = useState<RoomMessage[]>([]);
  const [filter, setFilter] = useState<"all" | InitiativeStatus>("all");
  const [stageCatalog, setStageCatalog] = useState<PipelineStageInfo[]>([]);
  const [pipelineDraft, setPipelineDraft] = useState<PipelineStageInfo[]>([]);
  const [pipelineEditing, setPipelineEditing] = useState(false);
  const [toolkitCatalog, setToolkitCatalog] = useState<ToolkitItem[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [artifactPreview, setArtifactPreview] = useState("");
  const [artifactPreviewPath, setArtifactPreviewPath] = useState("");
  const [artifactPreviewKind, setArtifactPreviewKind] = useState("");
  const [workspace, setWorkspace] = useState<MissionWorkspace | null>(null);
  const [inspectStage, setInspectStage] = useState<{ id: string; label: string } | null>(null);
  const [avatarPickRoleId, setAvatarPickRoleId] = useState<string | null>(null);
  const [crewOpen, setCrewOpen] = useState(false);
  const [lootOpen, setLootOpen] = useState(false);
  const [briefOpen, setBriefOpen] = useState(false);
  const [chatFullscreen, setChatFullscreen] = useState(false);
  const [subroomsOpen, setSubroomsOpen] = useState(false);
  // 任务详情页共享抽屉（只读成员列表，管理入口在任务列表页弹窗）
  const [sharesOpen, setSharesOpen] = useState(false);
  const [shares, setShares] = useState<MissionShare[]>([]);
  // 任务列表页共享管理弹窗：值为任务 id
  const [shareModalId, setShareModalId] = useState<string | null>(null);
  // Bumped on every sub-room SSE event so the panel re-pulls records/transcript
  const [subroomSignal, setSubroomSignal] = useState(0);
  const [subroomCount, setSubroomCount] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [booted, setBooted] = useState(false);
  const [loginForm, setLoginForm] = useState({ username: "admin", password: "admin123" });
  // 登录页登录/注册模式切换；注册成功后展示「等待审核」提示
  const [regMode, setRegMode] = useState(false);
  const [regForm, setRegForm] = useState({ username: "", password: "", display_name: "" });
  const [regDone, setRegDone] = useState(false);
  const [users, setUsers] = useState<PublicUser[]>([]);
  // 用户管理页搜索框：按用户名 / 显示名即时过滤（列表为全量拉取，前端过滤即可）
  const [userSearch, setUserSearch] = useState("");
  const [newUser, setNewUser] = useState<{
    username: string;
    password: string;
    display_name: string;
    role: UserRole;
  }>({
    username: "",
    password: "",
    display_name: "",
    role: "member",
  });
  const [kbDocs, setKbDocs] = useState<KnowledgeDoc[]>([]);
  const [kbQuery, setKbQuery] = useState("");
  // 知识库改版（仿记忆页）：库实体卡片 + 分类筛选 + 新建（内置导入）/ 导入弹窗
  const [kbBases, setKbBases] = useState<KnowledgeBaseInfo[]>([]);
  const [kbBaseFilter, setKbBaseFilter] = useState<"all" | KnowledgeBaseScope>("all");
  const [kbBaseQuery, setKbBaseQuery] = useState("");
  const [kbBaseId, setKbBaseId] = useState<string | null>(null);
  const [kbCreateOpen, setKbCreateOpen] = useState(false);
  const [kbImportOpen, setKbImportOpen] = useState(false);
  const [kbTasks, setKbTasks] = useState<Initiative[]>([]);
  const [kbDocDetail, setKbDocDetail] = useState<KnowledgeDoc | null>(null);
  const kbCreateFileRef = useRef<HTMLInputElement | null>(null);
  const kbImportFileRef = useRef<HTMLInputElement | null>(null);
  const [kbBaseForm, setKbBaseForm] = useState<{
    name: string;
    description: string;
    scope: KnowledgeBaseScope;
    task_id: string;
  }>({ name: "", description: "", scope: "user", task_id: "" });
  const [kbPendingFiles, setKbPendingFiles] = useState<
    { uid: string; file: File; status: "queued" | "uploading" | "done" | "failed"; message?: string }[]
  >([]);
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  // 记忆库看板（方案三）：筛选/搜索/弹窗写入/删除撤销
  const [memModalOpen, setMemModalOpen] = useState(false);
  const [memQuery, setMemQuery] = useState("");
  const [memScopeFilter, setMemScopeFilter] = useState<"all" | "workspace" | "user">("all");
  const [memKindFilter, setMemKindFilter] = useState<"" | MemoryKind>("");
  const [memToast, setMemToast] = useState(false);
  const memUndoRef = useRef<MemoryEntry | null>(null);
  const memToastTimer = useRef<number | null>(null);
  // 「我的」定向共享：所有任务 / 指定任务 / 指定 Agent（scope_id 复用 memForm.scope_id 存目标 id）
  const [memTarget, setMemTarget] = useState<"all" | "mission" | "agent">("all");
  // 任务详情页「记忆」面板（绑定索引 + 执行中读取记录）
  const [memPanelOpen, setMemPanelOpen] = useState(false);
  const [missionMemories, setMissionMemories] = useState<{
    bound: MemoryEntry[];
    read_ids: string[];
  } | null>(null);
  // 记忆卡片详情（简介作标题 + 全文渲染）与 md 导入
  const [memDetail, setMemDetail] = useState<MemoryEntry | null>(null);
  const memImportRef = useRef<HTMLInputElement | null>(null);
  const [memForm, setMemForm] = useState<{
    scope: MemoryScope;
    scope_id: string;
    kind: MemoryKind;
    content: string;
    tags: string;
  }>({
    scope: "workspace",
    scope_id: "global",
    kind: "fact",
    content: "",
    tags: "",
  });
  // 记忆表单「增强提示词」：优化中 spinner；成功覆盖后保留原文供撤销
  const [memEnhancing, setMemEnhancing] = useState(false);
  const [memEnhancePrev, setMemEnhancePrev] = useState<string | null>(null);
  // 发布角色的人设：结构化视图 + 编辑模式（结构化 / 原文）
  const [agentSpec, setAgentSpec] = useState<PersonaSpecShape>(emptyPersonaSpec);
  const [agentPersonaMode, setAgentPersonaMode] = useState<PersonaMode>("structured");
  const [agentList, setAgentList] = useState<PublishedAgent[]>([]);
  const [agentEditing, setAgentEditing] = useState(false);
  const [agentSelectedId, setAgentSelectedId] = useState<string | null>(null);
  // V2 作战室：名册网格顶部的可见性分段过滤器（all/public/private）
  const [agentVisFilter, setAgentVisFilter] = useState<"all" | Visibility>("all");
  const [agentForm, setAgentForm] = useState<{
    title: string;
    slot: AgentSlot;
    summary: string;
    persona: string;
    tags: string;
    visibility: Visibility;
    toolkit_ids: string[];
    avatar: string;
  }>({
    title: "",
    slot: "product",
    summary: "",
    persona: "",
    tags: "",
    visibility: "public",
    toolkit_ids: [],
    avatar: "",
  });
  // V-C 装配线：工具绑定弹框状态（草稿勾选在弹框内暂存，点「确定」才写回 agentForm）
  const [toolPickerOpen, setToolPickerOpen] = useState(false);
  const [toolPickDraft, setToolPickDraft] = useState<string[]>([]);
  const [toolQuery, setToolQuery] = useState("");
  const [toolKindFilter, setToolKindFilter] = useState<"" | ToolkitItem["kind"]>("");
  const [sfxMuted, setSfxMutedState] = useState(() => loadSfxMuted());
  const [bgmMuted, setBgmMutedState] = useState(() => loadBgmMuted());
  const agentAvatarRef = useRef<HTMLInputElement | null>(null);
  const [theme, setTheme] = useState<ThemeId>(() => loadTheme());
  const [bgUrl, setBgUrl] = useState(() => loadBackground());
  const [bgDraft, setBgDraft] = useState(() => loadBackground());
  const [themeOpen, setThemeOpen] = useState(false);
  const [toolkitTab, setToolkitTab] = useState<ToolkitKind>("tool");
  const [toolkitCategory, setToolkitCategory] = useState<string>("");
  const [toolkitQuery, setToolkitQuery] = useState("");
  const [toolkitItems, setToolkitItems] = useState<ToolkitItem[]>([]);
  const [toolkitSelectedId, setToolkitSelectedId] = useState<string | null>(null);
  const [toolkitEditing, setToolkitEditing] = useState(false);
  const [toolkitForm, setToolkitForm] = useState({
    name: "",
    summary: "",
    runtime: "cli" as ToolRuntime,
    endpoint: "",
    transport: "stdio" as McpTransport,
    command: "",
    url: "",
    body: "",
    category: "engineering",
    tags: "",
    featured: false,
    enabled: true,
    load: "defer" as "always" | "defer",
    scripts: [] as SkillScript[],
  });
  // --- V4 工具箱：发现带 / 弹窗 / 连接测试 / skill-install 优化 ---
  const [marketSkills, setMarketSkills] = useState<MarketSkill[]>([]);
  const [toolkitMounts, setToolkitMounts] = useState<Record<string, number>>({});
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [skillModalOpen, setSkillModalOpen] = useState(false);
  const [skillMode, setSkillMode] = useState<"remote" | "local">("remote");
  const [localMode, setLocalMode] = useState<"upload" | "write">("upload");
  const [skillUrl, setSkillUrl] = useState("");
  const [skillModalMsg, setSkillModalMsg] = useState("");
  const [skillUpload, setSkillUpload] = useState<{ file: File; name: string } | null>(null);
  const [writeForm, setWriteForm] = useState({ name: "", category: "engineering", body: "" });
  const [optimizePhase, setOptimizePhase] = useState<"idle" | "running" | "review">("idle");
  const [skillDraft, setSkillDraft] = useState<SkillDraft | null>(null);
  const [toolModalOpen, setToolModalOpen] = useState(false);
  const [toolForm, setToolForm] = useState({
    name: "",
    summary: "",
    category: "engineering",
    runtime: "cli" as ToolRuntime,
    endpoint: "",
    load: "defer" as "always" | "defer",
  });
  const [mcpModalOpen, setMcpModalOpen] = useState(false);
  const [mcpForm, setMcpForm] = useState({
    name: "",
    transport: "stdio" as McpTransport,
    url: "",
    auth: "bearer" as "bearer" | "oauth",
    token: "",
    command: "",
    category: "engineering",
  });
  const [mcpPkg, setMcpPkg] = useState<{ file: File | null; phase: "idle" | "importing" | "ready" }>({
    file: null,
    phase: "idle",
  });
  const [mcpTest, setMcpTest] = useState<{ state: "idle" | "testing" | "ok" | "fail"; msg: string }>({
    state: "idle",
    msg: "",
  });
  const [mcpItemTest, setMcpItemTest] = useState<{ id: string; state: string; msg: string } | null>(null);
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const avatarFileRef = useRef<HTMLInputElement | null>(null);

  const tr = useCallback((key: I18nKey | string) => t(lang, key), [lang]);

  // 记忆类型 → i18n key
  const memKindLabel: Record<MemoryKind, I18nKey> = {
    fact: "kindFact",
    preference: "kindPreference",
    episode: "kindEpisode",
    lesson: "kindLesson",
  };

  const filteredMemories = useMemo(() => {
    const q = memQuery.trim().toLowerCase();
    return memories.filter((m) => {
      // 「我的」含全部个人域：user / 指定任务(mission) / 指定 Agent(agent)
      if (memScopeFilter === "user" && !["user", "mission", "agent"].includes(m.scope)) return false;
      if (memScopeFilter === "workspace" && m.scope !== "workspace") return false;
      if (memKindFilter && m.kind !== memKindFilter) return false;
      if (q && !m.content.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [memories, memQuery, memScopeFilter, memKindFilter]);

  // 记忆作用域目标显示名（任务 / Agent 用标题回显，找不到退化为 id 前缀）
  const memTargetLabel = useCallback(
    (m: MemoryEntry): string => {
      if (m.scope === "workspace") return tr("scopeGlobalShort");
      if (m.scope === "user") return tr("scopeMineShort");
      if (m.scope === "mission")
        return `${tr("scopeMission")} · ${items.find((i) => i.id === m.scope_id)?.title || m.scope_id.slice(0, 10)}`;
      return `${tr("scopeAgent")} · ${agentList.find((a) => a.id === m.scope_id)?.title || m.scope_id.slice(0, 10)}`;
    },
    [tr, items, agentList]
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    if (!chatFullscreen) return;
    const onKey = (e: Event) => {
      if ((e as globalThis.KeyboardEvent).key === "Escape") setChatFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [chatFullscreen]);

  useEffect(() => {
    localStorage.setItem(BG_STORAGE_KEY, bgUrl);
    document.documentElement.classList.toggle("has-art-bg", Boolean(bgUrl));
    return () => document.documentElement.classList.remove("has-art-bg");
  }, [bgUrl]);

  useEffect(() => {
    const unlock = () => unlockSfx();
    window.addEventListener("pointerdown", unlock, { once: true, capture: true });
    window.addEventListener("keydown", unlock, { once: true, capture: true });
    return () => {
      window.removeEventListener("pointerdown", unlock, true);
      window.removeEventListener("keydown", unlock, true);
    };
  }, []);

  function applyBgUrl() {
    setBgUrl(bgDraft.trim());
  }

  function clearBg() {
    setBgUrl("");
    setBgDraft("");
  }

  function onBgFile(file: File | undefined) {
    if (!file) return;
    const okType =
      file.type.startsWith("image/") ||
      /\.(png|jpe?g|webp|gif)$/i.test(file.name);
    if (!okType) {
      setError(lang === "zh" ? "仅支持图片（含 webp）" : "Images only (including webp)");
      return;
    }
    if (file.size > 4_000_000) {
      setError(lang === "zh" ? "背景图请小于 4MB" : "Background image must be under 4MB");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      setBgUrl(result);
      setBgDraft(result.startsWith("data:") ? "" : result);
    };
    reader.readAsDataURL(file);
  }

  async function uploadRoleAvatarFile(roleId: string, file: File | undefined) {
    if (!token || !selectedId || !file) return;
    setBusy(true);
    setError("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`/api/initiatives/${selectedId}/roles/${roleId}/avatar`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: fd,
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(detail || res.statusText);
      }
      const data = (await res.json()) as { initiative?: Initiative; avatar?: string };
      if (data.initiative) {
        setDetail(data.initiative);
        setItems((prev) => prev.map((i) => (i.id === data.initiative!.id ? data.initiative! : i)));
      }
      setAvatarPickRoleId(null);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  const atmosphere = (
    <div className="atmosphere" aria-hidden="true">
      {bgUrl ? (
        <div
          className={`custom-bg on`}
          style={{ backgroundImage: `url(${bgUrl})` }}
        />
      ) : null}
      <div className="grid-floor" />
      <div className="scanline" />
      {!bgUrl ? (
        <>
          <div className="orb orb-a" />
          <div className="orb orb-b" />
        </>
      ) : (
        <div className="art-scrim" />
      )}
    </div>
  );

  // 当前主题的展示名，用于面板页脚状态（themeControls 内复用）
  const currentTheme = THEMES.find((t) => t.id === theme);

  const themeControls = (
    <div className="theme-dock">
      <button
        type="button"
        className={`op-chip ${themeOpen ? "on" : ""}`}
        onClick={() => {
          unlockSfx();
          playSfx("click");
          setThemeOpen((v) => !v);
        }}
        aria-label={tr("theme")}
        title={tr("theme")}
      >
        <span className="op-chip-ico" aria-hidden>
          ◈
        </span>
        <span className="op-chip-txt">{tr("theme")}</span>
      </button>
      {themeOpen && (
        <div className="theme-panel" role="dialog" aria-label={tr("appearance")}>
          <div className="ap-head">
            <h2>{tr("appearance")}</h2>
            <span>{tr("appearanceLive")}</span>
          </div>
          <div className="ap-group">
            <div className="g-label">{tr("themeGroup")}</div>
            <div className="theme-cards">
              {THEMES.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="tcard"
                  data-mini={item.id}
                  aria-pressed={theme === item.id}
                  onClick={() => {
                    playSfx("toggle");
                    setTheme(item.id);
                  }}
                >
                  <span className="tcard-view" aria-hidden="true">
                    <span className="tv-bar">
                      <i />
                      <i />
                      <i />
                    </span>
                    <span className="tv-title" />
                    <span className="tv-line" />
                    <span className="tv-card">
                      <span className="tv-line" style={{ width: "70%" }} />
                      <span className="tv-line" style={{ width: "45%" }} />
                    </span>
                  </span>
                  <span className="tcard-meta">
                    {lang === "zh" ? item.labelZh : item.labelEn}
                    {item.id === "paper" && <em>{tr("themeDefault")}</em>}
                  </span>
                </button>
              ))}
            </div>
          </div>
          <div className="ap-group">
            <div className="g-label">{tr("wallpaperGroup")}</div>
            <div className="wall-row">
              <input
                type="text"
                value={bgDraft}
                placeholder={tr("bgUrlPlaceholder")}
                onChange={(e) => setBgDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    applyBgUrl();
                  }
                }}
              />
              <button type="button" className="primary" onClick={applyBgUrl}>
                {tr("bgApply")}
              </button>
            </div>
            <div className="wall-links">
              <button type="button" onClick={() => fileRef.current?.click()}>
                {tr("bgUploadLink")}
              </button>
              <button type="button" onClick={clearBg}>
                {tr("bgClearLink")}
              </button>
            </div>
          </div>
          <div className="ap-group">
            <div className="g-label">{tr("soundGroup")}</div>
            <div className="snd-list">
              <label className="snd-item">
                <span className="name">
                  {tr("sfxName")}
                  <small>{tr("sfxHint")}</small>
                </span>
                <span className="pill">
                  <input
                    type="checkbox"
                    checked={!sfxMuted}
                    onChange={(e) => {
                      unlockSfx();
                      const on = e.target.checked;
                      setSfxMuted(!on);
                      setSfxMutedState(!on);
                      if (on) playSfx("toggle");
                    }}
                  />
                  <span className="tk" />
                  <span className="kn" />
                </span>
              </label>
              <label className="snd-item">
                <span className="name">
                  {tr("bgmName")}
                  <small>{tr("bgmHint")}</small>
                </span>
                <span className="pill">
                  <input
                    type="checkbox"
                    checked={!bgmMuted}
                    onChange={(e) => {
                      unlockSfx();
                      startBgm();
                      const on = e.target.checked;
                      setBgmMuted(!on);
                      setBgmMutedState(!on);
                      if (on) playSfx("ok");
                      else playSfx("toggle");
                    }}
                  />
                  <span className="tk" />
                  <span className="kn" />
                </span>
              </label>
            </div>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/*,.webp,image/webp"
            hidden
            onChange={(e) => onBgFile(e.target.files?.[0])}
          />
          <div className="ap-foot">
            <span>{tr("themeLocalNote")}</span>
            <span>{currentTheme ? (lang === "zh" ? currentTheme.labelZh : currentTheme.labelEn) : ""}</span>
          </div>
        </div>
      )}
    </div>
  );

  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) || detail,
    [items, selectedId, detail]
  );

  // Role key → display title, used by the sub-room member chips
  const roleTitles = useMemo(() => {
    const map: Record<string, string> = {};
    for (const r of selected?.roles || []) map[r.role] = r.title;
    return map;
  }, [selected]);

  const speakingRoles = useMemo(() => {
    const set = new Set<string>();
    const recent = (messages || []).slice(-12);
    for (const m of recent) {
      if (m.msg_type === "consult" || m.msg_type === "consult_reply") {
        if (m.role) set.add(String(m.role));
        const to = m.meta?.to_role;
        const from = m.meta?.from_role;
        if (typeof to === "string") set.add(to);
        if (typeof from === "string") set.add(from);
      }
    }
    return set;
  }, [messages]);

  async function setRoleAvatar(roleId: string, avatar: string) {
    if (!token || !selectedId) return;
    setBusy(true);
    setError("");
    try {
      const updated = await api<Initiative>(`/api/initiatives/${selectedId}/roles/${roleId}`, {
        method: "PATCH",
        token,
        body: JSON.stringify({ avatar }),
      });
      setDetail(updated);
      setItems((prev) => prev.map((i) => (i.id === updated.id ? updated : i)));
      setAvatarPickRoleId(null);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  const mentionOptions = useMemo<MentionOption[]>(() => {
    const base: MentionOption[] = [
      { id: "product", label: lang === "zh" ? "产品" : "Product", hint: "PRD / scope" },
      { id: "design", label: lang === "zh" ? "设计" : "Design", hint: "UI / UX" },
      { id: "eng", label: lang === "zh" ? "工程" : "Engineering", hint: "general build" },
      { id: "eng_ios", label: "iOS", hint: "Swift / SwiftUI" },
      { id: "eng_android", label: "Android", hint: "Kotlin" },
      { id: "eng_web", label: "Web", hint: "frontend" },
      { id: "eng_backend", label: lang === "zh" ? "后端" : "Backend", hint: "API / services" },
      { id: "eng_agent", label: "Agent", hint: "tools / MCP / eval" },
      { id: "qa", label: lang === "zh" ? "测试" : "QA", hint: "acceptance" },
      { id: "deploy", label: lang === "zh" ? "发布" : "Deploy", hint: "preview / release" },
      { id: "all", label: lang === "zh" ? "全体编制" : "Entire squad", hint: "broadcast" },
    ];
    const seen = new Set(base.map((b) => b.id));
    for (const r of selected?.roles || []) {
      const id = r.role;
      if (!id || seen.has(id)) continue;
      seen.add(id);
      base.push({ id, label: r.title || id, hint: r.executor });
    }
    return base;
  }, [lang, selected?.roles]);

  const commandOptions = useMemo<MentionOption[]>(() => {
    if (lang === "zh") {
      return [
        { id: "confirm", label: "确认航线", hint: "确认规划官提案后可出击" },
        { id: "start", label: "出击", hint: "启动已确认流水线" },
        { id: "approve", label: "门禁通过", hint: "HITL 放行" },
        { id: "reject", label: "门禁驳回", hint: "HITL 打回" },
        { id: "rewrite", label: "改写指令", hint: "后接修订说明" },
        { id: "stop", label: "停止", hint: "终止当前运行" },
      ];
    }
    return [
      { id: "confirm", label: "Confirm pipeline", hint: "lock stages then start" },
      { id: "start", label: "Start sortie", hint: "run confirmed pipeline" },
      { id: "approve", label: "Approve gate", hint: "HITL continue" },
      { id: "reject", label: "Reject gate", hint: "HITL send back" },
      { id: "rewrite", label: "Rewrite", hint: "append new instruction" },
      { id: "stop", label: "Stop", hint: "halt the run" },
    ];
  }, [lang]);

  const filtered = useMemo(() => {
    if (filter === "all") return items;
    return items.filter((i) => i.status === filter);
  }, [items, filter]);

  useEffect(() => {
    lsSet("sortie_lang", lang);
  }, [lang]);

  useEffect(() => {
    requestAnimationFrame(() => setBooted(true));
  }, []);

  useEffect(() => {
    if (!token) {
      setUser(null);
      return;
    }
    api<PublicUser>("/api/auth/me", { token })
      .then(setUser)
      .catch(() => {
        setToken("");
        lsRemove("sortie_token");
      });
  }, [token]);

  const refreshList = useCallback(async () => {
    if (!token) return;
    const data = await api<Initiative[]>("/api/initiatives", { token });
    setItems(data);
  }, [token]);

  const refreshDash = useCallback(async () => {
    if (!token) return;
    const data = await api<Dashboard>("/api/dashboard", { token });
    setDash(data);
  }, [token]);

  useEffect(() => {
    if (!token) return;
    refreshList().catch((e) => setError(errMsg(e)));
    refreshDash().catch(() => {});
    api<PublishedAgent[]>("/api/agents", { token })
      .then(setAgentList)
      .catch(() => {});
  }, [token, refreshList, refreshDash]);

  const prevArtCountRef = useRef(0);

  useEffect(() => {
    setInspectStage(null);
    setArtifactPreview("");
    setArtifactPreviewPath("");
    setArtifactPreviewKind("");
    setWorkspace(null);
    setSharesOpen(false);
    setShares([]);
    setMemPanelOpen(false);
    setMissionMemories(null);
    prevArtCountRef.current = 0;
  }, [selectedId]);

  // 打开详情页记忆面板：拉取生成时绑定的记忆 + 执行中读取记录
  useEffect(() => {
    if (!token || !memPanelOpen || !selectedId) return;
    let cancelled = false;
    api<{ bound: MemoryEntry[]; read_ids: string[] }>(
      `/api/initiatives/${selectedId}/memories`,
      { token }
    )
      .then((d) => {
        if (!cancelled) setMissionMemories({ bound: d.bound || [], read_ids: d.read_ids || [] });
      })
      .catch((e) => {
        if (!cancelled) setError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [token, memPanelOpen, selectedId]);

  // 当前账号是否为任务创建者；无归属任务仅 admin 视为 owner（与后端 _ensure_mission_owner 一致）
  const isMissionOwner = Boolean(
    user && (selected?.created_by === user.id || user.role === "admin")
  );

  // 打开详情页共享抽屉时拉取最新共享成员列表（只读展示）
  useEffect(() => {
    if (!token || !sharesOpen || !selectedId) return;
    let cancelled = false;
    listMissionShares(token, selectedId)
      .then((d) => {
        if (!cancelled) setShares(d || []);
      })
      .catch((e) => {
        if (!cancelled) setError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [token, sharesOpen, selectedId]);

  const artCount = (selected?.artifacts || []).length;

  useEffect(() => {
    if (!selectedId) return;
    if (artCount > prevArtCountRef.current && artCount > 0) {
      setLootOpen(true);
    }
    prevArtCountRef.current = artCount;
  }, [selectedId, artCount]);

  // Agent harness providers + the mission's current pick (draft selector state)
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    listHarnesses(token)
      .then((res) => {
        if (!cancelled) setHarnesses(res.harnesses || []);
      })
      .catch(() => setHarnesses([]));
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    setHarnessPick(String(selected?.harness || ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- sync on mission change
  }, [selected?.id, selected?.harness]);

  const harnessLocked = Boolean(selected?.harness_locked) && selected?.status !== "draft";

  async function saveHarness(harness: string) {
    if (!selectedId || !harness) return;
    setBusy(true);
    try {
      const data = await updateInitiativeHarness(token, selectedId, harness);
      setDetail(data);
      await refreshList();
    } catch (err) {
      setError(errMsg(err));
      playSfx("warn");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!token || !selectedId || (!lootOpen && view !== "squad")) return;
    void loadWorkspace(selectedId);
    const timer = window.setInterval(() => {
      void loadWorkspace(selectedId);
    }, 4000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadWorkspace closes over token/selectedId
  }, [token, selectedId, lootOpen, artCount, view]);

  useEffect(() => {
    if (!token || !selectedId || view !== "squad") return undefined;
    let cancelled = false;
    (async () => {
      try {
        const data = await api<Initiative>(`/api/initiatives/${selectedId}`, { token });
        if (!cancelled) setDetail(data);
        const squad = await api<{ messages?: RoomMessage[] }>(`/api/initiatives/${selectedId}/squad`, { token });
        if (!cancelled) setMessages(squad.messages || []);
        // Seed the 小群 dock badge even before the drawer is first opened
        const subrooms = await listSubRooms(token, selectedId);
        if (!cancelled) setSubroomCount(subrooms.length);
      } catch (e) {
        if (!cancelled) setError(errMsg(e));
      }
    })();

    const es = new EventSource(`/api/initiatives/${selectedId}/events?token=${encodeURIComponent(token)}`);
    es.addEventListener("snapshot", (ev: MessageEvent) => setDetail(JSON.parse(String(ev.data)) as Initiative));
    es.addEventListener("room_snapshot", (ev: MessageEvent) => setMessages(JSON.parse(String(ev.data)) as RoomMessage[]));
    es.addEventListener("room_message", (ev: MessageEvent) => {
      const payload = JSON.parse(String(ev.data)) as { message?: RoomMessage };
      if (payload.message) {
        const msg = payload.message;
        setMessages((prev) => (prev.some((m) => m.id === msg.id) ? prev : [...prev, msg]));
      }
    });
    // Sub-rooms keep their own transcript; these events keep the mission page's
    // 小群 panel live (new rooms + turn-by-turn messages) without polling.
    es.addEventListener("subroom", () => setSubroomSignal((v) => v + 1));
    es.addEventListener("subroom_message", () => setSubroomSignal((v) => v + 1));
    const onUpdate = (ev: MessageEvent) => {
      const payload = JSON.parse(String(ev.data)) as { initiative?: Initiative };
      if (payload.initiative) {
        const ini = payload.initiative;
        setDetail(ini);
        setItems((prev) => {
          const others = prev.filter((p) => p.id !== ini.id);
          return [ini, ...others];
        });
      }
    };
    es.addEventListener("hitl", onUpdate);
    es.addEventListener("update", onUpdate);
    es.addEventListener("progress", onUpdate);
    es.addEventListener("stopped", onUpdate);
    return () => {
      cancelled = true;
      es.close();
    };
  }, [token, selectedId, view]);

  async function doLogin(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api<LoginResponse>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify(loginForm),
      });
      lsSet("sortie_token", data.token);
      setToken(data.token);
      setUser(data.user);
      setView("home");
    } catch (err) {
      // 403：密码正确但账号待审核/已被拒绝，按后端 detail 映射为本地文案
      if (err instanceof ApiError && err.code === 403) {
        const msg = err.message || "";
        if (msg.includes("pending")) setError(tr("accountPending"));
        else if (msg.includes("rejected")) setError(tr("accountRejected"));
        else setError(errMsg(err));
      } else {
        setError(errMsg(err));
      }
    } finally {
      setBusy(false);
    }
  }

  async function doRegister(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await registerUser(regForm);
      setRegDone(true);
      setRegForm({ username: "", password: "", display_name: "" });
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function doLogout() {
    try {
      await api("/api/auth/logout", { token, method: "POST" });
    } catch {
      /* ignore */
    }
    lsRemove("sortie_token");
    setToken("");
    setUser(null);
    setView("home");
  }

  async function doDeleteAccount() {
    // 注销不可恢复，二次确认后再调接口；名下任务与共享关系保留
    if (!confirm(tr("deleteAccountConfirm"))) return;
    setBusy(true);
    try {
      await deleteOwnAccount(token);
      lsRemove("sortie_token");
      setToken("");
      setUser(null);
      setView("home");
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  // 「增强提示词」通用调用：按场景让 LLM 优化文本，失败时 toast 并返回 null（不覆盖输入框）
  async function enhancePromptText(scene: string, text: string, context = ""): Promise<string | null> {
    try {
      const d = await api<{ text: string }>("/api/prompts/enhance", {
        token,
        method: "POST",
        body: JSON.stringify({ scene, text, context }),
      });
      return d.text;
    } catch (err) {
      setError(errMsg(err));
      return null;
    }
  }

  // 房间输入框增强：带最近 8 条聊天作上下文，让优化贴合当前讨论
  async function enhanceRoomInput(text: string): Promise<string | null> {
    const ctx = messages
      .slice(-8)
      .map((m) => `${m.actor_name}: ${m.text.slice(0, 200)}`)
      .join("\n")
      .slice(0, 4000);
    return enhancePromptText("room", text, ctx);
  }

  async function sendChat(e?: FormEvent | KeyboardEvent) {
    e?.preventDefault();
    if (!selectedId || !chatInput.trim()) return;
    setBusy(true);
    const text = chatInput;
    setChatInput("");
    try {
      const data = await api<{ message?: RoomMessage; initiative?: Initiative }>(`/api/initiatives/${selectedId}/squad/messages`, {
        token,
        method: "POST",
        body: JSON.stringify({ text, author_name: user?.display_name }),
      });
      if (data.message) {
        const msg = data.message;
        setMessages((prev) => (prev.some((m) => m.id === msg.id) ? prev : [...prev, msg]));
      }
      if (data.initiative) setDetail(data.initiative);
      await refreshDash();
    } catch (err) {
      setError(errMsg(err));
      setChatInput(text);
    } finally {
      setBusy(false);
    }
  }

  async function startPipeline() {
    if (!selected?.meta?.pipeline_confirmed) {
      setError(tr("startNeedConfirm"));
      return;
    }
    setBusy(true);
    try {
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/start`, { token, method: "POST" });
      setDetail(data);
      await refreshList();
      await refreshDash();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function confirmPipeline() {
    if (!selectedId) return;
    setBusy(true);
    try {
      const body =
        pipelineEditing && pipelineDraft.length
          ? { stages: pipelineDraft.map((s) => ({ id: s.id, label: s.label, role: s.role, hitl_after: s.hitl_after })) }
          : {};
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/pipeline/confirm`, {
        token,
        method: "POST",
        body: JSON.stringify(body),
      });
      setDetail(data);
      setPipelineEditing(false);
      await refreshList();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function savePipelineDraft(confirm = false) {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/pipeline`, {
        token,
        method: "PATCH",
        body: JSON.stringify({
          confirm,
          stages: pipelineDraft.map((s) => ({
            id: s.id,
            label: s.label,
            role: s.role,
            hitl_after: !!s.hitl_after,
          })),
        }),
      });
      setDetail(data);
      setPipelineEditing(!confirm);
      await refreshList();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  function movePipelineStage(idx: number, dir: -1 | 1) {
    setPipelineDraft((prev) => {
      const next = [...prev];
      const j = idx + dir;
      if (j < 0 || j >= next.length) return prev;
      // keep intake first / done last
      if (next[idx].id === "intake" || next[idx].id === "done") return prev;
      if (next[j].id === "intake" || next[j].id === "done") return prev;
      [next[idx], next[j]] = [next[j], next[idx]];
      return next;
    });
  }

  function removePipelineStage(idx: number) {
    setPipelineDraft((prev) => {
      const s = prev[idx];
      if (!s || s.id === "intake" || s.id === "done") return prev;
      return prev.filter((_, i) => i !== idx);
    });
  }

  function togglePipelineHitl(idx: number) {
    setPipelineDraft((prev) =>
      prev.map((s, i) => (i === idx ? { ...s, hitl_after: !s.hitl_after } : s))
    );
  }

  function addPipelineStage(id: string) {
    const spec = stageCatalog.find((s) => s.id === id);
    if (!spec) return;
    setPipelineDraft((prev) => {
      if (prev.some((s) => s.id === id)) return prev;
      const doneIdx = prev.findIndex((s) => s.id === "done");
      const insertAt = doneIdx >= 0 ? doneIdx : prev.length;
      const next = [...prev];
      next.splice(insertAt, 0, { ...spec });
      return next;
    });
  }

  useEffect(() => {
    const proposal = selected?.meta?.pipeline_proposal as { stages?: PipelineStageInfo[] } | undefined;
    const specs = (selected?.meta?.pipeline_specs as PipelineStageInfo[] | undefined) || proposal?.stages;
    if (Array.isArray(specs) && specs.length && !pipelineEditing) {
      setPipelineDraft(specs.map((s) => ({ ...s })));
    }
  }, [selected?.id, selected?.meta?.pipeline_specs, selected?.meta?.pipeline_confirmed, pipelineEditing]);

  async function submitHitl(action: string, harness?: string) {
    setBusy(true);
    playSfx(action === "approve" ? "gate" : "warn");
    try {
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/hitl`, {
        token,
        method: "POST",
        body: JSON.stringify({ action, harness: harness || null }),
      });
      setDetail(data);
      await refreshList();
      await refreshDash();
      playSfx(action === "approve" ? "ok" : "warn");
    } catch (err) {
      playSfx("warn");
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function stopPipeline() {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/stop`, {
        token,
        method: "POST",
      });
      setDetail(data);
      await refreshList();
      await refreshDash();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggleDiscussing(participantId: string, next: boolean) {
    if (!selectedId) return;
    setBusy(true);
    try {
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/squad/discuss`, {
        token,
        method: "POST",
        body: JSON.stringify({ participant_id: participantId, discussing: next }),
      });
      setDetail(data);
      await refreshList();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function removeTask(id: string) {
    if (!confirm(tr("delete") + "?")) return;
    await api(`/api/initiatives/${id}`, { token, method: "DELETE" });
    if (selectedId === id) {
      setSelectedId(null);
      setView("tasks");
    }
    await refreshList();
    await refreshDash();
  }

  // 共享弹窗变更回调：同步任务列表与当前详情（徽标计数随之更新）
  function onShareChanged(updated: Initiative) {
    setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
    if (selectedId === updated.id) setDetail(updated);
  }

  async function openArtifact(path: string, kind?: string) {
    const data = await api<{ content: string }>(`/api/initiatives/${selectedId}/artifacts/${path}`, { token });
    setArtifactPreview(data.content);
    setArtifactPreviewPath(path);
    setArtifactPreviewKind(kind || path.split("/").pop() || "");
    return data.content;
  }

  async function loadWorkspace(initiativeId: string) {
    try {
      const data = await api<MissionWorkspace>(`/api/initiatives/${initiativeId}/workspace`, { token });
      setWorkspace(data);
    } catch {
      setWorkspace(null);
    }
  }

  async function revealPath(path: string) {
    if (!path) return;
    try {
      playSfx("click");
      await api("/api/system/reveal", {
        token,
        method: "POST",
        body: JSON.stringify({ path }),
      });
    } catch (err) {
      setError(errMsg(err));
    }
  }

  async function loadAdminUsers() {
    const data = await api<PublicUser[]>("/api/admin/users", { token });
    setUsers(data);
  }

  const refreshKbBases = useCallback(async () => {
    if (!token) return;
    setKbBases(await listKnowledgeBases(token));
  }, [token]);

  const refreshKb = useCallback(async () => {
    if (!token) return;
    const params = new URLSearchParams();
    if (kbQuery.trim()) params.set("q", kbQuery.trim());
    if (kbBaseId) params.set("base_id", kbBaseId);
    const qs = params.toString();
    setKbDocs(await api<KnowledgeDoc[]>(`/api/knowledge${qs ? `?${qs}` : ""}`, { token }));
  }, [token, kbQuery, kbBaseId]);

  useEffect(() => {
    if (view === "admin" && user?.role === "admin") {
      loadAdminUsers().catch((e) => setError(errMsg(e)));
    }
  }, [view, user]);

  useEffect(() => {
    if (view === "knowledge" && token) {
      refreshKbBases().catch((e) => setError(errMsg(e)));
    }
  }, [view, token, refreshKbBases]);

  useEffect(() => {
    if (view !== "knowledge" || !token) return;
    refreshKb().catch((e) => setError(errMsg(e)));
  }, [view, token, refreshKb]);

  const kbBase = useMemo(
    () => kbBases.find((b) => b.id === kbBaseId) || null,
    [kbBases, kbBaseId]
  );

  const kbVisibleBases = useMemo(() => {
    const q = kbBaseQuery.trim().toLowerCase();
    return kbBases.filter((b) => {
      const okScope = kbBaseFilter === "all" || b.scope === kbBaseFilter;
      const okQ =
        !q ||
        b.name.toLowerCase().includes(q) ||
        (b.description || "").toLowerCase().includes(q);
      return okScope && okQ;
    });
  }, [kbBases, kbBaseFilter, kbBaseQuery]);

  const kbScopeCounts = useMemo(
    () => ({
      global: kbBases.filter((b) => b.scope === "global").length,
      user: kbBases.filter((b) => b.scope === "user").length,
      task: kbBases.filter((b) => b.scope === "task").length,
    }),
    [kbBases]
  );

  // WeKnora 异步解析：仍处于解析中的文档（completed / failed 视为终态）
  const kbParsingDocs = useMemo(
    () =>
      kbDocs.filter(
        (d) =>
          d.backend === "weknora" &&
          !!d.remote_id &&
          !["completed", "failed"].includes(d.parse_status || "") &&
          d.parse_status !== ""
      ),
    [kbDocs]
  );

  // 库内存在解析中的文档时每 3s 轮询刷新 parse_status，直至全部到终态
  useEffect(() => {
    if (view !== "knowledge" || !kbBaseId || !kbParsingDocs.length) return;
    const timer = window.setInterval(() => {
      refreshKnowledgeParseStatus(token, kbBaseId)
        .then((docs) => setKbDocs(docs))
        .catch(() => {
          /* 轮询失败静默，下一轮重试 */
        });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [view, kbBaseId, kbParsingDocs.length, token]);

  /** 新建弹窗打开：重置表单并拉取当前用户可见任务列表供「关联任务」下拉 */
  async function openKbCreate() {
    setKbBaseForm({ name: "", description: "", scope: "user", task_id: "" });
    setKbPendingFiles([]);
    setKbCreateOpen(true);
    try {
      setKbTasks(await api<Initiative[]>("/api/initiatives", { token }));
    } catch (err) {
      setError(errMsg(err));
    }
  }

  function addKbFiles(list: FileList | null, autoBaseId?: string) {
    if (!list) return;
    const next = Array.from(list).map((file) => {
      const ext = file.name.split(".").pop()?.toLowerCase() || "";
      const ok = KB_IMPORT_EXTS.has(ext);
      return {
        uid: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        status: ok ? ("queued" as const) : ("failed" as const),
        message: ok ? undefined : "不支持的格式",
      };
    });
    setKbPendingFiles((prev) => [...prev, ...next]);
    // 库详情导入弹窗：选中即自动上传（新建弹窗库尚未创建，仍走「创建并导入」）
    if (autoBaseId) {
      void uploadKbItemList(autoBaseId, next.filter((n) => n.status !== "failed"));
    }
  }

  /** 按 uid 逐个上传到指定库；状态更新用 uid 匹配，避免闭包里对象引用失效 */
  async function uploadKbItemList(
    baseId: string,
    items: { uid: string; file: File }[]
  ) {
    for (const item of items) {
      setKbPendingFiles((prev) => prev.map((p) => (p.uid === item.uid ? { ...p, status: "uploading" } : p)));
      try {
        await importKnowledgeFile(token, baseId, item.file);
        setKbPendingFiles((prev) => prev.map((p) => (p.uid === item.uid ? { ...p, status: "done" } : p)));
      } catch (err) {
        setKbPendingFiles((prev) =>
          prev.map((p) => (p.uid === item.uid ? { ...p, status: "failed", message: errMsg(err) } : p))
        );
      }
    }
  }

  const kbImportableCount = kbPendingFiles.filter((p) => p.status !== "failed").length;
  const kbImportDoneCount = kbPendingFiles.filter((p) => p.status === "done").length;
  const kbImportFailedCount = kbPendingFiles.filter((p) => p.status === "failed").length;

  async function submitKbCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const base = await createKnowledgeBase(token, {
        name: kbBaseForm.name,
        description: kbBaseForm.description,
        scope: kbBaseForm.scope,
        task_id: kbBaseForm.scope === "task" ? kbBaseForm.task_id : "",
      });
      await uploadKbItemList(base.id, kbPendingFiles.filter((p) => p.status !== "failed"));
      setKbCreateOpen(false);
      setKbPendingFiles([]);
      setKbBaseId(base.id);
      await Promise.all([refreshKbBases(), refreshKb()]);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteKbBase(id: string) {
    if (!confirm("删除该知识库？")) return;
    try {
      await deleteKnowledgeBase(token, id);
      setKbBaseId(null);
      await Promise.all([refreshKbBases(), refreshKb()]);
    } catch (err) {
      setError(errMsg(err));
    }
  }

  /** 库管理入口（删除按钮）显隐：admin / user 库所有者 / task 库创建者；后端仍会再校验 */
  function kbCanManage(base: KnowledgeBaseInfo) {
    if (user?.role === "admin") return true;
    if (base.scope === "user") return base.scope_id === user?.id;
    if (base.scope === "task") return !!base.created_by && base.created_by === user?.id;
    return false;
  }

  async function deleteKbDoc(id: string) {
    if (!confirm(tr("delete") + "?")) return;
    await api(`/api/knowledge/${id}`, { token, method: "DELETE" });
    if (kbDocDetail?.id === id) setKbDocDetail(null);
    await Promise.all([refreshKb(), refreshKbBases()]);
  }

  const refreshMemory = useCallback(async () => {
    if (!token) return;
    const data = await api<MemoryEntry[]>("/api/memory", { token });
    setMemories(data);
  }, [token]);

  const refreshAgents = useCallback(async () => {
    if (!token) return;
    const data = await api<PublishedAgent[]>("/api/agents", { token });
    setAgentList(data);
  }, [token]);

  useEffect(() => {
    if (view === "memory" && token) {
      refreshMemory().catch((e) => setError(errMsg(e)));
    }
  }, [view, token, refreshMemory]);

  // 简介异步生成：列表存在 pending 记忆时每 2s 轮询刷新，全部落定后自动停止
  useEffect(() => {
    if (view !== "memory" || !token) return;
    if (!memories.some((m) => m.summary_status === "pending")) return;
    const t = window.setTimeout(async () => {
      try {
        const data = await api<MemoryEntry[]>("/api/memory", { token });
        setMemories(data);
        // 详情弹窗若打开中，同步最新条目（loading → 简介内容）
        setMemDetail((prev) => (prev ? data.find((m) => m.id === prev.id) || prev : prev));
      } catch {
        /* 单次轮询失败忽略，下一轮重试 */
      }
    }, 2000);
    return () => window.clearTimeout(t);
  }, [view, token, memories]);

  // 重新生成简介：置 pending 触发 loading，随后由轮询自动带回结果
  async function regenerateMemSummary(id: string) {
    try {
      const entry = await api<MemoryEntry>(`/api/memory/${id}/summary/regenerate`, {
        token,
        method: "POST",
      });
      setMemories((list) => list.map((m) => (m.id === id ? entry : m)));
      setMemDetail((prev) => (prev && prev.id === id ? entry : prev));
    } catch (err) {
      setError(errMsg(err));
    }
  }

  useEffect(() => {
    if ((view === "agents" || view === "tasks") && token) {
      refreshAgents().catch((e) => setError(errMsg(e)));
    }
  }, [view, token, refreshAgents]);

  // 写入记忆（弹窗表单）：全局约定 / 我的三种目标（所有任务 / 指定任务 / 指定 Agent）
  async function saveMemory(e: FormEvent) {
    e.preventDefault();
    if (!memForm.content.trim()) return;
    let scope: MemoryScope = memForm.scope;
    let scopeId = scope === "workspace" ? "global" : user?.id || "";
    // 「我的」定向共享：映射到 mission / agent 域（须已选目标）
    if (scope === "user" && memTarget !== "all") {
      if (!memForm.scope_id) {
        setError(memTarget === "mission" ? tr("memPickTask") : tr("memPickAgent"));
        return;
      }
      scope = memTarget === "mission" ? "mission" : "agent";
      scopeId = memForm.scope_id;
    }
    setBusy(true);
    try {
      await api("/api/memory", {
        token,
        method: "POST",
        body: JSON.stringify({
          scope,
          scope_id: scopeId,
          kind: memForm.kind,
          content: memForm.content,
          tags: [],
        }),
      });
      setMemForm({ ...memForm, content: "" });
      setMemModalOpen(false);
      await refreshMemory();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  // 记忆内容「增强提示词」：LLM 精炼为可复用陈述，覆盖表单内容，保留原文可撤销
  async function enhanceMemoryContent() {
    if (memEnhancing || !memForm.content.trim()) return;
    const original = memForm.content;
    setMemEnhancing(true);
    try {
      const next = await enhancePromptText("memory", original);
      if (next && next !== original) {
        setMemEnhancePrev(original);
        setMemForm({ ...memForm, content: next });
      }
    } finally {
      setMemEnhancing(false);
    }
  }

  // 导入 md 文档：读入内容作为记忆全文，简介由 LLM 异步总结（失败降级截断）
  function importMemoryMd(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setMemForm((f) => ({ ...f, content: String(reader.result || "") }));
    reader.readAsText(file);
  }

  // 删除记忆：不弹确认，toast 提供撤销（撤销 = 原内容重新写入）
  async function deleteMemory(id: string) {
    const entry = memories.find((m) => m.id === id);
    await api(`/api/memory/${id}`, { token, method: "DELETE" });
    await refreshMemory();
    if (entry) {
      memUndoRef.current = entry;
      setMemToast(true);
      if (memToastTimer.current) window.clearTimeout(memToastTimer.current);
      memToastTimer.current = window.setTimeout(() => setMemToast(false), 4000);
    }
  }

  async function undoDeleteMemory() {
    const entry = memUndoRef.current;
    if (!entry) return;
    memUndoRef.current = null;
    setMemToast(false);
    try {
      await api("/api/memory", {
        token,
        method: "POST",
        body: JSON.stringify({
          scope: entry.scope,
          scope_id: entry.scope_id,
          kind: entry.kind,
          content: entry.content,
          tags: entry.tags || [],
        }),
      });
      await refreshMemory();
    } catch (err) {
      setError(errMsg(err));
    }
  }

  function startNewAgent() {
    playSfx("click");
    setAgentEditing(true);
    setAgentSelectedId(null);
    setAgentForm({
      title: "",
      slot: "product",
      summary: "",
      persona: "",
      tags: "",
      visibility: "public",
      toolkit_ids: [],
      avatar: "",
    });
    setAgentSpec(emptyPersonaSpec());
    setAgentPersonaMode("structured");
  }

  function startEditAgent(a: PublishedAgent) {
    playSfx("click");
    setAgentEditing(true);
    setAgentSelectedId(a.id);
    setAgentForm({
      title: a.title || "",
      slot: a.slot || "product",
      summary: a.summary || "",
      persona: a.persona || "",
      tags: (a.tags || []).join(", "),
      visibility: a.visibility || "public",
      toolkit_ids: [...(a.toolkit_ids || [])],
      avatar: a.avatar || `preset:${a.slot || "product"}`,
    });
    // 旧角色可能只有自由文本：本地解析一份结构化视图，便于直接分段编辑
    setAgentSpec(a.persona_spec ?? parsePersonaText(a.persona || ""));
    setAgentPersonaMode("structured");
  }

  async function uploadPublishAvatar(file: File | undefined) {
    if (!token || !file) return;
    setBusy(true);
    setError("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const url = agentSelectedId
        ? `/api/agents/${agentSelectedId}/avatar`
        : "/api/media/avatars";
      const res = await fetch(url, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: fd,
      });
      if (!res.ok) throw new Error((await res.text()) || res.statusText);
      const data = (await res.json()) as { avatar?: string; agent?: PublishedAgent };
      if (data.avatar) setAgentForm((prev) => ({ ...prev, avatar: data.avatar! }));
      if (data.agent) {
        setAgentList((prev) => prev.map((a) => (a.id === data.agent!.id ? data.agent! : a)));
      }
      playSfx("ok");
    } catch (err) {
      playSfx("warn");
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function saveAgent(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    // 按当前编辑模式只提交一种人设来源，避免结构化字段与原文互相覆盖
    const payload = {
      title: agentForm.title,
      slot: agentForm.slot,
      summary: agentForm.summary,
      ...(agentPersonaMode === "structured"
        ? { persona_spec: agentSpec }
        : { persona: agentForm.persona }),
      visibility: agentForm.visibility,
      toolkit_ids: agentForm.toolkit_ids,
      avatar: agentForm.avatar || `preset:${agentForm.slot}`,
      tags: agentForm.tags
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    };
    try {
      const data = agentSelectedId
        ? await api<PublishedAgent>(`/api/agents/${agentSelectedId}`, {
            token,
            method: "PATCH",
            body: JSON.stringify(payload),
          })
        : await api<PublishedAgent>("/api/agents", {
            token,
            method: "POST",
            body: JSON.stringify(payload),
          });
      setAgentEditing(false);
      setAgentSelectedId(data.id);
      await refreshAgents();
      playSfx("ok");
    } catch (err) {
      playSfx("warn");
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteAgent(id: string) {
    if (!confirm(tr("delete") + "?")) return;
    await api(`/api/agents/${id}`, { token, method: "DELETE" });
    if (agentSelectedId === id) setAgentSelectedId(null);
    await refreshAgents();
  }

  const agentSelected = useMemo(
    () => agentList.find((a) => a.id === agentSelectedId) || null,
    [agentList, agentSelectedId]
  );

  // V2 名册网格数据源：按顶部分段过滤器筛可见性
  const filteredAgents = useMemo(
    () => (agentVisFilter === "all" ? agentList : agentList.filter((a) => a.visibility === agentVisFilter)),
    [agentList, agentVisFilter]
  );

  useEffect(() => {
    if ((view !== "tasks" && view !== "squad") || !token) return;
    api<{ tracks: PipelineTrackInfo[]; catalog?: PipelineStageInfo[] }>("/api/pipelines", { token })
      .then((d) => {
        if (d.catalog?.length) setStageCatalog(d.catalog);
      })
      .catch((e) => setError(errMsg(e)));
  }, [view, token]);

  function toggleAgentFormToolkit(id: string) {
    setAgentForm((prev) => {
      const cur = prev.toolkit_ids || [];
      const toolkit_ids = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      return { ...prev, toolkit_ids };
    });
  }

  // ── V-C 装配线 · 工具绑定弹框 ──
  // 打开时以当前已绑定为草稿底稿，弹框内增删只改草稿，确定才写回
  function openToolPicker() {
    playSfx("click");
    setToolPickDraft(agentForm.toolkit_ids);
    setToolQuery("");
    setToolKindFilter("");
    setToolPickerOpen(true);
  }

  function toggleToolDraft(id: string) {
    setToolPickDraft((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  // 确定绑定：草稿写回表单；取消 / Esc / 点遮罩则丢弃草稿
  function confirmToolPick() {
    setAgentForm((prev) => ({ ...prev, toolkit_ids: toolPickDraft }));
    setToolPickerOpen(false);
    playSfx("ok");
  }

  // 装配线导航：滚动到对应分段（01 身份 / 02 人设 / 03 装备 / 04 发布）
  // 不用 scrollIntoView——它会同时滚动多层祖先容器，容易把滚动状态搞乱；
  // 改为在主滚动容器 .main 上按视口差值设置 scrollTop，滚动后仍可自由上下滑
  function jumpToAsm(anchor: string) {
    const el = document.getElementById(anchor);
    if (!el) return;
    const container = el.closest(".main");
    if (!container) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const cRect = container.getBoundingClientRect();
    const eRect = el.getBoundingClientRect();
    container.scrollTo({ top: container.scrollTop + (eRect.top - cRect.top) - 12, behavior: "smooth" });
  }

  // 弹框打开期间：Esc 关闭 + 锁定背景滚动（App.tsx 中 KeyboardEvent 被 React 类型遮蔽，走 Event + key 断言）
  useEffect(() => {
    if (!toolPickerOpen) return undefined;
    const onKey = (e: Event) => {
      if ((e as { key?: string }).key === "Escape") setToolPickerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [toolPickerOpen]);

  const enabledToolkit = useMemo(
    () => toolkitCatalog.filter((i) => i.enabled !== false),
    [toolkitCatalog]
  );
  // 已选装备 id → 工具条目查找（id 可能指向已停用/已删除项，回退为仅显示 id）
  const toolById = useMemo(() => new Map(enabledToolkit.map((t) => [t.id, t])), [enabledToolkit]);
  // 弹框候选列表：类型过滤 + 名称/用途搜索
  const pickerTools = useMemo(() => {
    const q = toolQuery.trim().toLowerCase();
    return enabledToolkit.filter(
      (t) =>
        (!toolKindFilter || t.kind === toolKindFilter) &&
        (!q || t.name.toLowerCase().includes(q) || (t.summary || "").toLowerCase().includes(q))
    );
  }, [enabledToolkit, toolQuery, toolKindFilter]);
  // V-C 装配线四段（label/hint 为 i18n key；anchor 对应表单分段 DOM id）
  const ASM_STAGES: {
    key: "identity" | "persona" | "gear" | "publish";
    no: string;
    en: string;
    anchor: string;
    label: string;
    hint: string;
  }[] = [
    { key: "identity", no: "01", en: "IDENTITY", anchor: "wr-asm-identity", label: "wrSecIdentity", hint: "wrAsmIdentityHint" },
    { key: "persona", no: "02", en: "PERSONA", anchor: "wr-asm-persona", label: "wrAsmPersona", hint: "wrAsmPersonaHint" },
    { key: "gear", no: "03", en: "GEAR", anchor: "wr-asm-gear", label: "wrAsmGear", hint: "wrAsmGearHint" },
    { key: "publish", no: "04", en: "PUBLISH", anchor: "wr-asm-publish", label: "wrAsmPublish", hint: "wrAsmPublishHint" },
  ];
  // 各分段完成态：驱动装配线导航圆点与底部「可发布」提示
  const asmDone = {
    identity: !!agentForm.title.trim(),
    persona: !!agentForm.persona.trim(),
    gear: agentForm.toolkit_ids.length > 0,
    publish: !!agentForm.title.trim() && !!agentForm.persona.trim() && agentForm.toolkit_ids.length > 0,
  };
  // 常驻预算：启用且 load=always 的条目数（每轮注入上下文的成本计数）
  const toolkitAlwaysCount = useMemo(
    () => toolkitCatalog.filter((i) => i.enabled !== false && i.load === "always").length,
    [toolkitCatalog]
  );
  // 各 tab 条目数（大号分段 tab 上的计数）
  const toolkitKindCounts = useMemo(
    () => ({
      tool: toolkitCatalog.filter((i) => i.kind === "tool").length,
      skill: toolkitCatalog.filter((i) => i.kind === "skill").length,
      mcp: toolkitCatalog.filter((i) => i.kind === "mcp").length,
    }),
    [toolkitCatalog]
  );
  // 当前 tab 下各功能分类的条目数（分类 chips 上的计数）
  const toolkitCategoryCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const item of toolkitCatalog) {
      if (item.kind !== toolkitTab) continue;
      const key = item.category || "";
      out[key] = (out[key] || 0) + 1;
    }
    return out;
  }, [toolkitCatalog, toolkitTab]);

  const refreshToolkitCatalog = useCallback(async () => {
    if (!token) return;
    const data = await api<ToolkitItem[]>("/api/toolkit", { token });
    setToolkitCatalog(data);
  }, [token]);

  useEffect(() => {
    if ((view === "tasks" || view === "agents" || view === "toolkit") && token) {
      refreshToolkitCatalog().catch((e) => setError(errMsg(e)));
    }
  }, [view, token, refreshToolkitCatalog]);

  const refreshToolkit = useCallback(async () => {
    if (!token) return;
    const params = new URLSearchParams({ kind: toolkitTab });
    if (toolkitCategory) params.set("category", toolkitCategory);
    if (toolkitQuery.trim()) params.set("q", toolkitQuery.trim());
    const data = await api<ToolkitItem[]>(`/api/toolkit?${params}`, { token });
    setToolkitItems(data);
  }, [token, toolkitTab, toolkitCategory, toolkitQuery]);

  useEffect(() => {
    if (view === "toolkit" && token) {
      refreshToolkit().catch((e) => setError(errMsg(e)));
      api<Record<string, number>>("/api/toolkit/mounts", { token })
        .then(setToolkitMounts)
        .catch(() => setToolkitMounts({}));
      api<MarketSkill[]>("/api/market/skills", { token })
        .then(setMarketSkills)
        .catch(() => setMarketSkills([]));
    }
  }, [view, token, refreshToolkit]);

  // 卡片「…」菜单：点击其他区域时收起
  useEffect(() => {
    if (!openMenuId) return;
    const close = () => setOpenMenuId(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [openMenuId]);

  // 主题面板：点击面板外部时收起；触发按钮与面板同在 .theme-dock 内，点其内部不收起
  useEffect(() => {
    if (!themeOpen) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element | null;
      if (t && t.closest(".theme-dock")) return;
      setThemeOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [themeOpen]);

  // V4 弹窗/抽屉：Esc 逐层关闭
  useEffect(() => {
    const onKey = (e: Event) => {
      const key = (e as globalThis.KeyboardEvent).key;
      if (key !== "Escape") return;
      if (mcpModalOpen) setMcpModalOpen(false);
      else if (toolModalOpen) setToolModalOpen(false);
      else if (skillModalOpen) closeSkillModal();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mcpModalOpen, toolModalOpen, skillModalOpen]);

  const toolkitSelected = useMemo(
    () => toolkitItems.find((i) => i.id === toolkitSelectedId) || null,
    [toolkitItems, toolkitSelectedId]
  );

  const toolkitFeatured = useMemo(
    () => toolkitItems.filter((i) => i.featured && i.enabled !== false),
    [toolkitItems]
  );

  const toolkitGridItems = useMemo(() => {
    // Prefer non-featured in the main grid when a featured row is shown; keep all if none featured.
    if (!toolkitFeatured.length || toolkitCategory || toolkitQuery.trim()) return toolkitItems;
    return toolkitItems.filter((i) => !i.featured);
  }, [toolkitItems, toolkitFeatured, toolkitCategory, toolkitQuery]);

  function toolkitCategoryLabel(cat: string) {
    if (!cat) return tr("tkAllCategories");
    const key = CATEGORY_I18N[cat];
    return key ? tr(key) : cat;
  }

  // 卡片 monogram 的色调：按类型/状态给语义色（工程蓝 / 市场绿 / 异常红 / 中性墨）
  function toolkitMonogramTone(item: ToolkitItem): string {
    if (item.kind === "mcp") {
      if (item.enabled === false) return "r";
      return item.transport === "http" || item.transport === "sse" ? "g" : "k";
    }
    if (item.kind === "skill") return "g";
    return item.enabled === false ? "k" : "";
  }

  function startEditToolkit(item: ToolkitItem) {
    setToolkitEditing(true);
    setToolkitSelectedId(item.id);
    setToolkitForm({
      name: item.name || "",
      summary: item.summary || "",
      runtime: (item.runtime as ToolRuntime) || "cli",
      endpoint: item.endpoint || "",
      transport: (item.transport as McpTransport) || "stdio",
      command: item.command || "",
      url: item.url || "",
      body: item.body || "",
      category: item.category || "",
      tags: (item.tags || []).join(", "),
      featured: !!item.featured,
      enabled: item.enabled !== false,
      load: item.load === "always" ? "always" : "defer",
      scripts: (item.scripts || []).map((s) => ({ ...s })),
    });
  }

  async function saveToolkit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const payload = {
      kind: toolkitTab,
      name: toolkitForm.name,
      summary: toolkitForm.summary,
      runtime: toolkitTab === "tool" ? toolkitForm.runtime : null,
      endpoint: toolkitTab === "tool" ? toolkitForm.endpoint : "",
      transport: toolkitTab === "mcp" ? toolkitForm.transport : null,
      command: toolkitTab === "mcp" ? toolkitForm.command : "",
      url: toolkitTab === "mcp" ? toolkitForm.url : "",
      body: toolkitTab === "skill" ? toolkitForm.body : "",
      scripts: toolkitTab === "skill" ? toolkitForm.scripts : [],
      category: toolkitForm.category.trim(),
      tags: toolkitForm.tags
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      featured: toolkitForm.featured,
      enabled: toolkitForm.enabled,
      load: toolkitForm.load,
    };
    try {
      const data = toolkitSelectedId
        ? await api<ToolkitItem>(`/api/toolkit/${toolkitSelectedId}`, {
            token,
            method: "PATCH",
            body: JSON.stringify(payload),
          })
        : await api<ToolkitItem>("/api/toolkit", {
            token,
            method: "POST",
            body: JSON.stringify(payload),
          });
      setToolkitEditing(false);
      setToolkitSelectedId(data.id);
      await refreshToolkit();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  function addToolkitScript() {
    setToolkitForm({
      ...toolkitForm,
      scripts: [
        ...toolkitForm.scripts,
        { path: `scripts/script-${toolkitForm.scripts.length + 1}.py`, content: "", language: "py" },
      ],
    });
  }

  function updateToolkitScript(idx: number, patch: Partial<SkillScript>) {
    setToolkitForm({
      ...toolkitForm,
      scripts: toolkitForm.scripts.map((s, i) => (i === idx ? { ...s, ...patch } : s)),
    });
  }

  function removeToolkitScript(idx: number) {
    setToolkitForm({
      ...toolkitForm,
      scripts: toolkitForm.scripts.filter((_, i) => i !== idx),
    });
  }

  async function deleteToolkit(id: string) {
    if (!confirm(tr("delete") + "?")) return;
    await api(`/api/toolkit/${id}`, { token, method: "DELETE" });
    if (toolkitSelectedId === id) setToolkitSelectedId(null);
    await refreshToolkit();
  }

  async function toggleToolkitEnabled(item: ToolkitItem) {
    await api(`/api/toolkit/${item.id}`, {
      token,
      method: "PATCH",
      body: JSON.stringify({ enabled: !item.enabled }),
    });
    await refreshToolkit();
  }

  // ======================= V4 工具箱：安装 / 测试 / 优化 =======================

  async function refreshToolkitAfterChange() {
    await refreshToolkit();
    await refreshToolkitCatalog();
    api<MarketSkill[]>("/api/market/skills", { token }).then(setMarketSkills).catch(() => {});
  }

  function fileToBase64(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        resolve(result.includes(",") ? result.split(",", 2)[1] : result);
      };
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function installMarketSkill(key: string) {
    setBusy(true);
    try {
      await api("/api/market/skills/install", {
        token,
        method: "POST",
        body: JSON.stringify({ key }),
      });
      await refreshToolkitAfterChange();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function runInstallFromGithub() {
    if (!/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+/.test(skillUrl.trim())) {
      setSkillModalMsg(tr("tkSkillRemoteHint"));
      return;
    }
    setSkillModalMsg(tr("tkTesting") === "Testing…" ? "Fetching from GitHub…" : "正在从 GitHub 拉取…");
    setBusy(true);
    try {
      const item = await api<ToolkitItem>("/api/toolkit/install_skill", {
        token,
        method: "POST",
        body: JSON.stringify({ url: skillUrl.trim() }),
      });
      closeSkillModal();
      await refreshToolkitAfterChange();
      setToolkitTab("skill");
      setToolkitSelectedId(item.id);
    } catch (err) {
      setSkillModalMsg(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function runSkillUpload() {
    if (!skillUpload) {
      setSkillModalMsg(tr("tkSkillFileHint"));
      return;
    }
    setSkillModalMsg(tr("tkTesting") === "Testing…" ? "Parsing file…" : "正在解析文件…");
    setBusy(true);
    try {
      const isZip = skillUpload.file.name.toLowerCase().endsWith(".zip");
      const body: Record<string, unknown> = {
        kind: "skill",
        filename: skillUpload.file.name,
        overwrite: false,
      };
      if (isZip) body.zip_base64 = await fileToBase64(skillUpload.file);
      else body.content = await skillUpload.file.text();
      await api("/api/toolkit/import", { token, method: "POST", body: JSON.stringify(body) });
      closeSkillModal();
      await refreshToolkitAfterChange();
    } catch (err) {
      setSkillModalMsg(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function runOptimizeSkill() {
    if (!writeForm.name.trim() || !writeForm.body.trim()) {
      setSkillModalMsg(tr("tkTesting") === "Testing…" ? "Name and body are required" : "请填写技能名称与正文");
      return;
    }
    setOptimizePhase("running");
    setSkillModalMsg("");
    try {
      const draft = await api<SkillDraft>("/api/toolkit/optimize_skill", {
        token,
        method: "POST",
        body: JSON.stringify({ name: writeForm.name.trim(), category: writeForm.category, body: writeForm.body.trim() }),
      });
      setSkillDraft(draft);
      setOptimizePhase("review");
    } catch (err) {
      setOptimizePhase("idle");
      setSkillModalMsg(errMsg(err));
    }
  }

  async function confirmSkillDraft(keep: "original" | "optimized") {
    if (!skillDraft) return;
    setBusy(true);
    try {
      const item = await api<ToolkitItem>(`/api/toolkit/optimize_skill/${skillDraft.draft_id}/confirm`, {
        token,
        method: "POST",
        body: JSON.stringify({ keep, load: "defer" }),
      });
      closeSkillModal();
      await refreshToolkitAfterChange();
      setToolkitTab("skill");
      setToolkitSelectedId(item.id);
    } catch (err) {
      setSkillModalMsg(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  function closeSkillModal() {
    setSkillModalOpen(false);
    setSkillMode("remote");
    setLocalMode("upload");
    setSkillUrl("");
    setSkillModalMsg("");
    setSkillUpload(null);
    setWriteForm({ name: "", category: "engineering", body: "" });
    setOptimizePhase("idle");
    setSkillDraft(null);
  }

  function openToolModal() {
    setToolForm({ name: "", summary: "", category: "engineering", runtime: "cli", endpoint: "", load: "defer" });
    setToolModalOpen(true);
  }

  async function submitToolModal(e: FormEvent) {
    e.preventDefault();
    if (!toolForm.name.trim() || !toolForm.endpoint.trim()) {
      setError(tr("tkTesting") === "Testing…" ? "Name and endpoint are required" : "名称与接入点不能为空");
      return;
    }
    setBusy(true);
    try {
      const item = await api<ToolkitItem>("/api/toolkit", {
        token,
        method: "POST",
        body: JSON.stringify({
          kind: "tool",
          name: toolForm.name.trim(),
          summary: toolForm.summary.trim(),
          runtime: toolForm.runtime,
          endpoint: toolForm.endpoint.trim(),
          category: toolForm.category,
          load: toolForm.load,
        }),
      });
      setToolModalOpen(false);
      await refreshToolkitAfterChange();
      setToolkitTab("tool");
      setToolkitSelectedId(item.id);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  function openMcpModal() {
    setMcpForm({ name: "", transport: "stdio", url: "", auth: "bearer", token: "", command: "", category: "engineering" });
    setMcpPkg({ file: null, phase: "idle" });
    setMcpTest({ state: "idle", msg: "" });
    setMcpModalOpen(true);
  }

  // stdio：仅支持 zip 包导入；先在本地暂存文件，提交时经 /api/toolkit/import 安装
  async function pickMcpPkg(file: File | undefined) {
    if (!file) return;
    const slug = file.name.replace(/\.zip$/i, "").toLowerCase().replace(/[^a-z0-9-]+/g, "-");
    setMcpPkg({ file, phase: "ready" });
    setMcpForm((f) => ({
      ...f,
      name: f.name || slug,
      command: `npx -y @modelcontextprotocol/server-${slug}`,
    }));
    setMcpTest({ state: "idle", msg: tr("tkPkgReady") });
  }

  async function runMcpTest() {
    setMcpTest({ state: "testing", msg: tr("tkTesting") });
    try {
      const result = await api<McpTestResult>("/api/toolkit/mcp/test", {
        token,
        method: "POST",
        body: JSON.stringify({
          transport: mcpForm.transport,
          url: mcpForm.url.trim(),
          command: mcpForm.command.trim(),
          config: mcpForm.auth === "bearer" && mcpForm.token.trim() ? { bearer_token: mcpForm.token.trim() } : {},
        }),
      });
      if (result.ok) {
        setMcpTest({ state: "ok", msg: `${tr("tkTestOk")} · ${(result.tools || []).length} tools` });
      } else {
        setMcpTest({ state: "fail", msg: `${tr("tkTestFail")}：${result.error || "unknown"}` });
      }
    } catch (err) {
      setMcpTest({ state: "fail", msg: `${tr("tkTestFail")}：${errMsg(err)}` });
    }
  }

  async function submitMcpModal(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      let item: ToolkitItem;
      if (mcpForm.transport === "stdio") {
        if (!mcpPkg.file) {
          setError(tr("tkMcpStdioZipOnly"));
          setBusy(false);
          return;
        }
        // zip 导入即安装：后端落盘 mcp_packages 并生成 stdio 条目
        const imported = await api<{ items: ToolkitItem[] }>("/api/toolkit/import", {
          token,
          method: "POST",
          body: JSON.stringify({
            kind: "mcp",
            zip_base64: await fileToBase64(mcpPkg.file),
            filename: mcpPkg.file.name,
            overwrite: true,
          }),
        });
        item = imported.items[0];
        // 用户改过预填命令时回写
        if (mcpForm.command.trim() && mcpForm.command.trim() !== item.command) {
          item = await api<ToolkitItem>(`/api/toolkit/${item.id}`, {
            token,
            method: "PATCH",
            body: JSON.stringify({ command: mcpForm.command.trim() }),
          });
        }
      } else {
        item = await api<ToolkitItem>("/api/toolkit", {
          token,
          method: "POST",
          body: JSON.stringify({
            kind: "mcp",
            name: mcpForm.name.trim(),
            transport: mcpForm.transport,
            url: mcpForm.url.trim(),
            category: mcpForm.category,
            config:
              mcpForm.auth === "bearer" && mcpForm.token.trim()
                ? { bearer_token: mcpForm.token.trim() }
                : {},
          }),
        });
      }
      setMcpModalOpen(false);
      await refreshToolkitAfterChange();
      setToolkitTab("mcp");
      setToolkitSelectedId(item.id);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  // 详情页：对既有连接器发起连通性测试
  async function testMcpItem(item: ToolkitItem) {
    setMcpItemTest({ id: item.id, state: "testing", msg: tr("tkTesting") });
    try {
      const result = await api<McpTestResult>("/api/toolkit/mcp/test", {
        token,
        method: "POST",
        body: JSON.stringify({
          transport: item.transport || "stdio",
          url: item.url || "",
          command: item.command || "",
          args: item.args || [],
          env: item.env || {},
          config: item.config || {},
        }),
      });
      setMcpItemTest({
        id: item.id,
        state: result.ok ? "ok" : "fail",
        msg: result.ok
          ? `${tr("tkTestOk")} · ${(result.tools || []).length} tools`
          : `${tr("tkTestFail")}：${result.error || "unknown"}`,
      });
    } catch (err) {
      setMcpItemTest({ id: item.id, state: "fail", msg: `${tr("tkTestFail")}：${errMsg(err)}` });
    }
  }

  async function createUser(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/admin/users", { token, method: "POST", body: JSON.stringify(newUser) });
      setNewUser({ username: "", password: "", display_name: "", role: "member" });
      await loadAdminUsers();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteUser(id: string) {
    await api(`/api/admin/users/${id}`, { token, method: "DELETE" });
    await loadAdminUsers();
  }

  // 审核通过/拒绝：pending → active / rejected；拒绝后该用户所有 token 立即失效
  async function setUserStatus(id: string, status: "active" | "rejected") {
    setBusy(true);
    try {
      await adminUpdateUser(token, id, { status });
      await loadAdminUsers();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  // 管理员改角色：最后一个 admin 降级会被后端拒绝并提示
  async function setUserRole(id: string, role: UserRole) {
    setBusy(true);
    try {
      await adminUpdateUser(token, id, { role });
      await loadAdminUsers();
    } catch (err) {
      setError(errMsg(err));
      await loadAdminUsers();
    } finally {
      setBusy(false);
    }
  }

  // 用户管理页派生数据：计数、待审核名单、按用户名/显示名即时过滤
  const adminPending = users.filter((u) => u.status === "pending");
  const adminKeyword = userSearch.trim().toLowerCase();
  const adminFiltered = adminKeyword
    ? users.filter(
        (u) =>
          u.username.toLowerCase().includes(adminKeyword) ||
          u.display_name.toLowerCase().includes(adminKeyword),
      )
    : users;

  if (!token || !user) {
    return (
      <div className={`deck login-deck ${booted ? "booted" : ""}`}>
        {atmosphere}
        {regMode ? (
          <form
            className="login-card"
            onSubmit={(e) => {
              void doRegister(e);
            }}
          >
            <div className="brand-block">
              <img className="brand-logo" src="/logo.png" alt="Sortie Deck" />
              <div>
                <h1 className="brand-title">{tr("product")}</h1>
                <p className="brand-sub">{tr("register")}</p>
              </div>
            </div>
            {regDone && <div className="alert">{tr("registerPending")}</div>}
            <label>
              <span>{tr("username")}</span>
              <input
                value={regForm.username}
                onChange={(e) => setRegForm({ ...regForm, username: e.target.value })}
                required
              />
            </label>
            <label>
              <span>{tr("displayName")}</span>
              <input
                value={regForm.display_name}
                onChange={(e) => setRegForm({ ...regForm, display_name: e.target.value })}
                required
              />
            </label>
            <label>
              <span>{tr("password")}</span>
              <input
                type="password"
                value={regForm.password}
                onChange={(e) => setRegForm({ ...regForm, password: e.target.value })}
                required
              />
            </label>
            <button className="btn primary" type="submit" disabled={busy}>
              {tr("registerSubmit")}
            </button>
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setRegMode(false);
                setRegDone(false);
                setError("");
              }}
            >
              {tr("backToLogin")}
            </button>
            <div className="theme-panel-actions">
              <button type="button" className="btn ghost" onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
                {tr("lang")}
              </button>
              {themeControls}
            </div>
            {error && <div className="alert">{error}</div>}
          </form>
        ) : (
          <form className="login-card" onSubmit={doLogin}>
            <div className="brand-block">
              <img className="brand-logo" src="/logo.png" alt="Sortie Deck" />
              <div>
                <h1 className="brand-title">{tr("product")}</h1>
                <p className="brand-sub">{tr("tagline")}</p>
              </div>
            </div>
            <label>
              <span>{tr("username")}</span>
              <input
                value={loginForm.username}
                onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })}
                required
              />
            </label>
            <label>
              <span>{tr("password")}</span>
              <input
                type="password"
                value={loginForm.password}
                onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
                required
              />
            </label>
            <button className="btn primary" type="submit" disabled={busy}>
              {tr("login")}
            </button>
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setRegMode(true);
                setRegDone(false);
                setError("");
              }}
            >
              {tr("register")}
            </button>
            <p className="login-hint">{tr("loginHint")}</p>
            <div className="theme-panel-actions">
              <button type="button" className="btn ghost" onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
                {tr("lang")}
              </button>
              {themeControls}
            </div>
            {error && <div className="alert">{error}</div>}
          </form>
        )}
      </div>
    );
  }

  return (
    <div className={`deck ${booted ? "booted" : ""}`}>
      {atmosphere}

      <header className="deck-header">
        <div className="brand-block">
          <img className="brand-logo" src="/logo.png" alt="Sortie Deck" />
          <div>
            <h1 className="brand-title">{tr("product")}</h1>
            <p className="brand-sub">{tr("tagline")}</p>
          </div>
        </div>
        <nav className="main-nav">
          <button
            type="button"
            className={view === "home" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("home");
            }}
          >
            {tr("home")}
          </button>
          <button
            type="button"
            className={view === "tasks" || view === "squad" || view === "intake" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("tasks");
            }}
          >
            {tr("tasks")}
          </button>
          <button
            type="button"
            className={view === "knowledge" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("knowledge");
            }}
          >
            {tr("knowledge")}
          </button>
          {/* 记忆库仅管理员可见：普通用户不提供该设置界面 */}
          {user?.role === "admin" && (
            <button
              type="button"
              className={view === "memory" ? "on" : ""}
              onClick={() => {
                unlockSfx();
                playSfx("whoosh");
                setView("memory");
              }}
            >
              {tr("memory")}
            </button>
          )}
          <button
            type="button"
            className={view === "agents" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("agents");
            }}
          >
            {tr("agents")}
          </button>
          <button
            type="button"
            className={view === "toolkit" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("toolkit");
            }}
          >
            {tr("toolkit")}
          </button>
          <button
            type="button"
            className={view === "forge" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("forge");
            }}
          >
            {tr("forge")}
          </button>
          <button
            type="button"
            className={view === "subrooms" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("subrooms");
            }}
          >
            {tr("subrooms")}
          </button>
          <button
            type="button"
            className={view === "decisions" ? "on" : ""}
            onClick={() => {
              unlockSfx();
              playSfx("whoosh");
              setView("decisions");
            }}
          >
            {tr("decisions")}
          </button>
          {user.role === "admin" && (
            <button
              type="button"
              className={view === "admin" ? "on" : ""}
              onClick={() => {
                unlockSfx();
                playSfx("whoosh");
                setView("admin");
              }}
            >
              {tr("admin")}
            </button>
          )}
        </nav>
        <div className="header-meta">
          <OperatorChip user={user} tr={tr} />
          <div className="op-tools">
            {themeControls}
            <button
              type="button"
              className="op-chip"
              onClick={() => {
                unlockSfx();
                playSfx("toggle");
                setLang(lang === "zh" ? "en" : "zh");
              }}
              aria-label={tr("lang")}
              title={tr("lang")}
            >
              <span className="op-chip-ico" aria-hidden>
                文
              </span>
              <span className="op-chip-txt">{tr("lang")}</span>
            </button>
            <button type="button" className="op-chip danger" onClick={doDeleteAccount} aria-label={tr("deleteAccount")} title={tr("deleteAccount")}>
              <span className="op-chip-ico" aria-hidden>
                ✕
              </span>
              <span className="op-chip-txt">{tr("deleteAccount")}</span>
            </button>
            <button type="button" className="op-chip danger" onClick={doLogout} aria-label={tr("logout")} title={tr("logout")}>
              <span className="op-chip-ico" aria-hidden>
                ⎋
              </span>
              <span className="op-chip-txt">{tr("logout")}</span>
            </button>
          </div>
        </div>
      </header>

      {error && <div className="alert bar">{error}</div>}

      {view === "home" && (
        <main className="main page-pad">
          <h2 className="page-title">{tr("dashboard")}</h2>
          <div className="thesis-banner">
            <p>{tr("thesisBody")}</p>
          </div>
          <div className="stat-grid">
            {(
              [
                ["total", dash?.totals?.initiatives],
                ["discussing", dash?.totals?.draft],
                ["running", dash?.totals?.running],
                ["waiting", dash?.totals?.waiting_hitl],
                ["done", dash?.totals?.done],
              ] as const
            ).map(([key, val]) => (
              <div className="stat" key={key}>
                <strong>{val ?? 0}</strong>
                <span>{tr(key)}</span>
              </div>
            ))}
          </div>
          <div className="panel">
            <div className="panel-label">{tr("recent")}</div>
            <div className="list flat">
              {(dash?.recent || []).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="mission"
                  onClick={() => {
                    setSelectedId(item.id);
                    setView("squad");
                  }}
                >
                  <div className="mission-top">
                    <span className="mission-id">{item.id.slice(0, 10)}</span>
                    <span className={`st ${item.status}`}>{statusLabel(lang, item.status)}</span>
                  </div>
                  <strong>{item.title}</strong>
                </button>
              ))}
              {!dash?.recent?.length && <div className="empty-queue">{tr("emptyTasks")}</div>}
            </div>
          </div>
        </main>
      )}

      {view === "intake" && (
        <NewTaskPage
          token={token}
          lang={lang}
          tr={tr}
          busy={busy}
          setBusy={setBusy}
          onCancel={() => setView("tasks")}
          onError={(msg) => setError(msg)}
          onCreated={async (id) => {
            await refreshList();
            await refreshDash();
            setSelectedId(id);
            setView("squad");
          }}
        />
      )}

      {view === "tasks" && (
        <main className="main page-pad">
            <div className="title-row">
              <h2 className="page-title">{tr("taskMgmt")}</h2>
              <button
                type="button"
                className="btn primary"
                onClick={() => {
                  playSfx("whoosh");
                  setView("intake");
                }}
              >
                ＋ {tr("newTask")}
              </button>
            </div>
            <p className="hero-copy">
              {lang === "zh"
                ? "点「＋ 新开任务」进入建队页：与规划官对话提案航线与编制，或直接填写简报并导入 PRD/计划/设计稿等外部资料。"
                : "Open “+ New mission”: chat with the planner for track & loadout, or fill the brief directly with PRD / plan / design materials."}
            </p>
            <div className="filters">
              {(["all", "draft", "running", "waiting_hitl", "done"] as const).map((f) => (
                <button
                  key={f}
                  type="button"
                  className={`filter ${filter === f ? "on" : ""}`}
                  onClick={() => setFilter(f)}
                >
                  {f === "all" ? tr("all") : statusLabel(lang, f === "draft" ? "draft" : f)}
                </button>
              ))}
            </div>
            <div className="list">
              {filtered.map((item) => {
                const shared = item.shared_with || [];
                const canDelete = item.created_by === user?.id || user?.role === "admin";
                return (
                  <div key={item.id} className={`mission-wrap ${item.id === selectedId ? "active" : ""}`}>
                    {/* 卡片用 div 承载：footer 内要放共享/删除按钮，button 嵌 button 非法 */}
                    <div
                      className="mission"
                      role="button"
                      tabIndex={0}
                      onClick={() => {
                        setSelectedId(item.id);
                        setView("squad");
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          setSelectedId(item.id);
                          setView("squad");
                        }
                      }}
                    >
                      <div className="mission-top">
                        <span className="mission-id">{item.id.slice(0, 10)}</span>
                        <span className={`st ${item.status}`}>{statusLabel(lang, item.status)}</span>
                      </div>
                      <strong>{item.title}</strong>
                      <span className="mission-stage">
                        {item.current_stage} · {(item.participants || []).length} {tr("membersInSquad")}
                      </span>
                      <div className="mission-foot">
                        {/* 协作者头像堆叠：被共享账号首字母，最多 3 个 + 溢出计数 */}
                        <span className="avstack">
                          {shared.length ? (
                            <>
                              {shared.slice(0, 3).map((s, idx) => (
                                <span
                                  className={`avatar av-${idx % 3}`}
                                  key={s.user_id}
                                  title={s.display_name || s.username || s.user_id}
                                >
                                  {(s.display_name || s.username || "?").slice(0, 1).toUpperCase()}
                                </span>
                              ))}
                              {shared.length > 3 && <span className="avatar av-more">+{shared.length - 3}</span>}
                            </>
                          ) : (
                            <span className="av-empty">{tr("shareEmptyShort")}</span>
                          )}
                        </span>
                        <span className="foot-x">
                          {/* 共享入口：任务列表页管理共享（owner 添加/移除，被共享者可退出） */}
                          <button
                            type="button"
                            className="add-dot"
                            title={tr("sharedUsers")}
                            onClick={(e) => {
                              e.stopPropagation();
                              setShareModalId(item.id);
                            }}
                          >
                            ＋
                          </button>
                          {/* 删除是 owner 专属权限：被共享用户无删除入口（旧数据 created_by 为空保持原行为） */}
                          {canDelete && (
                            <button
                              type="button"
                              className="mission-del round"
                              onClick={(e) => {
                                e.stopPropagation();
                                removeTask(item.id);
                              }}
                            >
                              ×
                            </button>
                          )}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
              {!filtered.length && <div className="empty-queue">{tr("emptyTasks")}</div>}
            </div>
            {shareModalId &&
              (() => {
                const target = items.find((i) => i.id === shareModalId);
                return target ? (
                  <ShareManager
                    token={token}
                    lang={lang}
                    initiative={target}
                    currentUser={user}
                    onClose={() => setShareModalId(null)}
                    onChanged={onShareChanged}
                    onError={setError}
                  />
                ) : null;
              })()}
        </main>
      )}

      {view === "squad" && selected && (
        <main className="main page-pad">
          <div className="topbar">
            <div>
              <p className="eyebrow">
                {selected.room_id || selected.id}
                {selected.meta?.pipeline_track ? ` · ${String(selected.meta.pipeline_track)}` : ""}
              </p>
              <h2 className="mission-title">{selected.title}</h2>
              <div className="harness-row" title={tr("harnessDraftHint")}>
                <span className="panel-label">{tr("harness")}</span>
                {selected.status === "draft" || selected.status === "stopped" ? (
                  <>
                    <select
                      value={harnessPick}
                      disabled={busy || harnesses.length === 0}
                      onChange={(e) => {
                        setHarnessPick(e.target.value);
                        void saveHarness(e.target.value);
                      }}
                    >
                      {(harnesses.length
                        ? harnesses
                        : [{ name: String(selected.harness || "claude_code"), label: String(selected.harness || "claude_code"), available: true, probe: { status: "ok", detail: "" } }]
                      ).map((h) => (
                        <option key={h.name} value={h.name}>
                          {h.label} ({h.available ? tr("harnessAvailable") : tr("harnessUnavailable")})
                        </option>
                      ))}
                    </select>
                    {harnessLocked && <span className="st">🔒 {tr("harnessLocked")}</span>}
                  </>
                ) : (
                  <span className="st">
                    {String(selected.harness || "claude_code")}
                    {harnessLocked ? ` · 🔒 ${tr("harnessLocked")}` : ""}
                  </span>
                )}
              </div>
            </div>
            <div className="topbar-actions">
              <span className={`st large ${selected.status}`}>{statusLabel(lang, selected.status)}</span>
              {selected.status === "draft" && !selected.meta?.pipeline_confirmed && (
                <button className="btn primary" type="button" disabled={busy} onClick={confirmPipeline}>
                  {tr("pipelineConfirm")}
                </button>
              )}
              {selected.status === "draft" && (
                <button
                  className="btn primary"
                  type="button"
                  disabled={busy || !selected.meta?.pipeline_confirmed}
                  onClick={startPipeline}
                  title={!selected.meta?.pipeline_confirmed ? tr("startNeedConfirm") : undefined}
                >
                  {tr("startPipeline")}
                </button>
              )}
              <button className="btn danger" type="button" disabled={busy} onClick={() => void stopPipeline()}>
                {tr("stop")}
              </button>
              <button className="btn ghost" type="button" onClick={() => setView("tasks")}>
                ←
              </button>
            </div>
          </div>
          <div className="rail-wrap">
            <PipelineRail
              stage={selected.current_stage}
              status={selected.status}
              stages={railFromInitiative(selected, lang)}
              selectedId={inspectStage?.id}
              speakingRoles={speakingRoles}
              lang={lang}
              missionKey={selected.id}
              activeParallel={
                Array.isArray(selected.meta?.active_parallel)
                  ? (selected.meta.active_parallel as string[])
                  : undefined
              }
              onSelect={(id, label) =>
                setInspectStage((prev) => (prev?.id === id ? null : { id, label }))
              }
            />
          </div>
          {inspectStage ? (
            <StageInspect
              stageId={inspectStage.id}
              stageLabel={inspectStage.label}
              initiative={selected}
              messages={messages}
              lang={lang}
              onClose={() => setInspectStage(null)}
              onOpenArtifact={async (path) => openArtifact(path)}
              onRevealPath={(path) => void revealPath(path)}
              workspaceArtifactsDir={workspace?.artifacts_dir}
              labels={{
                title: lang === "zh" ? "节点详情" : "Stage detail",
                loop: lang === "zh" ? "执行 / Loop" : "Runs / loop",
                docs: lang === "zh" ? "产出文档" : "Artifacts",
                empty: lang === "zh" ? "该节点暂无记录" : "No records for this stage yet",
                close: lang === "zh" ? "收起" : "Close",
                role: lang === "zh" ? "角色" : "Role",
                executor: lang === "zh" ? "执行器" : "Executor",
                chat: lang === "zh" ? "频道" : "Channel",
                consult: lang === "zh" ? "上下游确认" : "Consult review",
                tools: lang === "zh" ? "工具调用" : "Toolkit usage",
                reveal: lang === "zh" ? "打开目录" : "Open folder",
              }}
            />
          ) : null}
          {selected.status === "draft" && (
            <PipelineEditor
              selected={selected}
              lang={lang}
              tr={tr}
              busy={busy}
              pipelineEditing={pipelineEditing}
              setPipelineEditing={setPipelineEditing}
              pipelineDraft={pipelineDraft}
              setPipelineDraft={setPipelineDraft}
              stageCatalog={stageCatalog}
              onMoveStage={movePipelineStage}
              onRemoveStage={removePipelineStage}
              onToggleHitl={togglePipelineHitl}
              onAddStage={addPipelineStage}
              onSaveDraft={savePipelineDraft}
            />
          )}
          {/* Reviewing a finished/other node: focus StageInspect, hide the channel strip */}
          {!(
            inspectStage &&
            (inspectStage.id !== selected.current_stage || selected.status === "done")
          ) && (
          <div className={`room-layout ${crewOpen || lootOpen || briefOpen || subroomsOpen || sharesOpen || memPanelOpen ? "with-side" : "solo"}`}>
            <section className={`panel chat-panel${chatFullscreen ? " fullscreen" : ""}`}>
              {chatFullscreen && (
                <button
                  type="button"
                  className="btn ghost tiny chat-fs-exit"
                  onClick={() => setChatFullscreen(false)}
                  title={lang === "zh" ? "退出全屏 (Esc)" : "Exit fullscreen (Esc)"}
                >
                  ⤡ {lang === "zh" ? "退出全屏" : "Exit"}
                </button>
              )}
              <div className="chat-head">
                <div className="panel-label">
                  {tr("squadChat")} · {tr("squadChatHint")}
                </div>
                <div className="chat-head-tools">
              <div className="room-dock">
                <button
                  type="button"
                  className={`room-dock-btn ${crewOpen ? "on" : ""}`}
                  onClick={() => {
                    playSfx("toggle");
                    setCrewOpen((v) => !v);
                  }}
                >
                  {tr("crew")}
                  <em>{(selected.roles || []).length}</em>
                </button>
                <button
                  type="button"
                  className={`room-dock-btn ${lootOpen ? "on" : ""}`}
                  onClick={() => {
                    playSfx("toggle");
                    setLootOpen((v) => !v);
                  }}
                >
                  {tr("artifacts")}
                  <em>{(selected.artifacts || []).length}</em>
                </button>
                <button
                  type="button"
                  className={`room-dock-btn ${briefOpen ? "on" : ""}`}
                  onClick={() => {
                    playSfx("toggle");
                    setBriefOpen((v) => !v);
                  }}
                >
                  BRIEF
                </button>
                <button
                  type="button"
                  className={`room-dock-btn ${subroomsOpen ? "on" : ""}`}
                  onClick={() => {
                    playSfx("toggle");
                    setSubroomsOpen((v) => !v);
                  }}
                >
                  {tr("subrooms")}
                  <em>{subroomCount}</em>
                </button>
                <button
                  type="button"
                  className={`room-dock-btn ${sharesOpen ? "on" : ""}`}
                  onClick={() => {
                    playSfx("toggle");
                    setSharesOpen((v) => !v);
                  }}
                >
                  {tr("share")}
                  <em>{(selected.shared_with || []).length}</em>
                </button>
                <button
                  type="button"
                  className={`room-dock-btn ${memPanelOpen ? "on" : ""}`}
                  onClick={() => {
                    playSfx("toggle");
                    setMemPanelOpen((v) => !v);
                  }}
                >
                  {tr("memory")}
                  <em>{((selected.meta?.memory_ids as string[]) || []).length}</em>
                </button>
              </div>
                  <button
                    type="button"
                    className="btn ghost tiny chat-fs-btn"
                    onClick={() => {
                      playSfx("toggle");
                      setChatFullscreen(true);
                    }}
                    title={lang === "zh" ? "全屏查看 (Esc 退出)" : "Fullscreen (Esc to exit)"}
                  >
                    ⛶
                  </button>
                </div>
              </div>
              <div className="participants" title={tr("discussToggleHint")}>
                {(selected.participants || []).map((p) => {
                  const on = p.discussing !== false;
                  return (
                    <button
                      key={p.id}
                      type="button"
                      className={`part ${p.kind}${on ? " discussing" : " spectating"}`}
                      disabled={busy}
                      onClick={() => void toggleDiscussing(p.id, !on)}
                      title={on ? tr("discussOn") : tr("discussOff")}
                    >
                      {p.kind === "agent" ? "◆" : "●"} {p.name}
                      {p.role ? `/${p.role}` : ""}
                      <em>{on ? tr("discussOn") : tr("discussOff")}</em>
                    </button>
                  );
                })}
              </div>
              <div className="chat-log">
                {messages.map((m) => {
                  const isSys =
                    m.msg_type === "system" ||
                    m.msg_type === "stage" ||
                    m.actor_kind === "system";
                  if (isSys) {
                    return (
                      <div key={m.id} className="wire sys">
                        <span className="sysline">
                          {m.text}
                          <span className="sys-time"> · {displayTime(m.at)}</span>
                        </span>
                      </div>
                    );
                  }
                  return (
                    <div key={m.id} className={`wire ${m.actor_kind} type-${m.msg_type}`}>
                      <div className="byline">
                        <div className="who">
                          <span className={`dot ${m.actor_kind}`} />
                          {m.actor_name}
                        </div>
                        {m.role ? <div className="what">{m.role}</div> : null}
                        <div className="when">{displayTime(m.at)}</div>
                      </div>
                      <div className="copy">
                        {m.msg_type === "chat" || m.msg_type === "stage" || m.msg_type === "consult" || m.msg_type === "consult_reply" ? (
                          <MarkdownPreview text={m.text} />
                        ) : (
                          <pre>{m.text}</pre>
                        )}
                      </div>
                    </div>
                  );
                })}
                <div ref={chatEndRef} />
              </div>
              {selected.status === "waiting_hitl" && selected.pending_hitl && (
                <div className="hitl-inline">
                  <span>
                    {tr("gate")} · {selected.pending_hitl.stage}
                  </span>
                  <button type="button" className="btn primary" disabled={busy} onClick={() => submitHitl("approve")}>
                    {tr("approve")}
                  </button>
                  <button type="button" className="btn" disabled={busy} onClick={() => submitHitl("reject")}>
                    {tr("reject")}
                  </button>
                </div>
              )}
              <ChatComposer
                value={chatInput}
                onChange={setChatInput}
                onSubmit={sendChat}
                placeholder={tr("chatPlaceholder")}
                hint={tr("squadCommandsHelp")}
                sendLabel={tr("send")}
                disabled={busy}
                mentions={mentionOptions}
                commands={commandOptions}
                onEnhance={enhanceRoomInput}
                enhanceLabel={tr("enhancePrompt")}
                enhanceUndoLabel={tr("enhanceUndo")}
                enhanceLoadingLabel={tr("enhanceLoading")}
              />
            </section>
            {(crewOpen || lootOpen || briefOpen || sharesOpen || memPanelOpen) && (
              <div className="stack room-side">
                {crewOpen && (
                  <section className="panel room-drawer">
                    <div className="panel-label row-between">
                      <span>{tr("crew")}</span>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        onClick={() => {
                          playSfx("click");
                          setCrewOpen(false);
                        }}
                      >
                        {tr("collapsePanel")}
                      </button>
                    </div>
                    <p className="loadout-hint">{tr("crewAvatarHint")}</p>
                    <div className="roles">
                      {(selected.roles || []).map((r) => (
                        <div className={`role ${avatarPickRoleId === r.id ? "picking" : ""}`} key={r.id}>
                          <RoleAvatar
                            avatar={r.avatar}
                            role={r.role}
                            title={r.title}
                            size={44}
                            onClick={() =>
                              setAvatarPickRoleId((cur) => (cur === r.id ? null : r.id))
                            }
                          />
                          <div>
                            <strong>{r.title}</strong>
                            <code>{r.executor}</code>
                          </div>
                          {avatarPickRoleId === r.id ? (
                            <div className="avatar-picker">
                              {AVATAR_PRESET_OPTIONS.map((p) => (
                                <RoleAvatar
                                  key={p.id}
                                  avatar={p.id}
                                  role={p.role}
                                  size={36}
                                  onClick={() => void setRoleAvatar(r.id, p.id)}
                                />
                              ))}
                              <button
                                type="button"
                                className="btn ghost avatar-upload-btn"
                                disabled={busy}
                                onClick={() => avatarFileRef.current?.click()}
                              >
                                {tr("crewAvatarUpload")}
                              </button>
                              <input
                                ref={avatarFileRef}
                                type="file"
                                accept="image/*,.webp,image/webp"
                                hidden
                                onChange={(e) => {
                                  const f = e.target.files?.[0];
                                  e.target.value = "";
                                  void uploadRoleAvatarFile(r.id, f);
                                }}
                              />
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </section>
                )}
                {lootOpen && (
                  <section className="panel room-drawer loot-drawer">
                    <div className="panel-label row-between">
                      <span>{tr("artifacts")}</span>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        onClick={() => {
                          playSfx("click");
                          setLootOpen(false);
                        }}
                      >
                        {tr("collapsePanel")}
                      </button>
                    </div>
                    <div className="workspace-actions">
                      <button
                        type="button"
                        className="btn ghost tiny"
                        disabled={!workspace?.artifacts_dir}
                        onClick={() => void revealPath(workspace?.artifacts_dir || "")}
                        title={workspace?.artifacts_dir || ""}
                      >
                        {tr("openArtifactsFolder")}
                      </button>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        disabled={!workspace?.worktree_dir}
                        onClick={() => void revealPath(workspace?.worktree_dir || "")}
                        title={workspace?.worktree_dir || ""}
                      >
                        {tr("openWorktreeFolder")}
                      </button>
                      {workspace?.git_repo ? (
                        <button
                          type="button"
                          className="btn ghost tiny"
                          onClick={() => void revealPath(workspace.git_repo || "")}
                          title={workspace.git_repo}
                        >
                          {tr("openGitRepo")}
                        </button>
                      ) : null}
                    </div>
                    {workspace ? (
                      <p className="workspace-paths muted">
                        <span>artifacts: {workspace.artifacts_dir}</span>
                        <span>
                          worktree: {workspace.worktree_exists ? workspace.worktree_dir : tr("noWorktreeYet")}
                        </span>
                        {workspace.git_repo ? <span>git: {workspace.git_repo}</span> : null}
                      </p>
                    ) : null}
                    <div className="artifacts">
                      {(selected.artifacts || []).map((a) => (
                        <button
                          key={a.path}
                          type="button"
                          className={artifactPreviewPath === a.path ? "active" : undefined}
                          onClick={() => void openArtifact(a.path, a.kind)}
                        >
                          <span className="art-stage">{a.stage}</span>
                          <span className="art-kind">{a.kind}</span>
                        </button>
                      ))}
                      {!selected.artifacts?.length && <p className="empty-queue">{tr("noArtifacts")}</p>}
                    </div>
                    {workspace?.worktree_exists && workspace.worktree_files.length > 0 ? (
                      <div className="worktree-files">
                        <div className="panel-label tiny">{tr("worktreeFiles")}</div>
                        <div className="artifacts worktree-list">
                          {workspace.worktree_files.map((f) => (
                            <button
                              key={f.path}
                              type="button"
                              onClick={() =>
                                void revealPath(`${workspace.worktree_dir}/${f.path}`)
                              }
                              title={f.path}
                            >
                              <span className="art-stage">{f.path.includes("/") ? f.path.split("/").slice(0, -1).join("/") : "·"}</span>
                              <span className="art-kind">{f.name}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    {artifactPreview ? (
                      looksLikeMarkdown(artifactPreviewPath || artifactPreviewKind) ? (
                        <MarkdownPreview text={artifactPreview} className="loot-md-preview" />
                      ) : (
                        <pre className="preview">{artifactPreview}</pre>
                      )
                    ) : null}
                  </section>
                )}
                {briefOpen && (
                  <section className="panel room-drawer">
                    <div className="panel-label row-between">
                      <span>BRIEF</span>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        onClick={() => {
                          playSfx("click");
                          setBriefOpen(false);
                        }}
                      >
                        {tr("collapsePanel")}
                      </button>
                    </div>
                    <pre className="preview dim">{selected.brief}</pre>
                  </section>
                )}
                {subroomsOpen && (
                  <section className="panel room-drawer">
                    <div className="panel-label row-between">
                      <span>{tr("subrooms")}</span>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        onClick={() => {
                          playSfx("click");
                          setSubroomsOpen(false);
                        }}
                      >
                        {tr("collapsePanel")}
                      </button>
                    </div>
                    <p className="tiny subroom-drawer-hint">{tr("subroomsHint")}</p>
                    <SubRoomPanel
                      token={token}
                      lang={lang}
                      initiativeId={selected.id}
                      liveSignal={subroomSignal}
                      onCount={setSubroomCount}
                      roleTitles={roleTitles}
                      variant="dock"
                    />
                  </section>
                )}
                {sharesOpen && (
                  <section className="panel room-drawer share-drawer">
                    <div className="panel-label row-between">
                      <span>{tr("sharedUsers")}</span>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        onClick={() => {
                          playSfx("click");
                          setSharesOpen(false);
                        }}
                      >
                        {tr("collapsePanel")}
                      </button>
                    </div>
                    <p className="tiny subroom-drawer-hint">{tr("shareHint")}</p>
                    {/* 详情页只读展示共享成员；添加/移除在任务列表页的共享弹窗里操作 */}
                    <div className="share-list">
                      <div className="share-row">
                        <span className="share-name">
                          {isMissionOwner ? tr("shareSelfOwner") : tr("shareOwner")}
                        </span>
                        <em className="share-tag">{tr("shareOwnerTag")}</em>
                      </div>
                      {shares.map((s) => (
                        <div className="share-row" key={s.user_id}>
                          <span className="share-name">
                            {s.display_name || s.username || s.user_id}
                            {s.username ? <code>{s.username}</code> : null}
                          </span>
                        </div>
                      ))}
                      {!shares.length && <p className="empty-queue">{tr("shareEmpty")}</p>}
                    </div>
                  </section>
                )}
                {memPanelOpen && (
                  <section className="panel room-drawer">
                    <div className="panel-label row-between">
                      <span>{tr("memory")}</span>
                      <button
                        type="button"
                        className="btn ghost tiny"
                        onClick={() => {
                          playSfx("click");
                          setMemPanelOpen(false);
                        }}
                      >
                        {tr("collapsePanel")}
                      </button>
                    </div>
                    <p className="tiny subroom-drawer-hint">{tr("memBound")}</p>
                    {missionMemories?.bound.length ? (
                      <div className="share-list">
                        {missionMemories.bound.map((m) => {
                          const wasRead = missionMemories.read_ids.includes(m.id);
                          return (
                            <div className="share-row" key={m.id}>
                              <span className="share-name" style={{ flexDirection: "column", alignItems: "flex-start" }}>
                                <span>
                                  <code>{m.kind}</code> {memTargetLabel(m)}
                                  {wasRead ? <em className="share-tag" style={{ marginLeft: "0.4rem" }}>{tr("memRead")}</em> : null}
                                </span>
                                <span className="tiny">{m.summary || m.content}</span>
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <p className="empty-queue">{tr("memPanelEmpty")}</p>
                    )}
                  </section>
                )}
              </div>
            )}
          </div>
          )}
        </main>
      )}

      {view === "knowledge" && (
        <>
          <main className="main page-pad">
            {!kbBase ? (
              <>
                <div className="title-row">
                  <div>
                    <h2 className="page-title">知识库</h2>
                    <p className="kb-page-sub">产品 / 方案 / 手册——供 Cursor 等辅助工具检索负重</p>
                  </div>
                  <button className="btn primary kb-btn-candy" type="button" onClick={openKbCreate}>
                    ＋ 新建知识库
                  </button>
                </div>
                <div className="kb-toolbar">
                  <div className="kb-seg">
                    {(
                      [
                        ["all", "全部", kbBases.length],
                        ["global", "全局共享", kbScopeCounts.global],
                        ["user", "我的知识库", kbScopeCounts.user],
                        ["task", "任务知识库", kbScopeCounts.task],
                      ] as const
                    ).map(([key, label, count]) => (
                      <button
                        key={key}
                        type="button"
                        className={kbBaseFilter === key ? "on" : ""}
                        onClick={() => setKbBaseFilter(key)}
                      >
                        {label} <i>{count}</i>
                      </button>
                    ))}
                  </div>
                  <input
                    className="kb-search-input"
                    placeholder="搜索知识库名称或简介…"
                    value={kbBaseQuery}
                    onChange={(e) => setKbBaseQuery(e.target.value)}
                  />
                </div>
                <div className="kb-grid">
                  {kbVisibleBases.map((b) => (
                    <article
                      key={b.id}
                      className={`kb-card scope-${b.scope}`}
                      onClick={() => {
                        setKbBaseId(b.id);
                        setKbQuery("");
                      }}
                    >
                      <div className="kb-card-top">
                        <strong>{b.name}</strong>
                        {b.scope === "global" && <span className="kb-badge scope-global">全局</span>}
                      </div>
                      <p className="kb-card-desc">{b.description || "—"}</p>
                      <p className="kb-card-meta">
                        {b.doc_count ?? 0} 篇 ·{" "}
                        {b.scope === "global" ? "admin 维护" : b.scope === "task" ? "挂靠任务" : "个人知识库"} ·{" "}
                        {(b.updated_at || "").slice(0, 10)}
                      </p>
                    </article>
                  ))}
                  {!kbVisibleBases.length && <div className="empty-queue">没有匹配的知识库</div>}
                </div>
              </>
            ) : (
              <>
                <p className="kb-crumb">
                  <button type="button" className="kb-crumb-link" onClick={() => setKbBaseId(null)}>
                    知识库
                  </button>
                  <span> / </span>
                  <span>
                    {kbBase.scope === "global" ? "全局共享" : kbBase.scope === "task" ? "任务知识库" : "我的知识库"}
                  </span>
                  <span> / </span>
                  <strong>{kbBase.name}</strong>
                </p>
                <div className="title-row">
                  <div>
                    <h2 className="page-title kb-detail-title">
                      {kbBase.name}
                      {kbBase.scope === "global" && <span className="kb-badge scope-global">全局</span>}
                    </h2>
                    <p className="kb-page-sub">
                      {kbDocs.length} 个文件
                      {kbBase.scope === "task" && <> · 任务 {kbBase.scope_id}</>}
                      {kbBase.description ? ` · ${kbBase.description}` : ""}
                    </p>
                  </div>
                  <div className="topbar-actions">
                    <button
                      className="btn primary kb-btn-candy"
                      type="button"
                      onClick={() => {
                        setKbPendingFiles([]);
                        setKbImportOpen(true);
                      }}
                    >
                      ＋ 导入文件
                    </button>
                    {kbCanManage(kbBase) && (
                      <button className="btn danger" type="button" onClick={() => deleteKbBase(kbBase.id)}>
                        {tr("delete")}
                      </button>
                    )}
                  </div>
                </div>
                <div className="kb-doc-list">
                  {kbDocs.map((doc) => {
                    const ext = (doc.title.split(".").pop() || "").toLowerCase();
                    return (
                      <div key={doc.id} className="kb-doc-row" onClick={() => setKbDocDetail(doc)}>
                        <span className="kb-doc-name">{doc.title}</span>
                        {KB_IMPORT_EXTS.has(ext) && (
                          <span className={`kb-type-badge ext-${ext}`}>{ext}</span>
                        )}
                        {doc.backend === "weknora" && !!doc.remote_id && (
                          <span className={`kb-parse-badge ps-${doc.parse_status || "processing"}`}>
                            {doc.parse_status === "completed"
                              ? "解析完成"
                              : doc.parse_status === "failed"
                                ? "解析失败"
                                : "解析中"}
                          </span>
                        )}
                        <span className="kb-doc-meta">
                          {categoryLabel(lang, doc.category)} · {doc.author_name || "—"} ·{" "}
                          {(doc.updated_at || "").slice(0, 10)}
                        </span>
                        <button
                          className="kb-doc-del"
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteKbDoc(doc.id);
                          }}
                        >
                          {tr("delete")}
                        </button>
                      </div>
                    );
                  })}
                  {!kbDocs.length && (
                    <div className="empty-queue">库里还没有文件，点右上角「＋ 导入文件」</div>
                  )}
                </div>
              </>
            )}
          </main>

          {/* 新建知识库弹窗：分类即可见范围；内置导入区，创建与首次导入一步完成 */}
          {kbCreateOpen && (
            <div className="mem-overlay" onClick={(e) => e.target === e.currentTarget && setKbCreateOpen(false)}>
              <form className="panel mem-modal kb-modal" onSubmit={submitKbCreate}>
                <div className="panel-label row-between">
                  <span>新建知识库</span>
                  <button type="button" className="btn ghost tiny" onClick={() => setKbCreateOpen(false)}>
                    ×
                  </button>
                </div>
                <div className="kb-modal-body">
                  <label className="kb-field">
                    <span>名称 *</span>
                    <input
                      className="kb-input"
                      value={kbBaseForm.name}
                      onChange={(e) => setKbBaseForm({ ...kbBaseForm, name: e.target.value })}
                      placeholder="例如：Short Chat 打招呼文案"
                      required
                    />
                  </label>
                  <label className="kb-field">
                    <span>简介</span>
                    <textarea
                      className="kb-input"
                      rows={2}
                      value={kbBaseForm.description}
                      onChange={(e) => setKbBaseForm({ ...kbBaseForm, description: e.target.value })}
                      placeholder="一句话说明这个库沉淀什么、供谁检索"
                    />
                  </label>
                  <div className="kb-field">
                    <span>分类（即可见范围）</span>
                    <div className="kb-scope-grid">
                      <button
                        type="button"
                        className={`kb-scope-opt${kbBaseForm.scope === "global" ? " on" : ""}`}
                        disabled={user?.role !== "admin"}
                        onClick={() => setKbBaseForm({ ...kbBaseForm, scope: "global" })}
                      >
                        <b>全局共享</b>
                        <em>{user?.role === "admin" ? "全员可见" : "仅 Admin 可创建"}</em>
                      </button>
                      <button
                        type="button"
                        className={`kb-scope-opt${kbBaseForm.scope === "user" ? " on" : ""}`}
                        onClick={() => setKbBaseForm({ ...kbBaseForm, scope: "user" })}
                      >
                        <b>我的知识库</b>
                        <em>仅自己可见</em>
                      </button>
                      <button
                        type="button"
                        className={`kb-scope-opt${kbBaseForm.scope === "task" ? " on" : ""}`}
                        onClick={() => setKbBaseForm({ ...kbBaseForm, scope: "task" })}
                      >
                        <b>任务知识库</b>
                        <em>挂靠任务</em>
                      </button>
                    </div>
                  </div>
                  {kbBaseForm.scope === "task" && (
                    <label className="kb-field">
                      <span>关联任务</span>
                      <select
                        className="kb-input"
                        value={kbBaseForm.task_id}
                        onChange={(e) => setKbBaseForm({ ...kbBaseForm, task_id: e.target.value })}
                        required
                      >
                        <option value="">选择任务…</option>
                        {kbTasks.map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.title} · {t.id}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <div className="kb-field">
                    <span>导入文件（可选，创建后也可在库详情继续导入）</span>
                    <div
                      className="kb-drop"
                      onClick={() => kbCreateFileRef.current?.click()}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => {
                        e.preventDefault();
                        addKbFiles(e.dataTransfer.files);
                      }}
                    >
                      <strong>拖拽文件到此处，或点击选择</strong>
                      <div className="kb-chips">
                        {["docx", "doc", "ppt", "pptx", "csv", "xlsx", "md", "txt", "pdf"].map((x) => (
                          <span key={x} className="kb-chip">
                            {x}
                          </span>
                        ))}
                      </div>
                    </div>
                    <input
                      ref={kbCreateFileRef}
                      type="file"
                      multiple
                      hidden
                      accept={KB_IMPORT_ACCEPT}
                      onChange={(e) => {
                        addKbFiles(e.target.files);
                        e.target.value = "";
                      }}
                    />
                    {kbPendingFiles.map((item, i) => (
                      <div key={`${item.file.name}-${i}`} className={`kb-frow${item.status === "failed" ? " dim" : ""}`}>
                        <span className="kb-fname">{item.file.name}</span>
                        <span className="kb-fmeta">{Math.max(1, Math.round(item.file.size / 1024))} KB</span>
                        <span className={`kb-fstatus st-${item.status}`}>
                          {item.status === "uploading"
                            ? "导入中…"
                            : item.status === "done"
                              ? "已完成"
                              : item.status === "failed"
                                ? item.message || "导入失败"
                                : "待导入"}
                        </span>
                        <button
                          type="button"
                          className="kb-fdel"
                          onClick={() => setKbPendingFiles((prev) => prev.filter((_, idx) => idx !== i))}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="kb-modal-foot">
                  <span className="kb-foot-meta">{kbImportableCount} 个文件将随库导入</span>
                  <button type="button" className="btn ghost" onClick={() => setKbCreateOpen(false)}>
                    {tr("cancel")}
                  </button>
                  <button className="btn primary kb-btn-candy" type="submit" disabled={busy}>
                    创建并导入 ({kbImportableCount})
                  </button>
                </div>
              </form>
            </div>
          )}

          {/* 库详情补充导入弹窗：选中/拖入即自动上传，无需再点按钮 */}
          {kbImportOpen && kbBase && (
            <div className="mem-overlay" onClick={(e) => e.target === e.currentTarget && setKbImportOpen(false)}>
              <div className="panel mem-modal kb-modal kb-modal-import">
                <div className="panel-label row-between">
                  <span>导入文件 · {kbBase.name}</span>
                  <button type="button" className="btn ghost tiny" onClick={() => setKbImportOpen(false)}>
                    ×
                  </button>
                </div>
                <div className="kb-modal-body">
                  <div
                    className="kb-drop"
                    onClick={() => kbImportFileRef.current?.click()}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                      e.preventDefault();
                      addKbFiles(e.dataTransfer.files, kbBase.id);
                    }}
                  >
                    <strong>拖拽文件到此处，或点击选择（选中后自动上传）</strong>
                    <div className="kb-chips">
                      {["docx", "doc", "ppt", "pptx", "csv", "xlsx", "md", "txt", "pdf"].map((x) => (
                        <span key={x} className="kb-chip">
                          {x}
                        </span>
                      ))}
                    </div>
                  </div>
                  <input
                    ref={kbImportFileRef}
                    type="file"
                    multiple
                    hidden
                    accept={KB_IMPORT_ACCEPT}
                    onChange={(e) => {
                      addKbFiles(e.target.files, kbBase.id);
                      e.target.value = "";
                    }}
                  />
                  {kbPendingFiles.map((item) => (
                    <div key={item.uid} className={`kb-frow${item.status === "failed" ? " dim" : ""}`}>
                      <span className="kb-fname">{item.file.name}</span>
                      <span className="kb-fmeta">{Math.max(1, Math.round(item.file.size / 1024))} KB</span>
                      <span className={`kb-fstatus st-${item.status}`}>
                        {item.status === "uploading"
                          ? "导入中…"
                          : item.status === "done"
                            ? "已完成"
                            : item.status === "failed"
                              ? item.message || "导入失败"
                              : "待导入"}
                      </span>
                      <button
                        type="button"
                        className="kb-fdel"
                        onClick={() => setKbPendingFiles((prev) => prev.filter((p) => p.uid !== item.uid))}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
                <div className="kb-modal-foot">
                  <span className="kb-foot-meta">
                    {kbImportDoneCount} 个已完成{kbImportFailedCount ? ` · ${kbImportFailedCount} 个失败` : ""}
                  </span>
                  <button
                    type="button"
                    className="btn primary kb-btn-candy"
                    onClick={() => {
                      setKbImportOpen(false);
                      setKbPendingFiles([]);
                    }}
                  >
                    完成
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* 文档详情弹窗：Markdown 渲染全文 */}
          {kbDocDetail && (
            <div className="mem-overlay" onClick={(e) => e.target === e.currentTarget && setKbDocDetail(null)}>
              <div className="panel mem-modal mem-detail-modal" onClick={(e) => e.stopPropagation()}>
                <div className="mem-detail-meta">
                  <strong>{kbDocDetail.title}</strong>
                  <span className="mem-detail-sep" />
                  <span className="mem-detail-scope">{categoryLabel(lang, kbDocDetail.category)}</span>
                  <span className="mem-detail-author">
                    {kbDocDetail.author_name || "—"} · {(kbDocDetail.updated_at || "").slice(0, 10)}
                  </span>
                </div>
                <div className="mem-detail-scroll">
                  <MarkdownPreview text={kbDocDetail.body || kbDocDetail.summary} />
                </div>
                <div className="kb-modal-foot">
                  <span className="kb-foot-meta">{(kbDocDetail.tags || []).join(" · ")}</span>
                  <button className="btn ghost" type="button" onClick={() => deleteKbDoc(kbDocDetail.id)}>
                    {tr("delete")}
                  </button>
                  <button className="btn primary" type="button" onClick={() => setKbDocDetail(null)}>
                    关闭
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}

      {view === "memory" && (
        <main className="main page-pad">
          <div className="title-row">
            <h2 className="page-title">{tr("memoryTitle")}</h2>
            <button
              className="btn primary"
              type="button"
              onClick={() => {
                playSfx("click");
                setMemForm({ ...memForm, content: "" });
                setMemModalOpen(true);
              }}
            >
              ＋ {tr("memNew")}
            </button>
          </div>
          <p className="hero-copy">{tr("memoryHint")}</p>
          {/* 工具行：范围分段 + 类型筛选 + 搜索 */}
          <div className="mem-toolbar">
            <div className="mem-seg">
              {(["all", "workspace", "user"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  className={memScopeFilter === s ? "on" : ""}
                  onClick={() => setMemScopeFilter(s)}
                >
                  {s === "all" ? tr("scopeAll") : s === "workspace" ? tr("scopeGlobalShort") : tr("scopeMineShort")}
                </button>
              ))}
            </div>
            {(["", "preference", "lesson", "episode", "fact"] as const).map((k) => (
              <button
                key={k || "all"}
                type="button"
                className={`mem-chip ${memKindFilter === k ? "on" : ""}`}
                onClick={() => setMemKindFilter(k)}
              >
                {k ? tr(memKindLabel[k]) : tr("scopeAll")}
              </button>
            ))}
            <input
              className="mem-search"
              value={memQuery}
              onChange={(e) => setMemQuery(e.target.value)}
              placeholder={tr("memSearch")}
            />
          </div>
          {/* 记忆卡网格：类型色条 + 衬线引语（方案三签名细节） */}
          <div className="memory-grid">
            {filteredMemories.map((m) => (
              <div
                key={m.id}
                className={`memory-card k-${m.kind}`}
                role="button"
                tabIndex={0}
                onClick={() => setMemDetail(m)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") setMemDetail(m);
                }}
              >
                <button
                  type="button"
                  className="btn ghost tiny mem-del"
                  onClick={(e) => {
                    e.stopPropagation();
                    void deleteMemory(m.id);
                  }}
                >
                  {tr("delete")}
                </button>
                <div className="mem-card-top">
                  <span className="kind-tag">{tr(memKindLabel[m.kind])}</span>
                  {/* 治理态条目（superseded/expired）角标：失效不删除，可点详情追溯演变链 */}
                  {m.status && m.status !== "active" && (
                    <span className="mem-status-badge">{tr("memInactive")}</span>
                  )}
                  <span className="mem-card-scope">{memTargetLabel(m)}</span>
                </div>
                <p className="memory-quote">
                  {m.summary_status === "pending" ? (
                    <span className="mem-summary-pending">
                      <span className="mini-spinner" aria-hidden="true" />
                      {tr("memSummaryPending")}
                    </span>
                  ) : (
                    <>「{m.summary || m.content}」</>
                  )}
                </p>
                <div className="mem-card-meta">
                  {(m.tags || []).length ? `${(m.tags || []).join(" · ")} · ` : ""}
                  {m.author_name || ""}
                  {m.created_at ? ` · ${displayTime(m.created_at)}` : ""}
                </div>
              </div>
            ))}
            {!filteredMemories.length && <div className="empty-queue">{tr("emptyMemory")}</div>}
          </div>
          {memModalOpen && (
            <div className="mem-overlay" onClick={() => setMemModalOpen(false)}>
              <form className="panel mem-modal" onSubmit={saveMemory} onClick={(e) => e.stopPropagation()}>
                <div className="panel-label row-between">
                  <span>{tr("memNew")}</span>
                  <button type="button" className="btn ghost tiny" onClick={() => setMemModalOpen(false)}>
                    ×
                  </button>
                </div>
                {/* 左右分栏：左编辑 / 右实时预览；弹框放大，md 预览常驻不再受字数阈值限制 */}
                <div className="mem-modal-main">
                  <div className="mem-edit">
                    <div className="mem-modal-row2">
                      <label>
                        <span>{tr("memScope")}</span>
                        {/* 全局约定（全员共享）或我的（支持三种生效范围） */}
                        <select
                          value={memForm.scope}
                          onChange={(e) =>
                            setMemForm({
                              ...memForm,
                              scope: e.target.value as MemoryScope,
                              scope_id: e.target.value === "workspace" ? "global" : "",
                            })
                          }
                        >
                          <option value="workspace">{tr("scopeGlobalShort")}</option>
                          <option value="user">{tr("scopeMineShort")}</option>
                        </select>
                      </label>
                      <label>
                        <span>{tr("memKind")}</span>
                        <select
                          value={memForm.kind}
                          onChange={(e) => setMemForm({ ...memForm, kind: e.target.value as MemoryKind })}
                        >
                          <option value="preference">{tr("kindPreference")}</option>
                          <option value="lesson">{tr("kindLesson")}</option>
                          <option value="episode">{tr("kindEpisode")}</option>
                          <option value="fact">{tr("kindFact")}</option>
                        </select>
                      </label>
                    </div>
                    {memForm.scope === "user" && (
                      <>
                        <div>
                          <span className="kind-tag">{tr("memTarget")}</span>
                          <div className="mem-seg" style={{ marginTop: "0.3rem" }}>
                            {(["all", "mission", "agent"] as const).map((t) => (
                              <button
                                key={t}
                                type="button"
                                className={memTarget === t ? "on" : ""}
                                onClick={() => {
                                  setMemTarget(t);
                                  setMemForm({ ...memForm, scope_id: "" });
                                }}
                              >
                                {t === "all" ? tr("memTargetAll") : t === "mission" ? tr("memTargetMission") : tr("memTargetAgent")}
                              </button>
                            ))}
                          </div>
                        </div>
                        {memTarget === "mission" && (
                          <label>
                            <span>{tr("memPickTask")}</span>
                            <select
                              value={memForm.scope_id}
                              onChange={(e) => setMemForm({ ...memForm, scope_id: e.target.value })}
                              required
                            >
                              <option value="">—</option>
                              {items.map((i) => (
                                <option key={i.id} value={i.id}>
                                  {i.title}（{i.id.slice(0, 10)}）
                                </option>
                              ))}
                            </select>
                          </label>
                        )}
                        {memTarget === "agent" && (
                          <label>
                            <span>{tr("memPickAgent")}</span>
                            <select
                              value={memForm.scope_id}
                              onChange={(e) => setMemForm({ ...memForm, scope_id: e.target.value })}
                              required
                            >
                              <option value="">—</option>
                              {agentList.map((a) => (
                                <option key={a.id} value={a.id}>
                                  {a.title}
                                </option>
                              ))}
                            </select>
                          </label>
                        )}
                      </>
                    )}
                    <label className="mem-content-label">
                      <span className="mem-content-head">
                        {tr("memContent")}
                        <span className="mem-content-tools">
                          {memEnhancePrev !== null && !memEnhancing && (
                            <button
                              type="button"
                              className="btn ghost tiny composer-enhance-btn composer-enhance-icon"
                              onClick={() => {
                                setMemForm({ ...memForm, content: memEnhancePrev });
                                setMemEnhancePrev(null);
                              }}
                              title={tr("enhanceUndo")}
                              aria-label={tr("enhanceUndo")}
                            >
                              ↺
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn ghost tiny composer-enhance-btn composer-enhance-icon"
                            disabled={memEnhancing || !memForm.content.trim()}
                            onClick={() => void enhanceMemoryContent()}
                            title={tr("enhancePrompt")}
                            aria-label={tr("enhancePrompt")}
                          >
                            {memEnhancing ? (
                              <span className="mini-spinner" aria-hidden />
                            ) : (
                              <SparkleIcon />
                            )}
                          </button>
                        </span>
                      </span>
                      <textarea
                        value={memForm.content}
                        onChange={(e) => {
                          setMemForm({ ...memForm, content: e.target.value });
                          if (memEnhancePrev !== null) setMemEnhancePrev(null);
                        }}
                        required
                        autoFocus
                      />
                      {/* 增强中浮层：与聊天输入框一致的效果 */}
                      {memEnhancing ? (
                        <span className="composer-enhance-toast" role="status">
                          <span className="mini-spinner" aria-hidden />
                          {tr("enhanceLoading")}
                        </span>
                      ) : null}
                    </label>
                  </div>
                  {/* 右栏：Markdown 实时渲染，导入文档后立即可读 */}
                  <div className="mem-preview-pane">
                    <div className="mem-preview-head">
                      <span className="flabel">{tr("memPreview")}</span>
                    </div>
                    {memForm.content.trim() ? (
                      <MarkdownPreview text={memForm.content} />
                    ) : (
                      <p className="mem-preview-empty">{tr("memImportHint")}</p>
                    )}
                  </div>
                </div>
                {/* 底栏：导入 / 元信息摘要 / 写入 */}
                <div className="mem-modal-foot">
                  <button type="button" className="btn ghost" onClick={() => memImportRef.current?.click()}>
                    {tr("memImport")}
                  </button>
                  <input
                    ref={memImportRef}
                    type="file"
                    accept=".md,.markdown,.txt,text/markdown,text/plain"
                    hidden
                    onChange={(e) => {
                      importMemoryMd(e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                  <span className="mem-foot-meta">
                    {memForm.scope === "workspace"
                      ? `${tr("scopeGlobalShort")} · ${tr("memTargetAll")}`
                      : `${tr("scopeMineShort")} · ${
                          memTarget === "all"
                            ? tr("memTargetAll")
                            : memTarget === "mission"
                              ? tr("memTargetMission")
                              : tr("memTargetAgent")
                        }`}
                  </span>
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("newMemory")}
                  </button>
                </div>
              </form>
            </div>
          )}
          {memDetail && (
            <div className="mem-overlay" onClick={() => setMemDetail(null)}>
              <div className="panel mem-modal mem-detail-modal" onClick={(e) => e.stopPropagation()}>
                <div className="panel-label row-between">
                  <span>{tr("memDetailTitle")}</span>
                  <button type="button" className="btn ghost tiny" onClick={() => setMemDetail(null)}>
                    ×
                  </button>
                </div>
                {/* 元信息条：类型 · 范围 · 标签 · 作者 · 时间 */}
                <div className="mem-detail-meta">
                  <span className="kind-tag">{tr(memKindLabel[memDetail.kind])}</span>
                  <span className="mem-detail-sep" />
                  <span className="mem-detail-scope">{memTargetLabel(memDetail)}</span>
                  {(memDetail.tags || []).length ? (
                    <>
                      <span className="mem-detail-sep" />
                      <span className="mem-detail-tags">
                        {(memDetail.tags || []).map((t) => `#${t}`).join(" ")}
                      </span>
                    </>
                  ) : null}
                  <span className="mem-detail-author">
                    {memDetail.author_name || ""}
                    {memDetail.created_at ? ` · ${displayTime(memDetail.created_at)}` : ""}
                  </span>
                </div>
                {/* 正文滚动区：简介高亮块 + 全文 */}
                <div className="mem-detail-scroll">
                  <div className="mem-detail-callout">
                    <span className="mem-detail-label">{tr("memSummary")}</span>
                    {memDetail.summary_status === "pending" ? (
                      <span className="mem-summary-pending">
                        <span className="mini-spinner" aria-hidden="true" />
                        {tr("memSummaryPending")}
                      </span>
                    ) : (
                      <>
                        <p className="mem-detail-summary">
                          {memDetail.summary || memDetail.content}
                        </p>
                        {/* 无简介（旧数据/空返回）或上次生成失败时允许手动重试 */}
                        {(!memDetail.summary || memDetail.summary_status === "failed") && (
                          <button
                            type="button"
                            className="btn ghost tiny mem-summary-retry"
                            onClick={() => void regenerateMemSummary(memDetail.id)}
                          >
                            {tr("memSummaryRetry")}
                            {memDetail.summary_status === "failed" ? ` · ${tr("memSummaryFailed")}` : ""}
                          </button>
                        )}
                      </>
                    )}
                  </div>
                  <div className="mem-detail-full">
                    <div className="mem-detail-full-label">
                      <span className="mem-detail-label">{tr("memFull")}</span>
                    </div>
                    <MarkdownPreview text={memDetail.content} />
                  </div>
                </div>
              </div>
            </div>
          )}
          {memToast && (
            <div className="mem-toast">
              {tr("memDeleted")}
              <u onClick={() => void undoDeleteMemory()}>{tr("memUndo")}</u>
            </div>
          )}
        </main>
      )}

      {view === "agents" && (
        /* 角色工坊视图：与其他页面一样包进 .main 滚动容器（.wr-page 只负责内边距），
           否则页面内容超出视口时没有可滚动容器，整页卡死 */
        <main className="main page-pad wr-page">
          <header className="wr-page-head">
            <h2>{tr("forgeTitle")}</h2>
            <span className="wr-hint">{tr("forgeHint")}</span>
            <div className="wr-head-right">
              {!agentEditing && (
                <div className="wr-seg">
                  <button
                    type="button"
                    className={agentVisFilter === "all" ? "on" : ""}
                    onClick={() => {
                      playSfx("toggle");
                      setAgentVisFilter("all");
                    }}
                  >
                    {tr("wrFilterAll")} {agentList.length}
                  </button>
                  <button
                    type="button"
                    className={agentVisFilter === "public" ? "on" : ""}
                    onClick={() => {
                      playSfx("toggle");
                      setAgentVisFilter("public");
                    }}
                  >
                    {tr("visPublic")} {agentList.filter((a) => a.visibility === "public").length}
                  </button>
                  <button
                    type="button"
                    className={agentVisFilter === "private" ? "on" : ""}
                    onClick={() => {
                      playSfx("toggle");
                      setAgentVisFilter("private");
                    }}
                  >
                    {tr("visPrivate")} {agentList.filter((a) => a.visibility === "private").length}
                  </button>
                </div>
              )}
              {!agentEditing && (
                <button className="btn primary" type="button" onClick={startNewAgent}>
                  {tr("publishAgent")}
                </button>
              )}
            </div>
          </header>
          {agentEditing ? (
            <>
              <form className="form kb-form wr-agent-form wr-asm-form" onSubmit={saveAgent}>
                <div className="wr-page-head">
                  <button
                    type="button"
                    className="btn ghost wr-back"
                    onClick={() => {
                      playSfx("click");
                      setAgentEditing(false);
                      setAgentSelectedId(null);
                    }}
                  >
                    ← {tr("wrBackToRoster")}
                  </button>
                  <h2>{agentSelectedId ? tr("editDoc") : tr("publishAgent")}</h2>
                </div>

                {/* V-C 装配线导航：圆点实时反映各分段完成态，点击滚动到对应分段 */}
                <div className="wr-asm-pipeline">
                  {ASM_STAGES.map((s) => (
                    <button
                      key={s.key}
                      type="button"
                      className={`wr-asm-stage${asmDone[s.key] ? " done" : ""}`}
                      onClick={() => jumpToAsm(s.anchor)}
                    >
                      <span className="wr-no">{s.no} · {s.en}</span>
                      <h4>{tr(s.label)}</h4>
                      <p>{tr(s.hint)}</p>
                      <span className="wr-dot" />
                    </button>
                  ))}
                </div>

                {/* 01 身份 */}
                <section className="wr-card wr-asm-sec" id="wr-asm-identity">
                  <div className="wr-sec-head asm"><span className="wr-no">01</span><b>{tr("wrSecIdentity")}</b></div>
                  <div className="agent-avatar-row">
                    <RoleAvatar
                      avatar={agentForm.avatar || `preset:${agentForm.slot}`}
                      role={agentForm.slot}
                      title={agentForm.title || agentForm.slot}
                      size={64}
                    />
                    <div className="agent-avatar-actions">
                      <span className="panel-label">{tr("crewAvatarUpload")}</span>
                      <div className="avatar-picker">
                        {AVATAR_PRESET_OPTIONS.map((p) => (
                          <RoleAvatar
                            key={p.id}
                            avatar={p.id}
                            role={p.role}
                            size={32}
                            onClick={() => {
                              playSfx("toggle");
                              setAgentForm({ ...agentForm, avatar: p.id });
                            }}
                          />
                        ))}
                      </div>
                      <button
                        type="button"
                        className="btn ghost"
                        disabled={busy}
                        onClick={() => {
                          playSfx("click");
                          agentAvatarRef.current?.click();
                        }}
                      >
                        {tr("crewAvatarUpload")}
                      </button>
                      <input
                        ref={agentAvatarRef}
                        type="file"
                        accept="image/*,.webp,image/webp"
                        hidden
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          e.target.value = "";
                          void uploadPublishAvatar(f);
                        }}
                      />
                    </div>
                  </div>
                  <label className="wr-field">
                    <span>{tr("agentTitle")}</span>
                    <input
                      value={agentForm.title}
                      onChange={(e) => setAgentForm({ ...agentForm, title: e.target.value })}
                      required
                    />
                  </label>
                  <div className="wr-form-row">
                    <label className="wr-field">
                      <span>{tr("agentSlot")}</span>
                      <select
                        value={agentForm.slot}
                        onChange={(e) => setAgentForm({ ...agentForm, slot: e.target.value as AgentSlot })}
                      >
                        {AGENT_SLOTS.map((slot) => (
                          <option key={slot} value={slot}>
                            {tr(SLOT_I18N[slot])}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="wr-field">
                      <span>{tr("agentVisibility")}</span>
                      <select
                        value={agentForm.visibility}
                        onChange={(e) => setAgentForm({ ...agentForm, visibility: e.target.value as Visibility })}
                      >
                        <option value="public">{tr("visPublic")}</option>
                        <option value="private">{tr("visPrivate")}</option>
                      </select>
                    </label>
                  </div>
                  <label className="wr-field">
                    <span>{tr("docTags")}</span>
                    <input
                      value={agentForm.tags}
                      onChange={(e) => setAgentForm({ ...agentForm, tags: e.target.value })}
                    />
                  </label>
                </section>

                {/* 02 人设 */}
                <section className="wr-card wr-asm-sec" id="wr-asm-persona">
                  <div className="wr-sec-head asm"><span className="wr-no">02</span><b>{tr("wrAsmPersona")}</b></div>
                  <label className="wr-field">
                    <span>{tr("agentSummary")}</span>
                    <input
                      value={agentForm.summary}
                      onChange={(e) => setAgentForm({ ...agentForm, summary: e.target.value })}
                    />
                  </label>
                  {/* 人设：结构化六段式（角色/背景/技能/工作流/示例/约束）或原文 */}
                  <PersonaEditor
                    token={token}
                    lang={lang}
                    mode={agentPersonaMode}
                    spec={agentSpec}
                    raw={agentForm.persona}
                    onModeChange={setAgentPersonaMode}
                    onSpecChange={setAgentSpec}
                    onRawChange={(value) => setAgentForm({ ...agentForm, persona: value })}
                    onError={(message) => setError(message)}
                  />
                </section>

                {/* 03 装备：已选工具卡片网格 + 弹框选择 CTA，确定后回显、可再次修改 */}
                <section className="wr-card wr-asm-sec" id="wr-asm-gear">
                  <div className="wr-sec-head asm">
                    <span className="wr-no">03</span>
                    <b>{tr("wrAsmGear")}</b>
                    {agentForm.toolkit_ids.length > 0 && (
                      <span className="wr-asm-count">
                        {tr("toolsSelected")} {agentForm.toolkit_ids.length}
                      </span>
                    )}
                  </div>
                  <div className={`wr-gear-zone${agentForm.toolkit_ids.length > 0 ? " has" : ""}`}>
                    {agentForm.toolkit_ids.length > 0 && (
                      <div className="wr-gear-grid">
                        {agentForm.toolkit_ids.map((id) => {
                          const item = toolById.get(id);
                          return (
                            <div className="wr-gear-card" key={id}>
                              {item && (
                                <span className={`wr-gear-kb kind-${item.kind}`}>
                                  {item.kind === "mcp"
                                    ? tr("toolKindMcp")
                                    : item.kind === "skill"
                                      ? tr("toolKindSkill")
                                      : tr("toolKindTool")}
                                </span>
                              )}
                              <b>{item ? `${item.kind}/${item.name}` : id}</b>
                              {item?.summary && <p>{item.summary}</p>}
                              <button
                                type="button"
                                className="wr-gear-x"
                                title={tr("clearTools")}
                                onClick={() => toggleAgentFormToolkit(id)}
                              >
                                ✕
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    )}
                    <button type="button" className="wr-pick-cta" onClick={openToolPicker}>
                      <span className="wr-plus">＋</span>
                      {tr("wrGearCta")}
                      <small>{tr("wrGearCtaHint")}</small>
                    </button>
                  </div>
                </section>

                {/* 04 发布 */}
                <section className="wr-card wr-asm-sec" id="wr-asm-publish">
                  <div className="wr-sec-head asm"><span className="wr-no">04</span><b>{tr("wrAsmPublish")}</b></div>
                  <p className="wr-asm-note">{tr("wrAsmPublishNote")}</p>
                </section>

                {/* 底部固定操作条：完成时给正向提示，不做强制拦截 */}
                <div className="wr-publish-bar">
                  {asmDone.publish && (
                    <span className="wr-ready ok">✓ {tr("wrAsmReady")}</span>
                  )}
                  <span className="wr-bar-spacer" />
                  <button className="btn ghost" type="button" onClick={() => setAgentEditing(false)}>
                    {tr("cancel")}
                  </button>
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("saveDoc")}
                  </button>
                </div>
              </form>

              {/* 工具绑定弹框：宽幅双栏（左浏览勾选 / 右已选实时对照），确定才写回表单 */}
              {toolPickerOpen && (
                <div
                  className="wr-tool-overlay"
                  onClick={(e) => {
                    if (e.target === e.currentTarget) setToolPickerOpen(false);
                  }}
                >
                  <div className="wr-tool-modal" role="dialog" aria-modal="true">
                    <div className="wr-tool-head">
                      <h3>{tr("wrPickTitle")}</h3>
                      <p>{tr("wrPickHint")}</p>
                    </div>
                    <div className="wr-tool-body">
                      <div className="wr-tool-browse">
                        <div className="wr-tool-tools">
                          <input
                            className="wr-tool-search"
                            value={toolQuery}
                            placeholder={tr("wrSearchTool")}
                            onChange={(e) => setToolQuery(e.target.value)}
                          />
                          <div className="wr-seg">
                            {(["", "tool", "skill", "mcp"] as const).map((k) => (
                              <button
                                key={k || "all"}
                                type="button"
                                className={toolKindFilter === k ? "on" : ""}
                                onClick={() => setToolKindFilter(k)}
                              >
                                {k === ""
                                  ? tr("wrFilterAll")
                                  : k === "tool"
                                    ? tr("toolKindTool")
                                    : k === "skill"
                                      ? tr("toolKindSkill")
                                      : tr("toolKindMcp")}
                              </button>
                            ))}
                          </div>
                        </div>
                        <div className="wr-tool-list">
                          {pickerTools.length === 0 ? (
                            <p className="wr-tool-empty">{tr("noToolkitYet")}</p>
                          ) : (
                            pickerTools.map((item) => {
                              const on = toolPickDraft.includes(item.id);
                              return (
                                <button
                                  key={item.id}
                                  type="button"
                                  className={`wr-tool-item${on ? " picked" : ""}`}
                                  title={item.summary || item.name}
                                  onClick={() => toggleToolDraft(item.id)}
                                >
                                  <span className="wr-tool-info">
                                    <b>{item.kind}/{item.name}</b>
                                    {item.summary && <p>{item.summary}</p>}
                                  </span>
                                  <span className="wr-tick">✓</span>
                                </button>
                              );
                            })
                          )}
                        </div>
                      </div>
                      <div className="wr-tool-picked">
                        <div className="wr-pane-head">
                          {tr("wrPicked")} <b>{toolPickDraft.length}</b>
                        </div>
                        <div className="wr-picked-list">
                          {toolPickDraft.length === 0 ? (
                            <p className="wr-tool-empty">—</p>
                          ) : (
                            toolPickDraft.map((id) => {
                              const item = toolById.get(id);
                              return (
                                <div className="wr-picked-row" key={id}>
                                  <b>{item ? `${item.kind}/${item.name}` : id}</b>
                                  <button type="button" title={tr("clearTools")} onClick={() => toggleToolDraft(id)}>
                                    ✕
                                  </button>
                                </div>
                              );
                            })
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="wr-tool-foot">
                      <span className="wr-willbind">
                        {tr("wrWillBind")} <b>{toolPickDraft.length}</b> {tr("wrItemsUnit")}
                      </span>
                      <span className="wr-bar-spacer" />
                      <button type="button" className="btn ghost" onClick={() => setToolPickerOpen(false)}>
                        {tr("cancel")}
                      </button>
                      <button type="button" className="btn primary" onClick={confirmToolPick}>
                        {tr("saveDoc")}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </>
          ) : agentSelected ? (
              <>
                <div className="wr-head-right" style={{ marginBottom: 10 }}>
                  <button
                    className="btn ghost wr-back"
                    type="button"
                    onClick={() => {
                      playSfx("click");
                      setAgentSelectedId(null);
                    }}
                  >
                    ← {tr("wrBackToRoster")}
                  </button>
                </div>
                <article className="kb-article">
                <div className="topbar">
                  <div>
                    <p className="eyebrow">
                      {agentSelected.slot} · {agentSelected.slug} · v{agentSelected.version}
                    </p>
                    <h2 className="mission-title">{agentSelected.title}</h2>
                    <p className="kb-summary">{agentSelected.summary}</p>
                  </div>
                  <div className="topbar-actions">
                    <button className="btn ghost" type="button" onClick={() => startEditAgent(agentSelected)}>
                      {tr("editDoc")}
                    </button>
                    <button className="btn danger" type="button" onClick={() => deleteAgent(agentSelected.id)}>
                      {tr("delete")}
                    </button>
                  </div>
                </div>
                <pre className="preview kb-preview">{agentSelected.persona}</pre>
                {(agentSelected.toolkit_ids || []).length > 0 && (
                  <div className="loadout-block">
                    <span className="loadout-label">{tr("agentToolkits")}</span>
                    <div className="toolkit-pick">
                      {(agentSelected.toolkit_ids || []).map((id) => {
                        const item = toolkitCatalog.find((t) => t.id === id);
                        return (
                          <span key={id} className={`toolkit-chip on kind-${item?.kind || "tool"}`}>
                            {item ? `${item.kind}/${item.name}` : id.slice(0, 10)}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}
                <p className="kb-meta">
                  {tr("byAuthor")} {agentSelected.author_name} · {agentSelected.updated_at}
                </p>
              </article>
              </>
            ) : (
              <div className="wr-roster">
                {filteredAgents.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    className="wr-role-card"
                    onClick={() => {
                      playSfx("click");
                      setAgentSelectedId(a.id);
                    }}
                  >
                    <span className="wr-top">
                      <RoleAvatar avatar={a.avatar} role={a.slot} title={a.title} size={34} />
                      <span className="wr-slot">{a.slot}</span>
                      <span className={`st wr-vis ${a.visibility === "public" ? "green" : "copper"}`}>
                        {a.visibility}
                      </span>
                    </span>
                    <b>{a.title}</b>
                    <p>
                      {a.summary || a.slug} · {tr("byAuthor")} {a.author_name}
                    </p>
                    {(a.toolkit_ids || []).length > 0 && (
                      <span className="wr-tools">
                        {(a.toolkit_ids || []).map((id) => {
                          const item = toolkitCatalog.find((t) => t.id === id);
                          return (
                            <span key={id} className={`toolkit-chip on kind-${item?.kind || "tool"}`}>
                              {item?.name || id.slice(0, 10)}
                            </span>
                          );
                        })}
                      </span>
                    )}
                  </button>
                ))}
                {!agentList.length && <div className="empty-queue">{tr("emptyAgents")}</div>}
                <button type="button" className="wr-role-card wr-new" onClick={startNewAgent}>
                  ＋ {tr("publishAgent")}
                </button>
              </div>
            )}
        </main>
      )}

      {view === "toolkit" && (
        <div className="deck-body workbench toolkit-market">
          <div className="tk-page">
            <header className="tk-hero">
              <div className="tk-hero-main">
                <div className="tk-eyebrow">TOOLBOX · {tr("tkCapabilityRepo")}</div>
                <h1 className="tk-title">Sortie Deck {tr("toolkitTitle")}</h1>
                <p className="tk-lede">{tr("tkHeroLede")}</p>
              </div>
              <div className="tk-hero-side">
                <div className="tk-budget">
                  <div className="tk-budget-top">
                    <span className="tk-budget-label">{tr("tkBudget")}</span>
                    <span className="tk-budget-val">
                      <b>{toolkitAlwaysCount}</b>
                      <em>{tr("tkBudgetCap")}</em>
                    </span>
                  </div>
                  <div className="tk-budget-bar">
                    <i style={{ width: `${Math.min(100, (toolkitAlwaysCount / 12) * 100)}%` }} />
                  </div>
                </div>
                <input
                  className="tk-search"
                  type="search"
                  placeholder={tr("tkSearch")}
                  value={toolkitQuery}
                  onChange={(e) => setToolkitQuery(e.target.value)}
                />
              </div>
            </header>

            <div className="tk-seg-large">
              {(
                [
                  ["tool", "tabTools"],
                  ["skill", "tabSkills"],
                  ["mcp", "tabMcp"],
                ] as const
              ).map(([kind, label]) => (
                <button
                  key={kind}
                  type="button"
                  className={toolkitTab === kind ? "on" : ""}
                  onClick={() => {
                    setToolkitTab(kind);
                    setToolkitEditing(false);
                    setToolkitSelectedId(null);
                    setOpenMenuId(null);
                  }}
                >
                  {tr(label)}
                  <span className="n">{toolkitKindCounts[kind]}</span>
                </button>
              ))}
            </div>

          {/* ===== V4 发现带：按 tab 分工具库 / 市场推荐 / 连接引导 ===== */}
          {toolkitTab === "tool" && (
            <div className="tk-band">
              <div className="tk-band-head">
                <div>
                  <div className="tk-band-title">{tr("tkToolsTitle")}</div>
                  <p className="tk-band-desc">{tr("tkToolBandHint")}</p>
                </div>
                <button className="btn primary" type="button" onClick={openToolModal}>
                  {tr("tkAddTool")}
                </button>
              </div>
            </div>
          )}
          {toolkitTab === "skill" && (
            <div className="tk-band">
              <div className="tk-band-head">
                <div>
                  <div className="tk-band-title">{tr("tkMarketSection")}</div>
                  <p className="tk-band-desc">
                    {tr("tkMarketHint")}
                    <span className="tk-src-note">{tr("tkMarketSource")}</span>
                  </p>
                </div>
                <button className="btn primary" type="button" onClick={() => setSkillModalOpen(true)}>
                  {tr("tkAddSkill")}
                </button>
              </div>
              <div className="tk-market-row">
                {marketSkills.map((m) => (
                  <div key={m.key} className="tk-market-card">
                    <div className="tk-mkt-top">
                      <span className={`tk-mkg${m.src === "GitHub" ? "" : " g"}`}>{m.name.slice(0, 2)}</span>
                      <strong>{m.name}</strong>
                      <span className={`tk-tag${m.src === "GitHub" ? "" : " g"}`}>{m.src}</span>
                    </div>
                    <p className="tk-mkt-desc">{m.summary}</p>
                    <div className="tk-mkt-foot">
                      {m.installed ? (
                        <span className="tk-status on">
                          <i />
                          {tr("tkInstalled")}
                        </span>
                      ) : (
                        <button
                          className="btn primary"
                          type="button"
                          disabled={busy}
                          onClick={() => installMarketSkill(m.key)}
                        >
                          {tr("tkInstall")}
                        </button>
                      )}
                      {m.category && <span className="tk-tag line">{toolkitCategoryLabel(m.category)}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {toolkitTab === "mcp" && (
            <div className="tk-band">
              <div className="tk-band-head">
                <div>
                  <div className="tk-band-title">{tr("tkMcpBandTitle")}</div>
                  <p className="tk-band-desc">{tr("tkMcpBandHint")}</p>
                </div>
                <div className="tk-band-actions">
                  <button className="btn primary" type="button" onClick={openMcpModal}>
                    {tr("tkAddMcp")}
                  </button>
                </div>
              </div>
              <div className="tk-guide-row">
                <div className="tk-guide">
                  <b>{tr("tkGuideSseTitle")}</b>
                  <span>{tr("tkGuideSseDesc")}</span>
                </div>
                <div className="tk-guide">
                  <b>
                    {tr("tkGuideHttpTitle")} <em className="tk-rec">推荐</em>
                  </b>
                  <span>{tr("tkGuideHttpDesc")}</span>
                </div>
                <div className="tk-guide">
                  <b>{tr("tkGuideStdioTitle")}</b>
                  <span>{tr("tkGuideStdioDesc")}</span>
                </div>
              </div>
            </div>
          )}


          <main className="main page-pad toolkit-main">
            {toolkitEditing ? (
              <form className="form kb-form" onSubmit={saveToolkit}>
                <button
                  type="button"
                  className="btn ghost toolkit-back"
                  onClick={() => setToolkitEditing(false)}
                >
                  ← {tr("tkBackToCatalog")}
                </button>
                <h2 className="page-title">
                  {toolkitSelectedId ? tr("editDoc") : tr("newToolkit")} ·{" "}
                  {tr(toolkitTab === "tool" ? "tabTools" : toolkitTab === "mcp" ? "tabMcp" : "tabSkills")}
                </h2>
                <label>
                  <span>{tr("tkName")}</span>
                  <input
                    value={toolkitForm.name}
                    onChange={(e) => setToolkitForm({ ...toolkitForm, name: e.target.value })}
                    required
                  />
                </label>
                <label>
                  <span>{tr("tkSummary")}</span>
                  <input
                    value={toolkitForm.summary}
                    onChange={(e) => setToolkitForm({ ...toolkitForm, summary: e.target.value })}
                  />
                </label>
                <label>
                  <span>{tr("tkCategory")}</span>
                  <select
                    value={toolkitForm.category}
                    onChange={(e) => setToolkitForm({ ...toolkitForm, category: e.target.value })}
                  >
                    {TOOLKIT_CATEGORIES.map((cat) => (
                      <option key={cat} value={cat}>
                        {toolkitCategoryLabel(cat)}
                      </option>
                    ))}
                  </select>
                </label>
                {toolkitTab === "tool" && (
                  <>
                    <label>
                      <span>{tr("tkRuntime")}</span>
                      <select
                        value={toolkitForm.runtime}
                        onChange={(e) =>
                          setToolkitForm({ ...toolkitForm, runtime: e.target.value as ToolRuntime })
                        }
                      >
                        <option value="cli">cli</option>
                        <option value="http">http</option>
                        <option value="function">function</option>
                        <option value="plugin">plugin</option>
                      </select>
                    </label>
                    <label>
                      <span>{tr("tkEndpoint")}</span>
                      <input
                        value={toolkitForm.endpoint}
                        onChange={(e) => setToolkitForm({ ...toolkitForm, endpoint: e.target.value })}
                        required
                      />
                    </label>
                  </>
                )}
                {toolkitTab === "mcp" && (
                  <>
                    <label>
                      <span>{tr("tkTransport")}</span>
                      <select
                        value={toolkitForm.transport}
                        onChange={(e) =>
                          setToolkitForm({ ...toolkitForm, transport: e.target.value as McpTransport })
                        }
                      >
                        <option value="stdio">stdio</option>
                        <option value="sse">sse</option>
                        <option value="http">http</option>
                      </select>
                    </label>
                    <label>
                      <span>{tr("tkCommand")}</span>
                      <input
                        value={toolkitForm.command}
                        onChange={(e) => setToolkitForm({ ...toolkitForm, command: e.target.value })}
                      />
                    </label>
                    <label>
                      <span>{tr("tkUrl")}</span>
                      <input
                        value={toolkitForm.url}
                        onChange={(e) => setToolkitForm({ ...toolkitForm, url: e.target.value })}
                      />
                    </label>
                  </>
                )}
                {toolkitTab === "skill" && (
                  <>
                    <label>
                      <span>{tr("tkBody")}</span>
                      <textarea
                        className="kb-body"
                        value={toolkitForm.body}
                        onChange={(e) => setToolkitForm({ ...toolkitForm, body: e.target.value })}
                        required
                      />
                    </label>
                    <div className="script-block">
                      <div className="panel-label">{tr("tkScripts")}</div>
                      {toolkitForm.scripts.map((script, idx) => (
                        <div key={`${script.path}-${idx}`} className="script-card">
                          <label>
                            <span>{tr("tkScriptPath")}</span>
                            <input
                              value={script.path}
                              onChange={(e) => updateToolkitScript(idx, { path: e.target.value })}
                            />
                          </label>
                          <label>
                            <span>{tr("tkScriptContent")}</span>
                            <textarea
                              className="kb-body"
                              value={script.content}
                              onChange={(e) => updateToolkitScript(idx, { content: e.target.value })}
                            />
                          </label>
                          <button type="button" className="btn danger" onClick={() => removeToolkitScript(idx)}>
                            {tr("removeScript")}
                          </button>
                        </div>
                      ))}
                      <button type="button" className="btn ghost" onClick={addToolkitScript}>
                        {tr("addScript")}
                      </button>
                    </div>
                  </>
                )}
                <label>
                  <span>{tr("docTags")}</span>
                  <input
                    value={toolkitForm.tags}
                    onChange={(e) => setToolkitForm({ ...toolkitForm, tags: e.target.value })}
                  />
                </label>
                <label className="theme-toggle">
                  <input
                    type="checkbox"
                    checked={toolkitForm.featured}
                    onChange={(e) => setToolkitForm({ ...toolkitForm, featured: e.target.checked })}
                  />
                  {tr("tkFeatured")}
                </label>
                <label>
                  <span>{tr("tkLoadPolicy")}</span>
                  <select
                    value={toolkitForm.load}
                    onChange={(e) =>
                      setToolkitForm({ ...toolkitForm, load: e.target.value as "always" | "defer" })
                    }
                  >
                    <option value="defer">{tr("tkLoadDeferHint")}</option>
                    <option value="always">{tr("tkLoadAlwaysHint")}</option>
                  </select>
                </label>
                <label className="theme-toggle">
                  <input
                    type="checkbox"
                    checked={toolkitForm.enabled}
                    onChange={(e) => setToolkitForm({ ...toolkitForm, enabled: e.target.checked })}
                  />
                  {tr("tkEnabled")}
                </label>
                <div className="kb-actions">
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("saveDoc")}
                  </button>
                  <button className="btn ghost" type="button" onClick={() => setToolkitEditing(false)}>
                    {tr("cancel")}
                  </button>
                </div>
              </form>
            ) : toolkitSelected ? (
              <article className="kb-article">
                <button
                  type="button"
                  className="btn ghost toolkit-back"
                  onClick={() => setToolkitSelectedId(null)}
                >
                  ← {tr("tkBackToCatalog")}
                </button>
                <div className="topbar">
                  <div>
                    <p className="eyebrow">
                      {tr(
                        toolkitSelected.kind === "tool"
                          ? "tabTools"
                          : toolkitSelected.kind === "mcp"
                            ? "tabMcp"
                            : "tabSkills"
                      )}
                      {toolkitSelected.category ? ` · ${toolkitCategoryLabel(toolkitSelected.category)}` : ""}
                      {" · "}
                      {toolkitSelected.enabled ? tr("tkEnabled") : tr("tkDisabled")}
                      {toolkitSelected.featured ? ` · ${tr("tkFeatured")}` : ""}
                    </p>
                    <h2 className="mission-title">{toolkitSelected.name}</h2>
                    <p className="kb-summary">{toolkitSelected.summary}</p>
                  </div>
                  <div className="topbar-actions">
                    {toolkitSelected.kind === "mcp" && (
                      <button
                        className="btn ghost"
                        type="button"
                        disabled={mcpItemTest?.id === toolkitSelected.id && mcpItemTest.state === "testing"}
                        onClick={() => testMcpItem(toolkitSelected)}
                      >
                        {mcpItemTest?.id === toolkitSelected.id && mcpItemTest.state === "testing"
                          ? tr("tkTesting")
                          : tr("tkTestConnection")}
                      </button>
                    )}
                    <button className="btn ghost" type="button" onClick={() => toggleToolkitEnabled(toolkitSelected)}>
                      {toolkitSelected.enabled ? tr("tkDisabled") : tr("tkEnabled")}
                    </button>
                    <button className="btn ghost" type="button" onClick={() => startEditToolkit(toolkitSelected)}>
                      {tr("editDoc")}
                    </button>
                    <button className="btn danger" type="button" onClick={() => deleteToolkit(toolkitSelected.id)}>
                      {tr("delete")}
                    </button>
                  </div>
                </div>
                <pre className="preview kb-preview">
                  {toolkitSelected.kind === "tool" &&
                    `${toolkitSelected.runtime || ""}\n${toolkitSelected.endpoint || ""}`}
                  {toolkitSelected.kind === "mcp" &&
                    [
                      toolkitSelected.transport || "",
                      toolkitSelected.command || "",
                      (toolkitSelected.args || []).join(" "),
                      toolkitSelected.url || "",
                    ]
                      .filter(Boolean)
                      .join("\n")}
                  {toolkitSelected.kind === "skill" && (toolkitSelected.body || "")}
                </pre>
                {toolkitSelected.kind === "skill" && (toolkitSelected.scripts || []).length > 0 && (
                  <div className="script-block">
                    <div className="panel-label">{tr("tkScripts")}</div>
                    {(toolkitSelected.scripts || []).map((s) => (
                      <details key={s.path} className="script-card">
                        <summary>
                          {s.path}
                          {s.language ? ` · ${s.language}` : ""}
                        </summary>
                        <pre className="preview">{s.content}</pre>
                      </details>
                    ))}
                  </div>
                )}
                <p className="kb-meta">
                  {(toolkitSelected.tags || []).join(" · ")} · {toolkitSelected.author_name}
                  {toolkitSelected.source ? ` · ${toolkitSelected.source}` : ""}
                  {" · "}
                  {toolkitSelected.load === "always" ? tr("tkAlways") : tr("tkDefer")}
                  {" · "}
                  {toolkitMounts[toolkitSelected.id] || 0} {tr("tkMountsSuffix")}
                </p>
                {mcpItemTest?.id === toolkitSelected.id && mcpItemTest.msg && (
                  <p className={`kb-meta tk-test-msg ${mcpItemTest.state}`}>{mcpItemTest.msg}</p>
                )}
              </article>
            ) : (
              <>
                <div className="tk-chips">
                  <button
                    type="button"
                    className={`tk-chip ${!toolkitCategory ? "on" : ""}`}
                    onClick={() => setToolkitCategory("")}
                  >
                    {tr("tkAllCategories")}
                    <span className="c">{toolkitKindCounts[toolkitTab]}</span>
                  </button>
                  {TOOLKIT_CATEGORIES.map((cat) => (
                    <button
                      key={cat}
                      type="button"
                      className={`tk-chip ${toolkitCategory === cat ? "on" : ""}`}
                      onClick={() => setToolkitCategory(cat)}
                    >
                      {toolkitCategoryLabel(cat)}
                      <span className="c">{toolkitCategoryCounts[cat] || 0}</span>
                    </button>
                  ))}
                </div>

                <section className="tk-cards">
                  {toolkitGridItems.map((item) => (
                    <div
                      key={item.id}
                      role="button"
                      tabIndex={0}
                      className={`tk-card${item.enabled === false ? " off" : ""}`}
                      onClick={() => setToolkitSelectedId(item.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") setToolkitSelectedId(item.id);
                      }}
                    >
                      <div className="tk-card-head">
                        <span className={`tk-monogram ${toolkitMonogramTone(item)}`}>
                          {item.name.slice(0, 2)}
                        </span>
                        <span className="tk-card-name">{item.name}</span>
                        <span
                          className="tk-more"
                          title={tr("tkMenuActions")}
                          onClick={(e) => {
                            e.stopPropagation();
                            setOpenMenuId(openMenuId === item.id ? null : item.id);
                          }}
                        >
                          …
                        </span>
                        {openMenuId === item.id && (
                          <div className="tk-menu" onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              onClick={() => {
                                setOpenMenuId(null);
                                toggleToolkitEnabled(item);
                              }}
                            >
                              {item.enabled ? tr("tkDisabled") : tr("tkEnabled")}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setOpenMenuId(null);
                                startEditToolkit(item);
                              }}
                            >
                              {tr("editDoc")}
                            </button>
                            <button
                              type="button"
                              className="danger"
                              onClick={() => {
                                setOpenMenuId(null);
                                deleteToolkit(item.id);
                              }}
                            >
                              {tr("delete")}
                            </button>
                          </div>
                        )}
                      </div>
                      <p className="tk-card-desc">{item.summary}</p>
                      {item.kind === "mcp" && (item.command || item.url) && (
                        <div className="tk-card-endpoint">
                          {item.command || item.url}
                        </div>
                      )}
                      <div className="tk-card-meta">
                        <span className={`tk-tag${item.load === "always" ? " c" : ""}`}>
                          {item.load === "always" ? tr("tkAlways") : tr("tkDefer")}
                        </span>
                        {item.kind === "mcp" && item.transport && (
                          <span className="tk-tag line">{item.transport}</span>
                        )}
                        {item.kind === "tool" && item.runtime && (
                          <span className="tk-tag line">{item.runtime}</span>
                        )}
                        {item.category && (
                          <span className="tk-tag line">{toolkitCategoryLabel(item.category)}</span>
                        )}
                        <span className="tk-tag line">
                          {toolkitMounts[item.id] || 0} {tr("tkMountsSuffix")}
                        </span>
                        {(item.tags || [])
                          .filter((t) => t === "github" || t === "market" || t === "handwritten")
                          .map((t) => (
                            <span key={t} className="tk-tag g">
                              {t === "github" ? "GitHub" : t === "market" ? tr("tkMarketSection") : tr("tkAddSkill")}
                            </span>
                          ))}
                      </div>
                      <div className="tk-card-foot">
                        <span className={`tk-status ${item.enabled ? "on" : ""}`}>
                          <i />
                          {item.enabled ? tr("tkEnabled") : tr("tkDisabled")}
                        </span>
                        {item.source && <span className="tk-tag">{item.source}</span>}
                      </div>
                    </div>
                  ))}
                  {!toolkitGridItems.length && <div className="tk-empty">{tr("emptyToolkit")}</div>}
                </section>
              </>
            )}
          </main>

          {/* ===== V4 弹窗：添加技能（外网 / 本地上传 / 手写 MD + skill-install 对照） ===== */}
          {skillModalOpen && (
            <div className="tk-mask" onClick={closeSkillModal}>
              <div className="tk-modal" onClick={(e) => e.stopPropagation()}>
                <div className="tk-modal-head">
                  <h3>{tr("tkAddSkill")}</h3>
                  <button className="tk-x" type="button" onClick={closeSkillModal}>×</button>
                </div>
                <div className="tk-modal-body">
                  <div className="tk-seg-inline" style={{ marginBottom: 16 }}>
                    <button
                      type="button"
                      className={skillMode === "remote" ? "on" : ""}
                      onClick={() => setSkillMode("remote")}
                    >
                      {tr("tkSkillModeRemote")}
                    </button>
                    <button
                      type="button"
                      className={skillMode === "local" ? "on" : ""}
                      onClick={() => setSkillMode("local")}
                    >
                      {tr("tkSkillModeLocal")}
                    </button>
                  </div>
                </div>
                {skillMode === "remote" && (
                  <div className="tk-modal-body">
                    <div className="tk-field">
                      <label htmlFor="tk-skill-url">{tr("tkSkillUrl")}</label>
                      <input
                        id="tk-skill-url"
                        className="tk-input mono"
                        value={skillUrl}
                        onChange={(e) => setSkillUrl(e.target.value)}
                        placeholder="https://github.com/org/skill-repo"
                      />
                      <div className="tk-hint">{tr("tkSkillRemoteHint")}</div>
                    </div>
                  </div>
                )}
                {skillMode === "local" && (
                  <div className="tk-modal-body">
                    <div className="tk-seg-inline" style={{ marginBottom: 16 }}>
                      <button
                        type="button"
                        className={localMode === "upload" ? "on" : ""}
                        onClick={() => setLocalMode("upload")}
                      >
                        {tr("tkLocalUpload")}
                      </button>
                      <button
                        type="button"
                        className={localMode === "write" ? "on" : ""}
                        onClick={() => setLocalMode("write")}
                      >
                        {tr("tkLocalWrite")}
                      </button>
                    </div>
                    {localMode === "upload" && (
                      <>
                        <div className="tk-field">
                          <label htmlFor="tk-skill-file">{tr("tkPickFile")}</label>
                          <input
                            id="tk-skill-file"
                            className="tk-input"
                            type="file"
                            accept=".zip,.md"
                            onChange={(e) => {
                              const f = e.target.files?.[0] || null;
                              setSkillUpload(f ? { file: f, name: f.name.replace(/\.(zip|md)$/i, "") } : null);
                            }}
                          />
                          <div className="tk-hint">{tr("tkSkillFileHint")}</div>
                        </div>
                        <div className="tk-field">
                          <label htmlFor="tk-skill-upname">{tr("tkSkillUploadName")}</label>
                          <input
                            id="tk-skill-upname"
                            className="tk-input"
                            value={skillUpload?.name || ""}
                            onChange={(e) =>
                              setSkillUpload((prev) => (prev ? { ...prev, name: e.target.value } : prev))
                            }
                          />
                        </div>
                      </>
                    )}
                    {localMode === "write" && optimizePhase !== "review" && (
                      <>
                        <div className="tk-field">
                          <label htmlFor="tk-write-name">{tr("tkName")}</label>
                          <input
                            id="tk-write-name"
                            className="tk-input mono"
                            value={writeForm.name}
                            onChange={(e) => setWriteForm({ ...writeForm, name: e.target.value })}
                          />
                        </div>
                        <div className="tk-field">
                          <label htmlFor="tk-write-cat">{tr("tkCategory")}</label>
                          <select
                            id="tk-write-cat"
                            className="tk-input"
                            value={writeForm.category}
                            onChange={(e) => setWriteForm({ ...writeForm, category: e.target.value })}
                          >
                            {TOOLKIT_CATEGORIES.map((cat) => (
                              <option key={cat} value={cat}>
                                {toolkitCategoryLabel(cat)}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="tk-field">
                          <label htmlFor="tk-write-body">{tr("tkBody")}</label>
                          <textarea
                            id="tk-write-body"
                            className="tk-input mono"
                            rows={9}
                            value={writeForm.body}
                            onChange={(e) => setWriteForm({ ...writeForm, body: e.target.value })}
                          />
                        </div>
                        {optimizePhase === "running" && (
                          <p className="tk-optimizing">{tr("tkOptimizing")}{tr("tkOptimizeSteps")}</p>
                        )}
                      </>
                    )}
                    {localMode === "write" && optimizePhase === "review" && skillDraft && (
                      <>
                        <p className="kb-hint tk-review-hint">{tr("tkReviewHint")}</p>
                        <div className="tk-review-grid">
                          <div className="tk-review-pane">
                            <h4>{tr("tkOriginal")}</h4>
                            <pre>{skillDraft.original}</pre>
                            <button className="btn ghost" type="button" onClick={() => confirmSkillDraft("original")}>
                              {tr("tkUseVersion")}
                            </button>
                          </div>
                          <div className="tk-review-pane opt">
                            <h4>
                              {tr("tkOptimized")} <em className="tk-rec-badge">{tr("tkSkillInstallRec")}</em>
                            </h4>
                            <pre>{skillDraft.optimized}</pre>
                            <button className="btn primary" type="button" onClick={() => confirmSkillDraft("optimized")}>
                              {tr("tkUseVersion")}
                            </button>
                          </div>
                        </div>
                      </>
                    )}
                  </div>
                )}
                {skillModalMsg && <p className="kb-meta tk-modal-msg">{skillModalMsg}</p>}
                <div className="tk-modal-foot">
                  <button className="btn ghost" type="button" onClick={closeSkillModal}>
                    {tr("cancel")}
                  </button>
                  {skillMode === "remote" && (
                    <button className="btn primary" type="button" disabled={busy} onClick={runInstallFromGithub}>
                      {tr("tkInstall")}
                    </button>
                  )}
                  {skillMode === "local" && localMode === "upload" && (
                    <button className="btn primary" type="button" disabled={busy} onClick={runSkillUpload}>
                      {tr("importRun")}
                    </button>
                  )}
                  {skillMode === "local" && localMode === "write" && optimizePhase === "idle" && (
                    <button className="btn primary" type="button" disabled={busy} onClick={runOptimizeSkill}>
                      {tr("tkCreateSkill")}
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* ===== V4 抽屉：添加工具（右侧抽屉 + runtime 分段卡） ===== */}
          {toolModalOpen && (
            <div className="tk-mask" onClick={() => setToolModalOpen(false)}>
              <form
                className="tk-drawer open"
                onClick={(e) => e.stopPropagation()}
                onSubmit={submitToolModal}
              >
                <div className="tk-drawer-head">
                  <h3>{tr("tkAddTool")}</h3>
                  <p>{tr("tkToolDrawerHint")}</p>
                  <button
                    className="tk-drawer-x"
                    type="button"
                    onClick={() => setToolModalOpen(false)}
                  >
                    ×
                  </button>
                </div>
                <div className="tk-drawer-body">
                  <div className="tk-field">
                    <label htmlFor="tk-tool-name">{tr("tkName")}</label>
                    <input
                      id="tk-tool-name"
                      className="tk-input mono"
                      value={toolForm.name}
                      onChange={(e) => setToolForm({ ...toolForm, name: e.target.value })}
                      required
                    />
                  </div>
                  <div className="tk-field">
                    <label htmlFor="tk-tool-summary">{tr("tkSummary")}</label>
                    <input
                      id="tk-tool-summary"
                      className="tk-input"
                      value={toolForm.summary}
                      onChange={(e) => setToolForm({ ...toolForm, summary: e.target.value })}
                    />
                  </div>
                  <div className="tk-field">
                    <label htmlFor="tk-tool-cat">{tr("tkCategory")}</label>
                    <select
                      id="tk-tool-cat"
                      className="tk-input"
                      value={toolForm.category}
                      onChange={(e) => setToolForm({ ...toolForm, category: e.target.value })}
                    >
                      {TOOLKIT_CATEGORIES.map((cat) => (
                        <option key={cat} value={cat}>
                          {toolkitCategoryLabel(cat)}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="tk-field">
                    <label>{tr("tkRuntime")}</label>
                    <div className="tk-select-cards">
                      {(
                        [
                          ["cli", tr("tkRuntimeCliDesc")],
                          ["http", tr("tkRuntimeHttpDesc")],
                          ["function", tr("tkRuntimeFunctionDesc")],
                          ["plugin", tr("tkRuntimePluginDesc")],
                        ] as const
                      ).map(([value, desc]) => (
                        <button
                          key={value}
                          type="button"
                          className={toolForm.runtime === value ? "on" : ""}
                          onClick={() => setToolForm({ ...toolForm, runtime: value })}
                        >
                          <span className="tt">{value}</span>
                          <span className="td">{desc}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="tk-field">
                    <label htmlFor="tk-tool-endpoint">{tr("tkEndpoint")}</label>
                    <input
                      id="tk-tool-endpoint"
                      className="tk-input mono"
                      value={toolForm.endpoint}
                      onChange={(e) => setToolForm({ ...toolForm, endpoint: e.target.value })}
                      placeholder="https://api.example.com/run"
                      required
                    />
                    <div className="tk-hint">{tr("tkToolEndpointHint")}</div>
                  </div>
                  <div className="tk-field">
                    <label htmlFor="tk-tool-load">{tr("tkLoadPolicy")}</label>
                    <select
                      id="tk-tool-load"
                      className="tk-input"
                      value={toolForm.load}
                      onChange={(e) => setToolForm({ ...toolForm, load: e.target.value as "always" | "defer" })}
                    >
                      <option value="defer">{tr("tkLoadDeferHint")}</option>
                      <option value="always">{tr("tkLoadAlwaysHint")}</option>
                    </select>
                  </div>
                </div>
                <div className="tk-drawer-foot">
                  <span className="tk-foot-spacer" />
                  <button className="btn ghost" type="button" onClick={() => setToolModalOpen(false)}>
                    {tr("cancel")}
                  </button>
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("tkAddTool")}
                  </button>
                </div>
              </form>
            </div>
          )}
          {/* ===== V4 抽屉：添加连接器（stdio 仅 zip 导入；SSE/HTTP 走 URL + 鉴权） ===== */}
          {mcpModalOpen && (
            <div className="tk-mask" onClick={() => setMcpModalOpen(false)}>
              <form
                className="tk-drawer open"
                onClick={(e) => e.stopPropagation()}
                onSubmit={submitMcpModal}
              >
                <div className="tk-drawer-head">
                  <h3>{tr("tkAddMcp")}</h3>
                  <p>{tr("tkMcpDrawerHint")}</p>
                  <button
                    className="tk-drawer-x"
                    type="button"
                    onClick={() => setMcpModalOpen(false)}
                  >
                    ×
                  </button>
                </div>
                <div className="tk-drawer-body">
                  {mcpForm.transport !== "stdio" && (
                    <div className="tk-field">
                      <label htmlFor="tk-mcp-name">{tr("tkName")}</label>
                      <input
                        id="tk-mcp-name"
                        className="tk-input mono"
                        value={mcpForm.name}
                        onChange={(e) => setMcpForm({ ...mcpForm, name: e.target.value })}
                        required
                      />
                    </div>
                  )}
                  <div className="tk-field">
                    <label>{tr("tkTransport")}</label>
                    <div className="tk-select-cards c3">
                      {(
                        [
                          ["stdio", tr("tkTransportStdioDesc"), ""],
                          ["sse", tr("tkTransportSseDesc"), ""],
                          ["http", tr("tkTransportHttpDesc"), tr("tkRecommended")],
                        ] as const
                      ).map(([value, desc, badge]) => (
                        <button
                          key={value}
                          type="button"
                          className={mcpForm.transport === value ? "on" : ""}
                          onClick={() => {
                            setMcpForm({ ...mcpForm, transport: value });
                            setMcpTest({ state: "idle", msg: "" });
                            setMcpPkg({ file: null, phase: "idle" });
                          }}
                        >
                          <span className="tt">
                            {value === "http" ? "Streamable HTTP" : value}
                            {badge ? <em className="tk-rec">{badge}</em> : null}
                          </span>
                          <span className="td">{desc}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                  {mcpForm.transport === "stdio" && (
                    <>
                      <div className="tk-field">
                        <label htmlFor="tk-mcp-zip">{tr("tkPickZip")}</label>
                        <input
                          id="tk-mcp-zip"
                          className="tk-input"
                          type="file"
                          accept=".zip"
                          onChange={(e) => pickMcpPkg(e.target.files?.[0])}
                        />
                        <div className="tk-hint">{tr("tkMcpStdioZipOnly")}</div>
                      </div>
                      {mcpPkg.phase === "ready" && (
                        <>
                          <div className="tk-field">
                            <label htmlFor="tk-mcp-cmd">{tr("tkCommand")}</label>
                            <input
                              id="tk-mcp-cmd"
                              className="tk-input mono"
                              value={mcpForm.command}
                              onChange={(e) => setMcpForm({ ...mcpForm, command: e.target.value })}
                            />
                            <div className="tk-hint tk-pkg-ready">{tr("tkPkgReady")}</div>
                          </div>
                        </>
                      )}
                    </>
                  )}
                  {mcpForm.transport !== "stdio" && (
                    <>
                      <div className="tk-field">
                        <label htmlFor="tk-mcp-url">{tr("tkUrl")}</label>
                        <input
                          id="tk-mcp-url"
                          className="tk-input mono"
                          value={mcpForm.url}
                          onChange={(e) => setMcpForm({ ...mcpForm, url: e.target.value })}
                          placeholder="https://…"
                          required
                        />
                      </div>
                      <div className="tk-field">
                        <label>{tr("tkAuthMode")}</label>
                        <div className="tk-seg-inline">
                          <button
                            type="button"
                            className={mcpForm.auth === "bearer" ? "on" : ""}
                            onClick={() => setMcpForm({ ...mcpForm, auth: "bearer" })}
                          >
                            {tr("tkAuthBearer")}
                          </button>
                          <button
                            type="button"
                            className={mcpForm.auth === "oauth" ? "on" : ""}
                            onClick={() => setMcpForm({ ...mcpForm, auth: "oauth" })}
                          >
                            {tr("tkAuthOAuth")}
                          </button>
                        </div>
                      </div>
                      {mcpForm.auth === "bearer" && (
                        <div className="tk-field">
                          <label htmlFor="tk-mcp-token">{tr("tkAuthToken")}</label>
                          <input
                            id="tk-mcp-token"
                            className="tk-input mono"
                            type="password"
                            value={mcpForm.token}
                            onChange={(e) => setMcpForm({ ...mcpForm, token: e.target.value })}
                          />
                        </div>
                      )}
                    </>
                  )}
                  {mcpTest.msg && (
                    <div className={`tk-test-line ${mcpTest.state}`}>
                      <i />
                      {mcpTest.msg}
                    </div>
                  )}
                </div>
                <div className="tk-drawer-foot">
                  <button
                    className="btn ghost"
                    type="button"
                    disabled={mcpTest.state === "testing"}
                    onClick={runMcpTest}
                  >
                    {mcpTest.state === "testing" ? tr("tkTesting") : tr("tkTestConnection")}
                  </button>
                  <span className="tk-foot-spacer" />
                  <button className="btn ghost" type="button" onClick={() => setMcpModalOpen(false)}>
                    {tr("cancel")}
                  </button>
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("tkAddMcp")}
                  </button>
                </div>
              </form>
            </div>
          )}
          </div>
        </div>
      )}

      {view === "forge" && (
        <main className="main page-pad">
          <ForgeReview token={token} lang={lang} />
        </main>
      )}

      {view === "subrooms" && (
        <main className="main page-pad">
          <SubRoomPanel token={token} lang={lang} initiativeId={selectedId} />
        </main>
      )}

      {view === "decisions" && (
        <main className="main page-pad">
          <DecisionCenter
            token={token}
            lang={lang}
            isAdmin={user.role === "admin"}
            initiativeTitles={Object.fromEntries(items.map((i) => [i.id, i.title]))}
          />
        </main>
      )}

      {view === "admin" && user.role === "admin" && (
        /* 用户管理页 · V3 Stripe 思维版式：读数条 + 待审核卡 + 全量用户卡（含搜索）+ 添加卡 */
        <main className="main page-pad">
          <div className="uadmin">
            <div className="uadmin-head">
              <div className="uadmin-eyebrow">ADMIN ONLY · {tr("admin")}</div>
              <h1>{tr("users")}</h1>
              <p className="uadmin-lede">{tr("usersLede")}</p>
            </div>

            <div className="uadmin-readout">
              <div className="seg">
                <div className="v">{users.length}</div>
                <div className="k">{tr("countUsers")}</div>
              </div>
              <div className={`seg${adminPending.length ? " attn" : ""}`}>
                <div className="v">{adminPending.length}</div>
                <div className="k">{tr("countPending")}</div>
              </div>
              <div className="seg">
                <div className="v">{users.filter((u) => u.role === "admin").length}</div>
                <div className="k">{tr("countAdmins")}</div>
              </div>
              <div className="seg">
                <div className="v">{users.filter((u) => u.role === "member").length}</div>
                <div className="k">{tr("countMembers")}</div>
              </div>
            </div>

            {adminPending.length > 0 && (
              <div className="uadmin-card">
                <div className="hd">
                  <h2>{tr("userStatusPending")}</h2>
                  <span className="hint">{tr("usersPendingHint")}</span>
                </div>
                {adminPending.map((u) => (
                  <div className="urow" key={u.id}>
                    <span className="av" style={{ background: avatarColor(u.username) }}>
                      {u.display_name.slice(0, 1)}
                    </span>
                    <div className="id">
                      <b>{u.display_name}</b>
                      <span className="un">@{u.username}</span>
                    </div>
                    <span className="sp" />
                    <button
                      type="button"
                      className="primary"
                      disabled={busy}
                      onClick={() => setUserStatus(u.id, "active")}
                    >
                      {tr("approveUser")}
                    </button>
                    <button
                      type="button"
                      className="linkbtn"
                      disabled={busy}
                      onClick={() => setUserStatus(u.id, "rejected")}
                    >
                      {tr("rejectUser")}
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="uadmin-card">
              <div className="hd">
                <h2>{tr("users")}</h2>
                <span className="hint">{tr("usersAllHint")}</span>
                <input
                  className="uadmin-search"
                  placeholder={tr("usersSearch")}
                  value={userSearch}
                  onChange={(e) => setUserSearch(e.target.value)}
                />
              </div>
              {adminFiltered.map((u) => (
                <div className="urow" key={u.id}>
                  <span className="av" style={{ background: avatarColor(u.username) }}>
                    {u.display_name.slice(0, 1)}
                  </span>
                  <div className="id">
                    <b>{u.display_name}</b>
                    <span className="un">@{u.username}</span>
                  </div>
                  <span className="sp" />
                  {/* 角色下拉：变更即提交；系统至少保留一个 admin */}
                  <select
                    className="sel"
                    value={u.role}
                    disabled={busy}
                    onChange={(e) => {
                      void setUserRole(u.id, e.target.value as UserRole);
                    }}
                  >
                    <option value="member">{tr("memberRole")}</option>
                    <option value="admin">{tr("adminRole")}</option>
                  </select>
                  <span className={`pill ${u.status === "pending" ? "pend" : u.status === "rejected" ? "no" : "ok"}`}>
                    {u.status === "pending"
                      ? tr("userStatusPending")
                      : u.status === "rejected"
                        ? tr("userStatusRejected")
                        : tr("userStatusActive")}
                  </span>
                  <button type="button" className="linkbtn" onClick={() => deleteUser(u.id)}>
                    {tr("delete")}
                  </button>
                </div>
              ))}
              {!adminFiltered.length && <div className="uadmin-empty">{tr("usersEmpty")}</div>}
            </div>

            <div className="uadmin-card">
              <div className="hd">
                <h2>{tr("addUser")}</h2>
                <span className="hint">{tr("addUserHint")}</span>
              </div>
              <form className="uadmin-form" onSubmit={createUser}>
                <div className="f">
                  <label>{tr("username")}</label>
                  <input
                    placeholder="chenmo"
                    value={newUser.username}
                    onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
                    required
                  />
                  <div className="help">{tr("helpUsername")}</div>
                </div>
                <div className="f">
                  <label>{tr("displayName")}</label>
                  <input
                    placeholder={tr("displayName")}
                    value={newUser.display_name}
                    onChange={(e) => setNewUser({ ...newUser, display_name: e.target.value })}
                    required
                  />
                </div>
                <div className="f">
                  <label>{tr("password")}</label>
                  <input
                    type="password"
                    placeholder="••••••••"
                    value={newUser.password}
                    onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
                    required
                  />
                  <div className="help">{tr("helpPassword")}</div>
                </div>
                <div className="f">
                  <label>{tr("role")}</label>
                  <select value={newUser.role} onChange={(e) => setNewUser({ ...newUser, role: e.target.value as UserRole })}>
                    <option value="member">{tr("memberRole")}</option>
                    <option value="admin">{tr("adminRole")}</option>
                  </select>
                  <div className="help">{tr("helpRole")}</div>
                </div>
                <button className="primary" type="submit" disabled={busy}>
                  {tr("addUser")}
                </button>
              </form>
            </div>
          </div>
        </main>
      )}
    </div>
  );
}
