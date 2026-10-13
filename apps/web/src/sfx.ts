/** Lightweight game-like UI SFX via Web Audio + looping BGM. */

type SfxKind = "click" | "hover" | "ok" | "warn" | "gate" | "whoosh" | "toggle" | "unlock";

let ctx: AudioContext | null = null;
let muted = false;
let bgmMuted = true;
let bgmEl: HTMLAudioElement | null = null;
let bgmStarted = false;

const MUTE_KEY = "sortie_sfx_mute";
const BGM_MUTE_KEY = "sortie_bgm_mute";
export const BGM_SRC = "/audio/bgm-ops.mp3";

export function loadSfxMuted(): boolean {
  return localStorage.getItem(MUTE_KEY) === "1";
}

export function setSfxMuted(v: boolean) {
  muted = v;
  localStorage.setItem(MUTE_KEY, v ? "1" : "0");
}

/** Default BGM off until user enables (autoplay policy + preference). */
export function loadBgmMuted(): boolean {
  const raw = localStorage.getItem(BGM_MUTE_KEY);
  if (raw === null) return true;
  return raw === "1";
}

export function setBgmMuted(v: boolean) {
  bgmMuted = v;
  localStorage.setItem(BGM_MUTE_KEY, v ? "1" : "0");
  syncBgmPlayback();
}

muted = typeof window !== "undefined" ? loadSfxMuted() : false;
bgmMuted = typeof window !== "undefined" ? loadBgmMuted() : true;

function ac(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (!ctx) {
    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return null;
    ctx = new Ctx();
  }
  return ctx;
}

function ensureBgm(): HTMLAudioElement | null {
  if (typeof window === "undefined") return null;
  if (!bgmEl) {
    bgmEl = new Audio(BGM_SRC);
    bgmEl.loop = true;
    bgmEl.preload = "auto";
    bgmEl.volume = 0.55;
  }
  return bgmEl;
}

function syncBgmPlayback() {
  const el = ensureBgm();
  if (!el) return;
  el.volume = 0.55;
  if (bgmMuted) {
    el.pause();
    return;
  }
  if (!bgmStarted) return;
  const p = el.play();
  if (p && typeof p.catch === "function") p.catch(() => undefined);
}

/** Call from a user gesture so AudioContext / BGM can start. */
export function unlockSfx() {
  muted = loadSfxMuted();
  bgmMuted = loadBgmMuted();
  const c = ac();
  if (c && c.state === "suspended") void c.resume();
  bgmStarted = true;
  syncBgmPlayback();
}

/** Enable BGM after a gesture; starts loop if not muted. */
export function startBgm() {
  bgmStarted = true;
  bgmMuted = loadBgmMuted();
  unlockSfx();
  syncBgmPlayback();
}

function toneNow(
  c: AudioContext,
  freq: number,
  dur: number,
  type: OscillatorType,
  gain = 0.18,
  slideTo?: number,
) {
  const t0 = c.currentTime;
  const osc = c.createOscillator();
  const g = c.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, t0);
  if (slideTo != null) {
    osc.frequency.exponentialRampToValueAtTime(Math.max(40, slideTo), t0 + dur);
  }
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(gain, t0 + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g);
  g.connect(c.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

function fire(kind: SfxKind, c: AudioContext) {
  switch (kind) {
    case "click":
      toneNow(c, 620, 0.08, "triangle", 0.22);
      toneNow(c, 920, 0.05, "square", 0.1);
      break;
    case "hover":
      toneNow(c, 720, 0.04, "sine", 0.08);
      break;
    case "ok":
      toneNow(c, 480, 0.11, "triangle", 0.22, 960);
      break;
    case "warn":
      toneNow(c, 240, 0.16, "sawtooth", 0.18, 150);
      break;
    case "gate":
      toneNow(c, 360, 0.12, "square", 0.18);
      window.setTimeout(() => {
        const live = ac();
        if (live && !muted) toneNow(live, 540, 0.14, "triangle", 0.2);
      }, 70);
      break;
    case "whoosh":
      toneNow(c, 200, 0.22, "sine", 0.18, 520);
      break;
    case "toggle":
      toneNow(c, 400, 0.07, "triangle", 0.2, 640);
      break;
    case "unlock":
      toneNow(c, 320, 0.12, "triangle", 0.2, 640);
      window.setTimeout(() => {
        const live = ac();
        if (live && !muted) toneNow(live, 520, 0.14, "square", 0.16, 880);
      }, 90);
      window.setTimeout(() => {
        const live = ac();
        if (live && !muted) toneNow(live, 780, 0.18, "triangle", 0.18, 1170);
      }, 180);
      break;
    default:
      break;
  }
}

export function playSfx(kind: SfxKind) {
  muted = loadSfxMuted();
  if (muted) return;
  const c = ac();
  if (!c) return;
  if (c.state === "suspended") {
    void c.resume().then(() => {
      if (!muted && ctx) fire(kind, ctx);
    });
    return;
  }
  fire(kind, c);
}
