export type ThemeId = "paper" | "ink";

export interface ThemeOption {
  id: ThemeId;
  labelZh: string;
  labelEn: string;
}

/** Editorial themes: 纸（浅·默认）与 墨（暗） */
export const THEMES: ThemeOption[] = [
  { id: "paper", labelZh: "纸 · 浅色", labelEn: "Paper (Light)" },
  { id: "ink", labelZh: "墨 · 暗色", labelEn: "Ink (Dark)" },
];

export interface BackgroundPreset {
  id: string;
  src: string;
  labelZh: string;
  labelEn: string;
}

/** Editorial style ships no decorative wallpapers; users may still set a
 *  custom background URL in the theme panel. */
export const BACKGROUND_PRESETS: BackgroundPreset[] = [];

export const THEME_STORAGE_KEY = "sortie_theme";
export const BG_STORAGE_KEY = "sortie_bg";

export function loadTheme(): ThemeId {
  const raw = localStorage.getItem(THEME_STORAGE_KEY);
  if (THEMES.some((t) => t.id === raw)) return raw as ThemeId;
  return "paper";
}

export function loadBackground(): string {
  const stored = localStorage.getItem(BG_STORAGE_KEY);
  return stored || "";
}
