import { useEffect, useState } from "react";
import { addMissionShare, listMissionShares, removeMissionShare, searchUsers } from "../api";
import { t } from "../i18n";
import type { Initiative, Lang, MissionShare, PublicUser } from "../types";

interface ShareManagerProps {
  token: string;
  lang: Lang;
  /** 要管理的任务（列表页选中的那张卡片） */
  initiative: Initiative;
  /** 当前登录用户，用于判定 owner / 本人退出共享 */
  currentUser: PublicUser | null;
  onClose: () => void;
  /** 共享变更后回传最新任务，调用方同步列表与详情 */
  onChanged: (ini: Initiative) => void;
  onError: (msg: string) => void;
}

/**
 * 任务共享管理弹窗（任务列表页入口）。
 * owner / admin：可搜索账号名或账号 ID 添加共享、移除任意成员；
 * 被共享用户：仅可查看成员列表并退出共享。重复添加由后端幂等兜底。
 */
export function ShareManager({ token, lang, initiative, currentUser, onClose, onChanged, onError }: ShareManagerProps) {
  const [shares, setShares] = useState<MissionShare[]>([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PublicUser[]>([]);
  const [busy, setBusy] = useState(false);

  const tr = (key: I18nKeyLocal) => t(lang, key);
  const isOwner = Boolean(
    currentUser && (!initiative.created_by || initiative.created_by === currentUser.id || currentUser.role === "admin")
  );

  useEffect(() => {
    let cancelled = false;
    listMissionShares(token, initiative.id)
      .then((d) => {
        if (!cancelled) setShares(d || []);
      })
      .catch((e: unknown) => onError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 弹窗生命周期内固定跟随 initiative.id
  }, [token, initiative.id]);

  // 搜索账号：按账号名 / 显示名 / 账号 ID 模糊匹配，250ms 防抖
  useEffect(() => {
    if (!isOwner) return;
    const kw = query.trim();
    if (!kw) {
      setResults([]);
      return;
    }
    const timer = window.setTimeout(() => {
      searchUsers(token, kw)
        .then(setResults)
        .catch(() => setResults([]));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query, isOwner, token]);

  async function shareToUser(u: PublicUser) {
    setBusy(true);
    try {
      const data = await addMissionShare(token, initiative.id, u.id);
      setShares(data.shared_with || []);
      setQuery("");
      setResults([]);
      onChanged(data);
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function unshareUser(userId: string) {
    setBusy(true);
    try {
      const data = await removeMissionShare(token, initiative.id, userId);
      setShares(data.shared_with || []);
      onChanged(data);
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="share-modal-overlay" onClick={onClose}>
      {/* 点击遮罩关闭；面板内点击不冒泡 */}
      <section className="panel share-modal" onClick={(e) => e.stopPropagation()}>
        <div className="panel-label row-between">
          <span>
            {tr("sharedUsers")} · <code>{initiative.title}</code>
          </span>
          <button type="button" className="btn ghost tiny" onClick={onClose}>
            ×
          </button>
        </div>
        <p className="tiny subroom-drawer-hint">{tr("shareHint")}</p>
        <div className="share-list">
          <div className="share-row">
            <span className="share-name">{isOwner ? tr("shareSelfOwner") : tr("shareOwner")}</span>
            <em className="share-tag">{tr("shareOwnerTag")}</em>
          </div>
          {shares.map((s) => {
            const isSelf = currentUser?.id === s.user_id;
            return (
              <div className="share-row" key={s.user_id}>
                <span className="share-name">
                  {s.display_name || s.username || s.user_id}
                  {s.username ? <code>{s.username}</code> : null}
                </span>
                {(isOwner || isSelf) && (
                  <button type="button" className="btn ghost tiny" disabled={busy} onClick={() => void unshareUser(s.user_id)}>
                    {isSelf && !isOwner ? tr("shareLeave") : tr("shareRemove")}
                  </button>
                )}
              </div>
            );
          })}
          {!shares.length && <p className="empty-queue">{tr("shareEmpty")}</p>}
        </div>
        {isOwner && (
          <div className="share-search">
            <input
              className="share-search-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={tr("shareAddPlaceholder")}
              autoFocus
            />
            {results.map((u) => (
              <div className="share-row" key={u.id}>
                <span className="share-name">
                  {u.display_name || u.username}
                  <code>
                    {u.username} · {u.id.slice(0, 10)}
                  </code>
                </span>
                <button type="button" className="btn ghost tiny" disabled={busy} onClick={() => void shareToUser(u)}>
                  ＋ {tr("shareAdd")}
                </button>
              </div>
            ))}
            {query.trim() && !results.length && <p className="empty-queue">{tr("shareSearchEmpty")}</p>}
          </div>
        )}
      </section>
    </div>
  );
}

type I18nKeyLocal = Parameters<typeof t>[1];
