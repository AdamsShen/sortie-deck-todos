import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "./api";
import { OperatorChip } from "./components/OperatorChip";
import { PipelineEditor } from "./components/PipelineEditor";
import { ChatComposer, type MentionOption } from "./components/ChatComposer";
import { MarkdownPreview, looksLikeMarkdown } from "./components/MarkdownPreview";
import { MissionBriefingChat } from "./components/MissionBriefingChat";
import { RoleAvatar, AVATAR_PRESET_OPTIONS } from "./components/RoleAvatar";
import { StageInspect } from "./components/StageInspect";
import { PipelineRail, railFromInitiative } from "./components/PipelineRail";
import { unlockSfx, playSfx, loadSfxMuted, setSfxMuted, loadBgmMuted, setBgmMuted, startBgm } from "./sfx";
import { categoryLabel, statusLabel, t, type I18nKey } from "./i18n";
import { RippleField } from "./RippleField";
import { lsGet, lsRemove, lsSet } from "./storage";
import {
  BG_STORAGE_KEY,
  BACKGROUND_PRESETS,
  THEMES,
  THEME_STORAGE_KEY,
  loadBackground,
  loadTheme,
  type ThemeId,
} from "./themes";
import type {
  AgentSlot,
  Dashboard,
  Initiative,
  InitiativeStatus,
  KnowledgeCategory,
  KnowledgeDoc,
  Lang,
  LoginResponse,
  MemoryEntry,
  MemoryKind,
  MemoryScope,
  MissionWorkspace,
  PipelineTrackInfo,
  PipelineStageInfo,
  PublishedAgent,
  PublicUser,
  RoomMessage,
  ToolkitItem,
  ToolkitKind,
  ToolRuntime,
  McpTransport,
  SkillScript,
  UserRole,
  View,
  Visibility,
} from "./types";
import { TOOLKIT_CATEGORIES } from "./types";

const CATEGORY_I18N: Record<string, I18nKey> = {
  engineering: "catEngineering",
  product: "catProduct",
  qa: "catQa",
  ops: "catOps",
  knowledge: "catKnowledge",
  browser: "catBrowser",
  office: "catOffice",
};

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

function displayTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}

export default function App() {
  const [lang, setLang] = useState<Lang>(() => (lsGet("sortie_lang", "rally_lang") as Lang) || "zh");
  const [token, setToken] = useState(() => lsGet("sortie_token", "rally_token") || "");
  const [user, setUser] = useState<PublicUser | null>(null);
  const [view, setView] = useState<View>("home");
  const [items, setItems] = useState<Initiative[]>([]);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Initiative | null>(null);
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
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [booted, setBooted] = useState(false);
  const [loginForm, setLoginForm] = useState({ username: "admin", password: "admin123" });
  const [users, setUsers] = useState<PublicUser[]>([]);
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
  const [kbSelectedId, setKbSelectedId] = useState<string | null>(null);
  const [kbEditing, setKbEditing] = useState(false);
  const [kbForm, setKbForm] = useState<{
    title: string;
    summary: string;
    body: string;
    tags: string;
    category: KnowledgeCategory;
  }>({
    title: "",
    summary: "",
    body: "",
    tags: "",
    category: "playbook",
  });
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
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
  const [agentList, setAgentList] = useState<PublishedAgent[]>([]);
  const [agentEditing, setAgentEditing] = useState(false);
  const [agentSelectedId, setAgentSelectedId] = useState<string | null>(null);
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
  const [sfxMuted, setSfxMutedState] = useState(() => loadSfxMuted());
  const [bgmMuted, setBgmMutedState] = useState(() => loadBgmMuted());
  const agentAvatarRef = useRef<HTMLInputElement | null>(null);
  const [theme, setTheme] = useState<ThemeId>(() => loadTheme());
  const [bgUrl, setBgUrl] = useState(() => loadBackground());
  const [bgDraft, setBgDraft] = useState(() => loadBackground());
  const [themeOpen, setThemeOpen] = useState(false);
  const [rippleOn, setRippleOn] = useState(() => lsGet("sortie_ripple", "rally_ripple") !== "0");
  const [reduceMotion, setReduceMotion] = useState(false);
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
    scripts: [] as SkillScript[],
  });
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importFilename, setImportFilename] = useState("");
  const [importZipB64, setImportZipB64] = useState("");
  const [importOverwrite, setImportOverwrite] = useState(true);
  const importFileRef = useRef<HTMLInputElement | null>(null);
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const avatarFileRef = useRef<HTMLInputElement | null>(null);

  const tr = useCallback((key: I18nKey | string) => t(lang, key), [lang]);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_STORAGE_KEY, theme);
    localStorage.removeItem("rally_theme");
  }, [theme]);

  useEffect(() => {
    localStorage.setItem(BG_STORAGE_KEY, bgUrl);
    localStorage.removeItem("rally_bg");
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

  useEffect(() => {
    lsSet("sortie_ripple", rippleOn ? "1" : "0", "rally_ripple");
  }, [rippleOn]);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => setReduceMotion(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
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
      <RippleField enabled={rippleOn && !reduceMotion} />
    </div>
  );

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
        <div className="theme-panel">
          <div className="theme-swatches">
            {THEMES.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`theme-swatch ${theme === item.id ? "on" : ""}`}
                onClick={() => {
                  playSfx("toggle");
                  setTheme(item.id);
                }}
              >
                {lang === "zh" ? item.labelZh : item.labelEn}
              </button>
            ))}
          </div>
          <div className="theme-swatches bg-presets">
            {BACKGROUND_PRESETS.map((bg) => (
              <button
                key={bg.id}
                type="button"
                className={`theme-swatch bg-thumb ${bgUrl === bg.src ? "on" : ""}`}
                style={{ backgroundImage: `url(${bg.src})` }}
                onClick={() => {
                  playSfx("whoosh");
                  setBgUrl(bg.src);
                  setBgDraft(bg.src);
                  // Anime art reads better with ice accents than acid green
                  if (theme === "signal") setTheme("ice");
                }}
                title={lang === "zh" ? bg.labelZh : bg.labelEn}
              >
                <span>{lang === "zh" ? bg.labelZh : bg.labelEn}</span>
              </button>
            ))}
          </div>
          <label>
            <span>{tr("themeBgUrl")}</span>
            <input
              type="text"
              value={bgDraft}
              placeholder="https://… 或 /backgrounds/*.webp"
              onChange={(e) => setBgDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  applyBgUrl();
                }
              }}
            />
          </label>
          <div className="theme-panel-actions">
            <button type="button" className="btn primary" onClick={applyBgUrl}>
              {tr("themeBg")}
            </button>
            <button type="button" className="btn ghost" onClick={() => fileRef.current?.click()}>
              {tr("themeBgUpload")}
            </button>
            <button type="button" className="btn ghost" onClick={clearBg}>
              {tr("themeBgClear")}
            </button>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/*,.webp,image/webp"
            hidden
            onChange={(e) => onBgFile(e.target.files?.[0])}
          />
          <label className="theme-toggle">
            <input type="checkbox" checked={rippleOn} onChange={(e) => setRippleOn(e.target.checked)} />
            {tr("themeFx")}
          </label>
          <label className="theme-toggle">
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
            {tr("themeSfx")}
          </label>
          <label className="theme-toggle">
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
            {tr("themeBgm")}
          </label>
        </div>
      )}
    </div>
  );

  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) || detail,
    [items, selectedId, detail]
  );

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
    lsSet("sortie_lang", lang, "rally_lang");
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
        lsRemove("sortie_token", "rally_token");
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
    prevArtCountRef.current = 0;
  }, [selectedId]);

  const artCount = (selected?.artifacts || []).length;

  useEffect(() => {
    if (!selectedId) return;
    if (artCount > prevArtCountRef.current && artCount > 0) {
      setLootOpen(true);
    }
    prevArtCountRef.current = artCount;
  }, [selectedId, artCount]);

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
      lsSet("sortie_token", data.token, "rally_token");
      setToken(data.token);
      setUser(data.user);
      setView("home");
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
    lsRemove("sortie_token", "rally_token");
    setToken("");
    setUser(null);
    setView("home");
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

  async function submitHitl(action: string) {
    setBusy(true);
    playSfx(action === "approve" ? "gate" : "warn");
    try {
      const data = await api<Initiative>(`/api/initiatives/${selectedId}/hitl`, {
        token,
        method: "POST",
        body: JSON.stringify({ action }),
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

  async function joinSquad() {
    await api(`/api/initiatives/${selectedId}/squad/join`, {
      token,
      method: "POST",
      body: JSON.stringify({ name: user?.display_name, title: user?.role }),
    });
    const data = await api<Initiative>(`/api/initiatives/${selectedId}`, { token });
    setDetail(data);
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

  const refreshKb = useCallback(async () => {
    if (!token) return;
    const q = kbQuery.trim() ? `?q=${encodeURIComponent(kbQuery.trim())}` : "";
    const data = await api<KnowledgeDoc[]>(`/api/knowledge${q}`, { token });
    setKbDocs(data);
  }, [token, kbQuery]);

  useEffect(() => {
    if (view === "admin" && user?.role === "admin") {
      loadAdminUsers().catch((e) => setError(errMsg(e)));
    }
  }, [view, user]);

  useEffect(() => {
    if (view === "knowledge" && token) {
      refreshKb().catch((e) => setError(errMsg(e)));
    }
  }, [view, token, refreshKb]);

  useEffect(() => {
    if (view !== "knowledge" || kbEditing) return;
    if (kbSelectedId && kbDocs.some((d) => d.id === kbSelectedId)) return;
    if (kbDocs[0]) setKbSelectedId(kbDocs[0].id);
  }, [view, kbDocs, kbSelectedId, kbEditing]);

  const kbSelected = useMemo(
    () => kbDocs.find((d) => d.id === kbSelectedId) || null,
    [kbDocs, kbSelectedId]
  );

  function startNewKbDoc() {
    setKbEditing(true);
    setKbSelectedId(null);
    setKbForm({ title: "", summary: "", body: "", tags: "", category: "playbook" });
  }

  function startEditKbDoc(doc: KnowledgeDoc) {
    setKbEditing(true);
    setKbSelectedId(doc.id);
    setKbForm({
      title: doc.title || "",
      summary: doc.summary || "",
      body: doc.body || "",
      tags: (doc.tags || []).join(", "),
      category: doc.category || "playbook",
    });
  }

  async function saveKbDoc(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const payload = {
      title: kbForm.title,
      summary: kbForm.summary,
      body: kbForm.body,
      tags: kbForm.tags
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      category: kbForm.category,
    };
    try {
      const data = kbSelectedId
        ? await api<KnowledgeDoc>(`/api/knowledge/${kbSelectedId}`, {
            token,
            method: "PATCH",
            body: JSON.stringify(payload),
          })
        : await api<KnowledgeDoc>("/api/knowledge", {
            token,
            method: "POST",
            body: JSON.stringify(payload),
          });
      setKbEditing(false);
      setKbSelectedId(data.id);
      await refreshKb();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteKbDoc(id: string) {
    if (!confirm(tr("delete") + "?")) return;
    await api(`/api/knowledge/${id}`, { token, method: "DELETE" });
    if (kbSelectedId === id) setKbSelectedId(null);
    await refreshKb();
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

  useEffect(() => {
    if ((view === "agents" || view === "tasks") && token) {
      refreshAgents().catch((e) => setError(errMsg(e)));
    }
  }, [view, token, refreshAgents]);

  async function saveMemory(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/memory", {
        token,
        method: "POST",
        body: JSON.stringify({
          scope: memForm.scope,
          scope_id: memForm.scope === "user" ? (user?.id || "") : memForm.scope_id || "global",
          kind: memForm.kind,
          content: memForm.content,
          tags: memForm.tags
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        }),
      });
      setMemForm({ ...memForm, content: "", tags: "" });
      await refreshMemory();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteMemory(id: string) {
    if (!confirm(tr("delete") + "?")) return;
    await api(`/api/memory/${id}`, { token, method: "DELETE" });
    await refreshMemory();
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
    const payload = {
      title: agentForm.title,
      slot: agentForm.slot,
      summary: agentForm.summary,
      persona: agentForm.persona,
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

  const enabledToolkit = useMemo(
    () => toolkitCatalog.filter((i) => i.enabled !== false),
    [toolkitCatalog]
  );

  const refreshToolkitCatalog = useCallback(async () => {
    if (!token) return;
    const data = await api<ToolkitItem[]>("/api/toolkit", { token });
    setToolkitCatalog(data);
  }, [token]);

  useEffect(() => {
    if ((view === "tasks" || view === "agents") && token) {
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
    }
  }, [view, token, refreshToolkit]);

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

  function startNewToolkit() {
    setToolkitEditing(true);
    setToolkitSelectedId(null);
    setToolkitForm({
      name: "",
      summary: "",
      runtime: "cli",
      endpoint: "",
      transport: "stdio",
      command: "",
      url: "",
      body: "",
      category: toolkitCategory || "engineering",
      tags: "",
      featured: false,
      enabled: true,
      scripts: [],
    });
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

  async function runToolkitImport() {
    if (toolkitTab !== "mcp" && toolkitTab !== "skill") return;
    setBusy(true);
    setError("");
    try {
      const data = await api<{ imported: number; items: ToolkitItem[] }>("/api/toolkit/import", {
        token,
        method: "POST",
        body: JSON.stringify({
          kind: toolkitTab,
          content: importText,
          filename: importFilename,
          zip_base64: importZipB64,
          overwrite: importOverwrite,
        }),
      });
      setImportOpen(false);
      setImportText("");
      setImportFilename("");
      setImportZipB64("");
      await refreshToolkit();
      if (data.items?.[0]) setToolkitSelectedId(data.items[0].id);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  function onToolkitImportFile(file: File | undefined) {
    if (!file) return;
    setImportFilename(file.name);
    if (file.name.toLowerCase().endsWith(".zip")) {
      const reader = new FileReader();
      reader.onload = () => {
        const buf = reader.result;
        if (!(buf instanceof ArrayBuffer)) return;
        const bytes = new Uint8Array(buf);
        let binary = "";
        for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]!);
        setImportZipB64(btoa(binary));
        setImportText("");
      };
      reader.readAsArrayBuffer(file);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setImportZipB64("");
      setImportText(typeof reader.result === "string" ? reader.result : "");
    };
    reader.readAsText(file);
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

  if (!token || !user) {
    return (
      <div className={`deck login-deck ${booted ? "booted" : ""}`}>
        {atmosphere}
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
          <p className="login-hint">{tr("loginHint")}</p>
          <div className="theme-panel-actions">
            <button type="button" className="btn ghost" onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
              {tr("lang")}
            </button>
            {themeControls}
          </div>
          {error && <div className="alert">{error}</div>}
        </form>
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
            className={view === "tasks" || view === "squad" ? "on" : ""}
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

      {view === "tasks" && (
        <div className="deck-body workbench briefing-layout">
          <aside className="sidebar briefing-sidebar">
            <MissionBriefingChat
              token={token}
              lang={lang}
              busy={busy}
              setBusy={setBusy}
              onError={(msg) => setError(msg)}
              onCreated={async (id) => {
                await refreshList();
                await refreshDash();
                setSelectedId(id);
                setView("squad");
              }}
              labels={{
                title: tr("newTask"),
                placeholder:
                  lang === "zh"
                    ? "描述任务目标… 可粘贴截图或拖入文件"
                    : "Describe the mission… paste or drop files",
                hint:
                  lang === "zh"
                    ? "多轮对话 · 图片/文档附件 · /confirm 确认"
                    : "Multimodal chat · /confirm to lock plan",
                send: tr("send"),
                confirm: lang === "zh" ? "确认组建" : "Confirm",
                reject: lang === "zh" ? "驳回重来" : "Reject",
                restart: lang === "zh" ? "新会话" : "New chat",
                waterfall: lang === "zh" ? "规划瀑布流" : "Planning waterfall",
                attach: lang === "zh" ? "附件" : "Attach",
              }}
            />
          </aside>
          <main className="main page-pad">
            <h2 className="page-title">{tr("taskMgmt")}</h2>
            <p className="hero-copy">
              {lang === "zh"
                ? "左侧对话规划官会检索历史与知识库，提案航线与编制；确认后组建小队。"
                : "Chat with the planner — it reads history & KB, proposes track & loadout, then creates the squad."}
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
              {filtered.map((item) => (
                <div key={item.id} className={`mission-wrap ${item.id === selectedId ? "active" : ""}`}>
                  <button
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
                    <span className="mission-stage">
                      {item.current_stage} · {(item.participants || []).length} {tr("membersInSquad")}
                    </span>
                  </button>
                  <button type="button" className="mission-del" onClick={() => removeTask(item.id)}>
                    ×
                  </button>
                </div>
              ))}
              {!filtered.length && <div className="empty-queue">{tr("emptyTasks")}</div>}
            </div>
          </main>
        </div>
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
              <button className="btn ghost" type="button" onClick={joinSquad}>
                {tr("joinSquad")}
              </button>
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
          <div className={`room-layout ${crewOpen || lootOpen || briefOpen ? "with-side" : "solo"}`}>
            <section className="panel chat-panel">
              <div className="panel-label">
                {tr("squadChat")} · {tr("squadChatHint")}
              </div>
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
                {messages.map((m) => (
                  <div key={m.id} className={`bubble ${m.actor_kind} type-${m.msg_type}`}>
                    <div className="bubble-meta">
                      <strong>{m.actor_name}</strong>
                      {m.role ? <span>{m.role}</span> : null}
                      <span>{displayTime(m.at)}</span>
                    </div>
                    {m.msg_type === "chat" || m.msg_type === "stage" || m.msg_type === "consult" || m.msg_type === "consult_reply" ? (
                      <MarkdownPreview text={m.text} />
                    ) : (
                      <pre>{m.text}</pre>
                    )}
                  </div>
                ))}
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
              />
            </section>
            {(crewOpen || lootOpen || briefOpen) && (
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
              </div>
            )}
          </div>
          )}
        </main>
      )}

      {view === "knowledge" && (
        <div className="deck-body workbench kb-layout">
          <aside className="sidebar">
            <div className="panel-label">{tr("codex")}</div>
            <p className="kb-hint">{tr("codexHint")}</p>
            <input
              className="kb-search"
              placeholder={tr("searchCodex")}
              value={kbQuery}
              onChange={(e) => setKbQuery(e.target.value)}
            />
            <button className="btn primary" type="button" onClick={startNewKbDoc}>
              {tr("newDoc")}
            </button>
            <div className="list">
              {kbDocs.map((doc) => (
                <button
                  key={doc.id}
                  type="button"
                  className={`mission ${doc.id === kbSelectedId && !kbEditing ? "active" : ""}`}
                  onClick={() => {
                    setKbEditing(false);
                    setKbSelectedId(doc.id);
                  }}
                >
                  <div className="mission-top">
                    <span className="mission-id">{categoryLabel(lang, doc.category)}</span>
                  </div>
                  <strong>{doc.title}</strong>
                  <span className="mission-stage">{doc.summary || (doc.tags || []).join(" · ")}</span>
                </button>
              ))}
              {!kbDocs.length && <div className="empty-queue">{tr("emptyCodex")}</div>}
            </div>
          </aside>
          <main className="main page-pad">
            {kbEditing ? (
              <form className="form kb-form" onSubmit={saveKbDoc}>
                <h2 className="page-title">{kbSelectedId ? tr("saveDoc") : tr("newDoc")}</h2>
                <label>
                  <span>{tr("docTitle")}</span>
                  <input
                    value={kbForm.title}
                    onChange={(e) => setKbForm({ ...kbForm, title: e.target.value })}
                    required
                  />
                </label>
                <label>
                  <span>{tr("docSummary")}</span>
                  <input
                    value={kbForm.summary}
                    onChange={(e) => setKbForm({ ...kbForm, summary: e.target.value })}
                  />
                </label>
                <label>
                  <span>{tr("docCategory")}</span>
                  <select
                    value={kbForm.category}
                    onChange={(e) => setKbForm({ ...kbForm, category: e.target.value as KnowledgeCategory })}
                  >
                    <option value="playbook">{tr("catPlaybook")}</option>
                    <option value="runbook">{tr("catRunbook")}</option>
                    <option value="lore">{tr("catLore")}</option>
                    <option value="reference">{tr("catReference")}</option>
                  </select>
                </label>
                <label>
                  <span>{tr("docTags")}</span>
                  <input
                    value={kbForm.tags}
                    onChange={(e) => setKbForm({ ...kbForm, tags: e.target.value })}
                  />
                </label>
                <label>
                  <span>{tr("docBody")}</span>
                  <textarea
                    className="kb-body"
                    value={kbForm.body}
                    onChange={(e) => setKbForm({ ...kbForm, body: e.target.value })}
                    required
                  />
                </label>
                <div className="kb-actions">
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("saveDoc")}
                  </button>
                  <button
                    className="btn ghost"
                    type="button"
                    onClick={() => {
                      setKbEditing(false);
                      if (!kbSelectedId && kbDocs[0]) setKbSelectedId(kbDocs[0].id);
                    }}
                  >
                    {tr("cancel")}
                  </button>
                </div>
              </form>
            ) : kbSelected ? (
              <article className="kb-article">
                <div className="topbar">
                  <div>
                    <p className="eyebrow">
                      {categoryLabel(lang, kbSelected.category)} · {(kbSelected.tags || []).join(" / ")}
                    </p>
                    <h2 className="mission-title">{kbSelected.title}</h2>
                    <p className="kb-summary">{kbSelected.summary}</p>
                  </div>
                  <div className="topbar-actions">
                    <button className="btn ghost" type="button" onClick={() => startEditKbDoc(kbSelected)}>
                      {tr("editDoc")}
                    </button>
                    <button className="btn danger" type="button" onClick={() => deleteKbDoc(kbSelected.id)}>
                      {tr("delete")}
                    </button>
                  </div>
                </div>
                <pre className="preview kb-preview">{kbSelected.body}</pre>
                <p className="kb-meta">
                  {kbSelected.author_name} · {kbSelected.updated_at}
                </p>
              </article>
            ) : (
              <div className="empty-queue">{tr("emptyCodex")}</div>
            )}
          </main>
        </div>
      )}

      {view === "memory" && (
        <div className="deck-body workbench">
          <aside className="sidebar">
            <div className="panel-label">{tr("memoryTitle")}</div>
            <p className="kb-hint">{tr("memoryHint")}</p>
            <form className="form" onSubmit={saveMemory}>
              <p className="kb-hint">{tr("memoryScopeHint")}</p>
              <label>
                <span>{tr("memScope")}</span>
                <select
                  value={memForm.scope}
                  onChange={(e) =>
                    setMemForm({
                      ...memForm,
                      scope: e.target.value as MemoryScope,
                      scope_id: e.target.value === "workspace" ? "global" : memForm.scope_id,
                    })
                  }
                >
                  <option value="workspace">{tr("scopeWorkspaceLabel")}</option>
                  <option value="user">{tr("scopeUserLabel")}</option>
                  <option value="mission">{tr("scopeMissionLabel")}</option>
                  <option value="agent">{tr("scopeAgentLabel")}</option>
                </select>
              </label>
              {(memForm.scope === "mission" || memForm.scope === "agent") && (
                <label>
                  <span>{tr("memScopeId")}</span>
                  <input
                    value={memForm.scope_id}
                    onChange={(e) => setMemForm({ ...memForm, scope_id: e.target.value })}
                    required
                  />
                </label>
              )}
              <label>
                <span>{tr("memKind")}</span>
                <select value={memForm.kind} onChange={(e) => setMemForm({ ...memForm, kind: e.target.value as MemoryKind })}>
                  <option value="fact">{tr("kindFact")}</option>
                  <option value="preference">{tr("kindPreference")}</option>
                  <option value="episode">{tr("kindEpisode")}</option>
                  <option value="lesson">{tr("kindLesson")}</option>
                </select>
              </label>
              <label>
                <span>{tr("memTags")}</span>
                <input value={memForm.tags} onChange={(e) => setMemForm({ ...memForm, tags: e.target.value })} />
              </label>
              <label>
                <span>{tr("memContent")}</span>
                <textarea
                  value={memForm.content}
                  onChange={(e) => setMemForm({ ...memForm, content: e.target.value })}
                  required
                />
              </label>
              <button className="btn primary" type="submit" disabled={busy}>
                {tr("newMemory")}
              </button>
            </form>
          </aside>
          <main className="main page-pad">
            <h2 className="page-title">{tr("memoryTitle")}</h2>
            <div className="mem-list">
              {memories.map((m) => (
                <div key={m.id} className="mem-card">
                  <div className="mission-top">
                    <span className="mission-id">
                      {m.scope}/{m.kind}
                    </span>
                    <button type="button" className="btn danger" onClick={() => deleteMemory(m.id)}>
                      {tr("delete")}
                    </button>
                  </div>
                  <p>{m.content}</p>
                  <span className="kb-meta">
                    {(m.tags || []).join(" · ")} · {m.author_name}
                  </span>
                </div>
              ))}
              {!memories.length && <div className="empty-queue">{tr("emptyMemory")}</div>}
            </div>
          </main>
        </div>
      )}

      {view === "agents" && (
        <div className="deck-body workbench kb-layout">
          <aside className="sidebar">
            <div className="panel-label">{tr("forgeTitle")}</div>
            <p className="kb-hint">{tr("forgeHint")}</p>
            <button className="btn primary" type="button" onClick={startNewAgent}>
              {tr("publishAgent")}
            </button>
            <div className="list">
              {agentList.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className={`mission ${a.id === agentSelectedId && !agentEditing ? "active" : ""}`}
                  onClick={() => {
                    playSfx("click");
                    setAgentEditing(false);
                    setAgentSelectedId(a.id);
                  }}
                >
                  <div className="mission-top">
                    <RoleAvatar avatar={a.avatar} role={a.slot} title={a.title} size={28} />
                    <span className="mission-id">{a.slot}</span>
                    <span className="st">{a.visibility}</span>
                  </div>
                  <strong>{a.title}</strong>
                  <span className="mission-stage">
                    {a.summary || a.slug} · {tr("byAuthor")} {a.author_name}
                  </span>
                  {(a.toolkit_ids || []).length > 0 && (
                    <div className="toolkit-pick">
                      {(a.toolkit_ids || []).map((id) => {
                        const item = toolkitCatalog.find((t) => t.id === id);
                        return (
                          <span key={id} className={`toolkit-chip on kind-${item?.kind || "tool"}`}>
                            {item?.name || id.slice(0, 10)}
                          </span>
                        );
                      })}
                    </div>
                  )}
                </button>
              ))}
              {!agentList.length && <div className="empty-queue">{tr("emptyAgents")}</div>}
            </div>
          </aside>
          <main className="main page-pad">
            {agentEditing ? (
              <form className="form kb-form" onSubmit={saveAgent}>
                <h2 className="page-title">{agentSelectedId ? tr("editDoc") : tr("publishAgent")}</h2>
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
                <label>
                  <span>{tr("agentTitle")}</span>
                  <input
                    value={agentForm.title}
                    onChange={(e) => setAgentForm({ ...agentForm, title: e.target.value })}
                    required
                  />
                </label>
                <label>
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
                <label>
                  <span>{tr("agentVisibility")}</span>
                  <select
                    value={agentForm.visibility}
                    onChange={(e) => setAgentForm({ ...agentForm, visibility: e.target.value as Visibility })}
                  >
                    <option value="public">{tr("visPublic")}</option>
                    <option value="private">{tr("visPrivate")}</option>
                  </select>
                </label>
                <label>
                  <span>{tr("agentSummary")}</span>
                  <input
                    value={agentForm.summary}
                    onChange={(e) => setAgentForm({ ...agentForm, summary: e.target.value })}
                  />
                </label>
                <label>
                  <span>{tr("docTags")}</span>
                  <input
                    value={agentForm.tags}
                    onChange={(e) => setAgentForm({ ...agentForm, tags: e.target.value })}
                  />
                </label>
                <label>
                  <span>{tr("agentPersona")}</span>
                  <textarea
                    className="kb-body"
                    value={agentForm.persona}
                    onChange={(e) => setAgentForm({ ...agentForm, persona: e.target.value })}
                    required
                  />
                </label>
                <div className="loadout-block">
                  <span className="loadout-label">{tr("agentToolkits")}</span>
                  <p className="loadout-hint">{tr("agentToolkitsHint")}</p>
                  {enabledToolkit.length === 0 ? (
                    <p className="loadout-hint">{tr("noToolkitYet")}</p>
                  ) : (
                    <div className="toolkit-pick">
                      {enabledToolkit.map((item) => {
                        const on = agentForm.toolkit_ids.includes(item.id);
                        return (
                          <button
                            key={item.id}
                            type="button"
                            className={`toolkit-chip kind-${item.kind} ${on ? "on" : ""}`}
                            onClick={() => toggleAgentFormToolkit(item.id)}
                            title={item.summary || item.name}
                          >
                            {item.kind}/{item.name}
                          </button>
                        );
                      })}
                    </div>
                  )}
                  <div className="loadout-slot-actions">
                    <span className="muted">
                      {tr("toolsSelected")} {agentForm.toolkit_ids.length}
                    </span>
                    {agentForm.toolkit_ids.length > 0 && (
                      <button
                        type="button"
                        className="btn linkish"
                        onClick={() => setAgentForm({ ...agentForm, toolkit_ids: [] })}
                      >
                        {tr("clearTools")}
                      </button>
                    )}
                  </div>
                </div>
                <div className="kb-actions">
                  <button className="btn primary" type="submit" disabled={busy}>
                    {tr("saveDoc")}
                  </button>
                  <button className="btn ghost" type="button" onClick={() => setAgentEditing(false)}>
                    {tr("cancel")}
                  </button>
                </div>
              </form>
            ) : agentSelected ? (
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
            ) : (
              <div className="empty-queue">{tr("emptyAgents")}</div>
            )}
          </main>
        </div>
      )}

      {view === "toolkit" && (
        <div className="deck-body workbench toolkit-market">
          <header className="toolkit-head">
            <div>
              <div className="panel-label">{tr("toolkitTitle")}</div>
              <p className="kb-hint">{tr("toolkitHint")}</p>
            </div>
            <div className="toolkit-head-actions">
              <input
                className="toolkit-search"
                type="search"
                placeholder={tr("tkSearch")}
                value={toolkitQuery}
                onChange={(e) => setToolkitQuery(e.target.value)}
              />
              <button className="btn primary" type="button" onClick={startNewToolkit}>
                {tr("newToolkit")}
              </button>
              {(toolkitTab === "mcp" || toolkitTab === "skill") && (
                <button
                  className="btn ghost"
                  type="button"
                  onClick={() => {
                    setImportOpen((v) => !v);
                    setImportText("");
                    setImportZipB64("");
                    setImportFilename("");
                  }}
                >
                  {tr("importToolkit")}
                </button>
              )}
            </div>
          </header>

          <div className="filters toolkit-kind-tabs">
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
                className={`filter ${toolkitTab === kind ? "on" : ""}`}
                onClick={() => {
                  setToolkitTab(kind);
                  setToolkitEditing(false);
                  setToolkitSelectedId(null);
                }}
              >
                {tr(label)}
              </button>
            ))}
          </div>

          {importOpen && (toolkitTab === "mcp" || toolkitTab === "skill") && (
            <div className="import-box toolkit-import">
              <p className="kb-hint">
                {toolkitTab === "mcp" ? tr("importHintMcp") : tr("importHintSkill")}
              </p>
              <textarea
                className="kb-body"
                placeholder={tr("importPaste")}
                value={importText}
                onChange={(e) => {
                  setImportText(e.target.value);
                  setImportZipB64("");
                }}
              />
              <div className="theme-panel-actions">
                <button type="button" className="btn ghost" onClick={() => importFileRef.current?.click()}>
                  {tr("importFile")}
                </button>
                <label className="theme-toggle">
                  <input
                    type="checkbox"
                    checked={importOverwrite}
                    onChange={(e) => setImportOverwrite(e.target.checked)}
                  />
                  {tr("importOverwrite")}
                </label>
                <button
                  type="button"
                  className="btn primary"
                  disabled={busy || (!importText.trim() && !importZipB64)}
                  onClick={runToolkitImport}
                >
                  {tr("importRun")}
                </button>
              </div>
              {importFilename && <p className="kb-meta">{importFilename}</p>}
              <input
                ref={importFileRef}
                type="file"
                accept={
                  toolkitTab === "skill"
                    ? ".md,.json,.zip,text/markdown,application/json,application/zip"
                    : ".json,application/json,text/plain"
                }
                hidden
                onChange={(e) => onToolkitImportFile(e.target.files?.[0])}
              />
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
                </p>
              </article>
            ) : (
              <>
                <div className="toolkit-cat-row">
                  <button
                    type="button"
                    className={`toolkit-cat ${!toolkitCategory ? "on" : ""}`}
                    onClick={() => setToolkitCategory("")}
                  >
                    {tr("tkAllCategories")}
                  </button>
                  {TOOLKIT_CATEGORIES.map((cat) => (
                    <button
                      key={cat}
                      type="button"
                      className={`toolkit-cat ${toolkitCategory === cat ? "on" : ""}`}
                      onClick={() => setToolkitCategory(cat)}
                    >
                      {toolkitCategoryLabel(cat)}
                    </button>
                  ))}
                </div>

                {!!toolkitFeatured.length && !toolkitCategory && !toolkitQuery.trim() && (
                  <section className="toolkit-featured">
                    <div className="panel-label">{tr("tkFeaturedSection")}</div>
                    <div className="toolkit-featured-row">
                      {toolkitFeatured.map((item) => (
                        <button
                          key={item.id}
                          type="button"
                          className="toolkit-feature-card"
                          onClick={() => setToolkitSelectedId(item.id)}
                        >
                          <span className="toolkit-card-kind">
                            {tr(
                              item.kind === "tool" ? "tabTools" : item.kind === "mcp" ? "tabMcp" : "tabSkills"
                            )}
                          </span>
                          <strong>{item.name}</strong>
                          <span className="toolkit-card-summary">{item.summary}</span>
                          {item.category && (
                            <span className="toolkit-card-cat">{toolkitCategoryLabel(item.category)}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </section>
                )}

                <section className="toolkit-catalog">
                  <div className="panel-label">{tr("tkCatalog")}</div>
                  <div className="toolkit-grid">
                    {toolkitGridItems.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className="toolkit-card"
                        onClick={() => setToolkitSelectedId(item.id)}
                      >
                        <div className="toolkit-card-top">
                          <span className={`toolkit-card-kind kind-${item.kind}`}>
                            {tr(
                              item.kind === "tool" ? "tabTools" : item.kind === "mcp" ? "tabMcp" : "tabSkills"
                            )}
                          </span>
                          <span className={`st ${item.enabled ? "done" : "failed"}`}>
                            {item.enabled ? tr("tkEnabled") : tr("tkDisabled")}
                          </span>
                        </div>
                        <strong>{item.name}</strong>
                        <span className="toolkit-card-summary">{item.summary}</span>
                        <div className="toolkit-card-meta">
                          {item.category && (
                            <span className="toolkit-card-cat">{toolkitCategoryLabel(item.category)}</span>
                          )}
                          {(item.tags || []).slice(0, 3).map((tag) => (
                            <span key={tag} className="toolkit-tag">
                              {tag}
                            </span>
                          ))}
                        </div>
                      </button>
                    ))}
                  </div>
                  {!toolkitItems.length && <div className="empty-queue">{tr("emptyToolkit")}</div>}
                </section>
              </>
            )}
          </main>
        </div>
      )}

      {view === "admin" && user.role === "admin" && (
        <main className="main page-pad">
          <h2 className="page-title">{tr("admin")} · {tr("users")}</h2>
          <form className="form admin-form" onSubmit={createUser}>
            <input
              placeholder={tr("username")}
              value={newUser.username}
              onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
              required
            />
            <input
              placeholder={tr("displayName")}
              value={newUser.display_name}
              onChange={(e) => setNewUser({ ...newUser, display_name: e.target.value })}
              required
            />
            <input
              type="password"
              placeholder={tr("password")}
              value={newUser.password}
              onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
              required
            />
            <select value={newUser.role} onChange={(e) => setNewUser({ ...newUser, role: e.target.value as UserRole })}>
              <option value="member">{tr("memberRole")}</option>
              <option value="admin">{tr("adminRole")}</option>
            </select>
            <button className="btn primary" type="submit" disabled={busy}>
              {tr("addUser")}
            </button>
          </form>
          <div className="panel">
            <div className="user-table">
              {users.map((u) => (
                <div key={u.id} className="user-row">
                  <strong>{u.display_name}</strong>
                  <code>{u.username}</code>
                  <span className="st">{u.role === "admin" ? tr("adminRole") : tr("memberRole")}</span>
                  <button type="button" className="btn danger" onClick={() => deleteUser(u.id)}>
                    {tr("delete")}
                  </button>
                </div>
              ))}
            </div>
          </div>
        </main>
      )}
    </div>
  );
}
