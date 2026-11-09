import { useEffect, useState } from "react";

import { THEMES, type ThemeId } from "../themes";
import type { I18nKey } from "../i18n";
import type { Lang, PublicUser } from "../types";
import { ApiKeysPage } from "./ApiKeysPage";
import { GitSettingsPanel } from "./GitSettingsPanel";
import { ImIntegrationsPanel } from "./ImIntegrationsPanel";
import { MissionInsightsPanel } from "./MissionInsightsPanel";

/**
 * 设置中心（设计稿 v2）：全屏面板 + 左轨目录 + 宽内容区。
 *
 * 这里承接原「密钥」一级页面：API 密钥与 Git 凭据都收进「接入」分组。
 * 账号与外观复用 App 现有状态；数据页复用现有的导出 / 注销 / 退出逻辑。
 */

const ZH = {
  title: "设置",
  signature: "SETTINGS / CONTROL ROOM",
  personal: "个人",
  access: "接入",
  dataGroup: "数据",
  account: "账号",
  appearance: "外观",
  git: "Git 凭据",
  keys: "API 密钥",
  im: "IM 集成",
  data: "导出与删除",
  insights: "耗时分析",
  back: "返回应用",
  close: "关闭设置",
  accountTitle: "账号",
  accountLede: "登录名与角色由管理员分配；显示名会出现在任务署名与消息里。",
  displayName: "显示名",
  username: "登录名",
  role: "角色",
  status: "状态",
  admin: "管理员",
  member: "成员",
  statusActive: "正常",
  statusPending: "待审核",
  statusRejected: "已拒绝",
  accountNote: "账号信息由管理员维护；如需修改显示名或角色，请联系工作区管理员。",
  appearanceTitle: "外观",
  appearanceLede: "纸与墨共用同一套版式，切主题只换色，不改变信息密度。",
  theme: "主题",
  themeHint: "默认「纸」；「墨」适合夜间与投屏",
  themeDefault: "默认",
  language: "界面语言",
  languageHint: "只影响本机，不同步给其他成员",
  sfx: "界面音效",
  sfxHint: "切页与提交时的短音",
  bgm: "背景音乐",
  bgmHint: "低音量环境音，默认关闭",
  background: "背景图",
  backgroundHint: "支持外链图片；留空恢复默认底纹",
  apply: "应用",
  clear: "清除",
  dataTitle: "数据与账号",
  dataLede: "带走你的东西，或者彻底离开。两件事都需要二次确认。",
  exportData: "导出我的数据",
  exportHint: "任务、消息、记忆、凭据元信息，JSON 打包下载",
  exporting: "导出中…",
  logout: "退出登录",
  logoutHint: "只清本机会话，不影响其他设备",
  deleteAccount: "删除账号",
  deleteHint: "历史消息署名会保留为「已注销用户」，不可恢复",
  remove: "删除",
  footer: "凭据密钥只写不读；管理员在「Git 凭据」页维护工作区凭据与仓库，成员可只读查看配置。",
};

const EN: typeof ZH = {
  title: "Settings",
  signature: "SETTINGS / CONTROL ROOM",
  personal: "Personal",
  access: "Access",
  dataGroup: "Data",
  account: "Account",
  appearance: "Appearance",
  git: "Git credentials",
  keys: "API keys",
  im: "IM integrations",
  data: "Export & delete",
  insights: "Duration insights",
  back: "Back to app",
  close: "Close settings",
  accountTitle: "Account",
  accountLede: "Your login and role are assigned by an admin; display name appears on missions and messages.",
  displayName: "Display name",
  username: "Login",
  role: "Role",
  status: "Status",
  admin: "Admin",
  member: "Member",
  statusActive: "Active",
  statusPending: "Pending",
  statusRejected: "Rejected",
  accountNote: "Account fields are managed by an admin. Ask a workspace admin to change your display name or role.",
  appearanceTitle: "Appearance",
  appearanceLede: "Paper and Ink share the same layout; switching only changes color, not density.",
  theme: "Theme",
  themeHint: "Paper is default; Ink suits night work and projection",
  themeDefault: "default",
  language: "Language",
  languageHint: "Local to this browser; not synced to other members",
  sfx: "Interface sounds",
  sfxHint: "Short cues on navigation and submit",
  bgm: "Background music",
  bgmHint: "Low-volume ambience, off by default",
  background: "Background image",
  backgroundHint: "External URL; leave blank to restore the default texture",
  apply: "Apply",
  clear: "Clear",
  dataTitle: "Data & account",
  dataLede: "Take your data with you, or leave for good. Both require a second confirmation.",
  exportData: "Export my data",
  exportHint: "Missions, messages, memory and credential metadata as JSON",
  exporting: "Exporting…",
  logout: "Log out",
  logoutHint: "Clears this browser session only",
  deleteAccount: "Delete account",
  deleteHint: "Message authorship becomes “deleted user”; this cannot be undone",
  remove: "Delete",
  footer: "Secrets are write-only; admins manage workspace credentials and repos here, while members have read-only access.",
};

type Tab = "git" | "keys" | "im" | "account" | "appearance" | "data" | "insights";

interface Props {
  open: boolean;
  onClose: () => void;
  token: string;
  user: PublicUser;
  lang: Lang;
  tr: (key: I18nKey | string) => string;
  theme: ThemeId;
  onTheme: (theme: ThemeId) => void;
  bgUrl: string;
  onBgUrl: (url: string) => void;
  sfxMuted: boolean;
  bgmMuted: boolean;
  onSfxMuted: (muted: boolean) => void;
  onBgmMuted: (muted: boolean) => void;
  exporting: boolean;
  exportNote: string;
  onExport: () => void;
  onDeleteAccount: () => void;
  onLogout: () => void;
  onLangChange: (lang: Lang) => void;
}

export function SettingsCenter({
  open,
  onClose,
  token,
  user,
  lang,
  tr,
  theme,
  onTheme,
  bgUrl,
  onBgUrl,
  sfxMuted,
  bgmMuted,
  onSfxMuted,
  onBgmMuted,
  exporting,
  exportNote,
  onExport,
  onDeleteAccount,
  onLogout,
  onLangChange,
}: Props) {
  const C = lang === "zh" ? ZH : EN;
  const [tab, setTab] = useState<Tab>("git");
  const [gitCount, setGitCount] = useState(0);
  const [keysCount, setKeysCount] = useState(0);
  const [bgDraft, setBgDraft] = useState(bgUrl);

  useEffect(() => {
    if (open) setBgDraft(bgUrl);
  }, [open, bgUrl]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Git 表单与密钥签发弹层自己处理 Esc，避免一次关两层
      if (document.querySelector(".stg-modal, .akeys-overlay")) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  const roleText = user.role === "admin" ? C.admin : C.member;
  const statusText =
    user.status === "active"
      ? C.statusActive
      : user.status === "pending"
        ? C.statusPending
        : C.statusRejected;
  const initial = (user.display_name || user.username || "?").slice(0, 1);
  const currentLabel =
    tab === "git"
      ? C.git
      : tab === "keys"
        ? C.keys
        : tab === "im"
          ? C.im
        : tab === "account"
          ? C.account
          : tab === "appearance"
            ? C.appearance
            : tab === "insights"
              ? C.insights
              : C.data;

  return (
    <div
      className="settings-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section className="settings-center" role="dialog" aria-modal="true" aria-label={C.title}>
        <aside className="settings-rail">
          <div className="settings-user">
            <div className="av">{initial}</div>
            <b>{user.display_name || user.username}</b>
            <span>
              {user.role === "admin" ? "ADMIN" : "MEMBER"} · {user.username}
            </span>
          </div>

          <nav className="settings-nav" aria-label={C.title}>
            <div className="grp">
              <div className="cap">{C.personal}</div>
              <button type="button" className={tab === "account" ? "on" : ""} onClick={() => setTab("account")}>
                {C.account}
              </button>
              <button type="button" className={tab === "appearance" ? "on" : ""} onClick={() => setTab("appearance")}>
                {C.appearance}
              </button>
            </div>
            <div className="grp">
              <div className="cap">{C.access}</div>
              <button type="button" className={tab === "git" ? "on" : ""} onClick={() => setTab("git")}>
                {C.git}
                <span className="num">{gitCount || ""}</span>
              </button>
              <button type="button" className={tab === "keys" ? "on" : ""} onClick={() => setTab("keys")}>
                {C.keys}
                <span className="num">{keysCount || ""}</span>
              </button>
              <button type="button" className={tab === "im" ? "on" : ""} onClick={() => setTab("im")}>
                {C.im}
              </button>
            </div>
            <div className="grp">
              <div className="cap">{C.dataGroup}</div>
              <button type="button" className={tab === "data" ? "on" : ""} onClick={() => setTab("data")}>
                {C.data}
              </button>
              <button type="button" className={tab === "insights" ? "on" : ""} onClick={() => setTab("insights")}>
                {C.insights}
              </button>
            </div>
          </nav>
        </aside>

        <div className="settings-pane">
          <div className="settings-bar">
            <div className="crumb">
              {C.title} / <b>{currentLabel}</b>
            </div>
            <span className="sp" />
            <button type="button" className="settings-close" onClick={onClose} aria-label={C.close}>
              ×
            </button>
          </div>

          <div className="settings-body">
            {tab === "git" && (
              <GitSettingsPanel
                token={token}
                lang={lang}
                isAdmin={user.role === "admin"}
                onCountChange={setGitCount}
              />
            )}

            {tab === "keys" && (
              <div className="settings-embed">
                <ApiKeysPage token={token} lang={lang} tr={tr} onCountChange={setKeysCount} />
              </div>
            )}

            {tab === "im" && (
              <div className="settings-embed">
                <ImIntegrationsPanel token={token} lang={lang} />
              </div>
            )}

            {tab === "account" && (
              <div className="stg">
                <header className="stg-head">
                  <div>
                    <h2>{C.accountTitle}</h2>
                    <p>{C.accountLede}</p>
                  </div>
                </header>

                <div className="stg-profile">
                  <div className="stg-avatar">{initial}</div>
                  <div>
                    <b>{user.display_name || user.username}</b>
                    <span>@{user.username}</span>
                  </div>
                </div>

                <div className="stg-kv">
                  <div>
                    <span>{C.displayName}</span>
                    <b>{user.display_name || "—"}</b>
                  </div>
                  <div>
                    <span>{C.username}</span>
                    <b>{user.username}</b>
                  </div>
                  <div>
                    <span>{C.role}</span>
                    <b>{roleText}</b>
                  </div>
                  <div>
                    <span>{C.status}</span>
                    <b>{statusText}</b>
                  </div>
                </div>
                <p className="stg-note">{C.accountNote}</p>
              </div>
            )}

            {tab === "appearance" && (
              <div className="stg">
                <header className="stg-head">
                  <div>
                    <h2>{C.appearanceTitle}</h2>
                    <p>{C.appearanceLede}</p>
                  </div>
                </header>

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.theme}</b>
                    <span>{C.themeHint}</span>
                  </div>
                  <div className="stg-seg">
                    {THEMES.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={theme === item.id ? "on" : ""}
                        onClick={() => onTheme(item.id)}
                      >
                        {lang === "zh" ? item.labelZh : item.labelEn}
                        {item.id === "paper" && <em>{C.themeDefault}</em>}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.language}</b>
                    <span>{C.languageHint}</span>
                  </div>
                  <div className="stg-seg">
                    <button type="button" className={lang === "zh" ? "on" : ""} onClick={() => onLangChange("zh")}>
                      中文
                    </button>
                    <button type="button" className={lang === "en" ? "on" : ""} onClick={() => onLangChange("en")}>
                      EN
                    </button>
                  </div>
                </div>

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.sfx}</b>
                    <span>{C.sfxHint}</span>
                  </div>
                  <button
                    type="button"
                    className={`stg-switch${sfxMuted ? "" : " on"}`}
                    aria-pressed={!sfxMuted}
                    aria-label={C.sfx}
                    onClick={() => onSfxMuted(!sfxMuted)}
                  />
                </div>

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.bgm}</b>
                    <span>{C.bgmHint}</span>
                  </div>
                  <button
                    type="button"
                    className={`stg-switch${bgmMuted ? "" : " on"}`}
                    aria-pressed={!bgmMuted}
                    aria-label={C.bgm}
                    onClick={() => onBgmMuted(!bgmMuted)}
                  />
                </div>

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.background}</b>
                    <span>{C.backgroundHint}</span>
                  </div>
                  <div className="stg-bg">
                    <input
                      value={bgDraft}
                      placeholder="https://…"
                      onChange={(e) => setBgDraft(e.target.value)}
                    />
                    <button type="button" className="stg-quiet" onClick={() => onBgUrl(bgDraft.trim())}>
                      {C.apply}
                    </button>
                    <button
                      type="button"
                      className="stg-quiet"
                      onClick={() => {
                        setBgDraft("");
                        onBgUrl("");
                      }}
                    >
                      {C.clear}
                    </button>
                  </div>
                </div>
              </div>
            )}

            {tab === "data" && (
              <div className="stg">
                <header className="stg-head">
                  <div>
                    <h2>{C.dataTitle}</h2>
                    <p>{C.dataLede}</p>
                  </div>
                </header>

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.exportData}</b>
                    <span>{C.exportHint}</span>
                  </div>
                  <button type="button" className="stg-quiet" onClick={onExport} disabled={exporting}>
                    {exporting ? C.exporting : C.exportData}
                  </button>
                </div>
                {exportNote && <p className="stg-note">{exportNote}</p>}

                <div className="stg-setting">
                  <div className="t">
                    <b>{C.logout}</b>
                    <span>{C.logoutHint}</span>
                  </div>
                  <button type="button" className="stg-quiet" onClick={onLogout}>
                    {C.logout}
                  </button>
                </div>

                <div className="stg-setting danger">
                  <div className="t">
                    <b>{C.deleteAccount}</b>
                    <span>{C.deleteHint}</span>
                  </div>
                  <button type="button" className="stg-quiet danger" onClick={onDeleteAccount}>
                    {C.remove}
                  </button>
                </div>
              </div>
            )}

            {tab === "insights" && (
              <div className="settings-embed">
                <MissionInsightsPanel token={token} lang={lang} />
              </div>
            )}
          </div>

          <div className="settings-foot">{C.footer}</div>
        </div>
      </section>
    </div>
  );
}
