import { useMemo, type CSSProperties } from "react";

const PRESET_HUE: Record<string, number> = {
  product: 28,
  design: 265,
  eng: 152,
  eng_ios: 200,
  eng_android: 130,
  eng_web: 190,
  eng_backend: 210,
  eng_agent: 300,
  qa: 45,
  deploy: 170,
  ops: 0,
};

const GLYPH: Record<string, string> = {
  product: "PM",
  design: "DX",
  eng: "EN",
  eng_ios: "iOS",
  eng_android: "AND",
  eng_web: "WEB",
  eng_backend: "API",
  eng_agent: "AG",
  qa: "QA",
  deploy: "SH",
  ops: "OP",
};

/** Built-in comic/pixel portraits */
export const PRESET_SRC: Record<string, string> = {
  product: "/avatars/product.webp",
  design: "/avatars/design.webp",
  eng: "/avatars/eng.webp",
  eng_ios: "/avatars/eng_ios.webp",
  eng_android: "/avatars/eng_android.webp",
  eng_web: "/avatars/eng_web.webp",
  eng_backend: "/avatars/eng_backend.webp",
  eng_agent: "/avatars/eng_agent.webp",
  qa: "/avatars/qa.webp",
  deploy: "/avatars/deploy.webp",
  ops: "/avatars/eng.webp",
};

export function resolveAvatarRole(avatar?: string | null, role?: string | null): string {
  if (avatar?.startsWith("preset:")) return avatar.slice(7);
  return (role || "ops").toLowerCase();
}

export function resolveAvatarSrc(avatar?: string | null, _role?: string | null): string | null {
  // Editorial: preset portraits retired — glyph circle is the default.
  // Only a user-set custom avatar (upload / URL) renders an img.
  if (!avatar) return null;
  if (avatar.startsWith("preset:")) return null;
  if (avatar.startsWith("http") || avatar.startsWith("data:") || avatar.startsWith("/")) {
    return avatar;
  }
  return null;
}

export function RoleAvatar({
  avatar,
  role,
  title,
  size = 36,
  live = false,
  speaking = false,
  onClick,
  className = "",
}: {
  avatar?: string | null;
  role?: string | null;
  title?: string;
  size?: number;
  live?: boolean;
  speaking?: boolean;
  onClick?: () => void;
  className?: string;
}) {
  const roleKey = resolveAvatarRole(avatar, role);
  const hue = PRESET_HUE[roleKey] ?? 160;
  const glyph = GLYPH[roleKey] || (title || roleKey).slice(0, 2).toUpperCase();
  const src = resolveAvatarSrc(avatar, role);

  const style = useMemo(
    () =>
      ({
        width: size,
        height: size,
        ["--ra-h" as string]: String(hue),
        fontSize: Math.max(9, Math.round(size * 0.28)),
      }) as CSSProperties,
    [size, hue],
  );

  const Tag = onClick ? "button" : "span";
  return (
    <Tag
      type={onClick ? "button" : undefined}
      className={`role-avatar ${live ? "live" : ""} ${speaking ? "speaking" : ""} ${src ? "has-img" : ""} ${className}`}
      style={style}
      onClick={onClick}
      title={title || roleKey}
      aria-label={title || roleKey}
    >
      {src ? <img src={src} alt="" /> : <span className="role-avatar-glyph">{glyph}</span>}
    </Tag>
  );
}

export const AVATAR_PRESET_OPTIONS = Object.keys(PRESET_HUE)
  .filter((r) => r !== "ops")
  .map((role) => ({
    id: `preset:${role}`,
    role,
    src: PRESET_SRC[role],
  }));
