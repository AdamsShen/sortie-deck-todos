import { useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { compilePersona, emptyPersonaExample, parsePersonaText } from "../persona";
import { SparkleIcon } from "./ChatComposer";
import type { Lang, PersonaSpecShape } from "../types";

export type PersonaMode = "structured" | "raw";

type Props = {
  token: string;
  lang: Lang;
  mode: PersonaMode;
  spec: PersonaSpecShape;
  raw: string;
  rows?: number;
  onModeChange: (mode: PersonaMode) => void;
  onSpecChange: (spec: PersonaSpecShape) => void;
  onRawChange: (value: string) => void;
  onError?: (message: string) => void;
};

function stripBullet(line: string): string {
  return line.replace(/^\s*(?:[-*•·]|\d+[.)、])\s*/, "").trim();
}

function linesOf(value: string): string[] {
  return value.split("\n").map(stripBullet).filter(Boolean);
}

/**
 * 角色人设编辑器：结构化（角色/背景/技能/工作流/示例/约束）与原文两种模式。
 *
 * 结构化模式的每段都可以单独调用「增强本段」，避免整篇重写覆盖其他段；
 * 原文模式保留整篇增强与撤销。模式切换时在本地做编译/解析，内容不丢失。
 */
export function PersonaEditor({
  token,
  lang,
  mode,
  spec,
  raw,
  rows = 6,
  onModeChange,
  onSpecChange,
  onRawChange,
  onError,
}: Props) {
  const [busy, setBusy] = useState("");
  const [rawPrev, setRawPrev] = useState<string | null>(null);

  function patch(partial: Partial<PersonaSpecShape>) {
    onSpecChange({ ...spec, ...partial });
  }

  async function enhance(
    text: string,
    scene: "persona" | "persona_section",
    context: string,
    apply: (value: string) => void
  ) {
    if (busy || !text.trim()) return;
    setBusy(context || scene);
    try {
      const data = await api<{ text: string }>("/api/prompts/enhance", {
        token,
        method: "POST",
        body: JSON.stringify({ scene, text, context }),
      });
      const next = (data.text || "").trim();
      if (next && next !== text) apply(next);
    } catch (err) {
      onError?.(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  function switchMode(next: PersonaMode) {
    if (next === mode) return;
    if (next === "raw") {
      onRawChange(compilePersona(spec));
    } else {
      onSpecChange(parsePersonaText(raw));
    }
    onModeChange(next);
  }

  function sectionEnhanceButton(
    key: string,
    label: string,
    text: string,
    apply: (value: string) => void
  ) {
    if (busy === key) {
      return <span className="mini-spinner" aria-hidden />;
    }
    return (
      <button
        type="button"
        className="btn ghost tiny"
        disabled={Boolean(busy) || !text.trim()}
        title={t(lang, "personaEnhanceSection")}
        aria-label={`${t(lang, "personaEnhanceSection")} · ${label}`}
        onClick={() => void enhance(text, "persona_section", label, apply)}
      >
        <SparkleIcon />
      </button>
    );
  }

  return (
    <div className="wr-persona-editor">
      <div className="wr-persona-modes" role="tablist" aria-label={t(lang, "agentPersona")}>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "structured"}
          className={`btn tiny ${mode === "structured" ? "primary" : "ghost"}`}
          onClick={() => switchMode("structured")}
        >
          {t(lang, "personaModeStructured")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "raw"}
          className={`btn tiny ${mode === "raw" ? "primary" : "ghost"}`}
          onClick={() => switchMode("raw")}
        >
          {t(lang, "personaModeRaw")}
        </button>
      </div>

      {mode === "structured" ? (
        <div className="wr-field-stack">
          <label className="wr-field">
            <span className="wr-persona-head">
              {t(lang, "personaRole")}
              {sectionEnhanceButton("role", t(lang, "personaRole"), spec.role, (v) =>
                patch({ role: v.replace(/\s*\n\s*/g, " ").trim() })
              )}
            </span>
            <input value={spec.role} onChange={(e) => patch({ role: e.target.value })} />
          </label>

          <label className="wr-field">
            <span className="wr-persona-head">
              {t(lang, "personaBackground")}
              {sectionEnhanceButton("background", t(lang, "personaBackground"), spec.background, (v) =>
                patch({ background: linesOf(v).join("\n") })
              )}
            </span>
            <textarea
              rows={4}
              placeholder={t(lang, "personaPerLine")}
              value={spec.background}
              onChange={(e) => patch({ background: e.target.value })}
            />
          </label>

          <label className="wr-field">
            <span className="wr-persona-head">
              {t(lang, "personaSkills")}
              {sectionEnhanceButton(
                "skills",
                t(lang, "personaSkills"),
                spec.skills.join("\n"),
                (v) => patch({ skills: linesOf(v) })
              )}
            </span>
            <textarea
              rows={4}
              placeholder={t(lang, "personaPerLine")}
              value={spec.skills.join("\n")}
              onChange={(e) => patch({ skills: e.target.value.split("\n") })}
            />
          </label>

          <label className="wr-field">
            <span className="wr-persona-head">
              {t(lang, "personaWorkflow")}
              {sectionEnhanceButton(
                "workflow",
                t(lang, "personaWorkflow"),
                spec.workflow.join("\n"),
                (v) => patch({ workflow: linesOf(v) })
              )}
            </span>
            <textarea
              rows={4}
              placeholder={t(lang, "personaStepPerLine")}
              value={spec.workflow.join("\n")}
              onChange={(e) => patch({ workflow: e.target.value.split("\n") })}
            />
          </label>

          <div className="wr-field">
            <span className="wr-persona-head">
              {t(lang, "personaExamples")}
              {sectionEnhanceButton(
                "examples",
                t(lang, "personaExamples"),
                spec.examples
                  .map((e) => `- 输入：${e.situation}\n- 期望输出：${e.response}`)
                  .join("\n"),
                (v) => {
                  const parsed = parsePersonaText(`## 示例\n${v}`).examples;
                  if (parsed.length) patch({ examples: parsed });
                }
              )}
            </span>
            {spec.examples.map((example, index) => (
              <div className="wr-persona-example" key={index}>
                <input
                  placeholder={t(lang, "personaExampleInput")}
                  value={example.situation}
                  onChange={(e) => {
                    const next = [...spec.examples];
                    next[index] = { ...example, situation: e.target.value };
                    patch({ examples: next });
                  }}
                />
                <input
                  placeholder={t(lang, "personaExampleOutput")}
                  value={example.response}
                  onChange={(e) => {
                    const next = [...spec.examples];
                    next[index] = { ...example, response: e.target.value };
                    patch({ examples: next });
                  }}
                />
                <button
                  type="button"
                  className="btn ghost tiny"
                  onClick={() =>
                    patch({ examples: spec.examples.filter((_, i) => i !== index) })
                  }
                >
                  {t(lang, "personaRemove")}
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn ghost tiny"
              onClick={() => patch({ examples: [...spec.examples, emptyPersonaExample()] })}
            >
              + {t(lang, "personaAddExample")}
            </button>
          </div>

          <label className="wr-field">
            <span className="wr-persona-head">
              {t(lang, "personaConstraints")}
              {sectionEnhanceButton(
                "constraints",
                t(lang, "personaConstraints"),
                spec.constraints.join("\n"),
                (v) => patch({ constraints: linesOf(v) })
              )}
            </span>
            <textarea
              rows={4}
              placeholder={t(lang, "personaPerLine")}
              value={spec.constraints.join("\n")}
              onChange={(e) => patch({ constraints: e.target.value.split("\n") })}
            />
          </label>
        </div>
      ) : (
        <div style={{ position: "relative" }}>
          {busy === "persona" ? (
            <span className="composer-enhance-toast" role="status">
              <span className="mini-spinner" aria-hidden />
              {t(lang, "enhanceLoading")}
            </span>
          ) : (
            <button
              type="button"
              className="btn ghost composer-enhance-btn composer-enhance-icon composer-enhance-corner"
              disabled={Boolean(busy) || !raw.trim()}
              onClick={() =>
                void enhance(raw, "persona", "persona", (value) => {
                  setRawPrev(raw);
                  onRawChange(value);
                })
              }
              title={t(lang, "enhancePrompt")}
              aria-label={t(lang, "enhancePrompt")}
            >
              <SparkleIcon />
            </button>
          )}
          {rawPrev !== null && busy !== "persona" && (
            <button
              type="button"
              className="btn ghost tiny composer-enhance-btn composer-enhance-corner"
              style={{ bottom: "2.8rem" }}
              onClick={() => {
                onRawChange(rawPrev);
                setRawPrev(null);
              }}
              title={t(lang, "enhanceUndo")}
              aria-label={t(lang, "enhanceUndo")}
            >
              ↺ {t(lang, "enhanceUndo")}
            </button>
          )}
          <textarea
            className="kb-body"
            rows={rows}
            value={raw}
            onChange={(e) => {
              onRawChange(e.target.value);
              if (rawPrev !== null) setRawPrev(null);
            }}
          />
        </div>
      )}
    </div>
  );
}
