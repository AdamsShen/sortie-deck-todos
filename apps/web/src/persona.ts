import type { PersonaExampleShape, PersonaSpecShape } from "./types";

/**
 * 人设（persona）的前端编译/解析工具。
 *
 * 后端 `sortie_deck/persona.py` 是唯一权威实现；这里只用于编辑器的模式切换与
 * 本地预览，保证「结构化 ↔ 原文」切换时内容不丢失。
 */

const SECTION_ALIASES: Record<string, keyof PersonaSpecShape> = {
  角色: "role",
  定位: "role",
  身份: "role",
  role: "role",
  背景: "background",
  上下文: "background",
  background: "background",
  backstory: "background",
  context: "background",
  技能: "skills",
  能力: "skills",
  知识储备: "skills",
  skills: "skills",
  capabilities: "skills",
  工作流: "workflow",
  流程: "workflow",
  执行步骤: "workflow",
  使用说明: "workflow",
  workflow: "workflow",
  示例: "examples",
  例子: "examples",
  examples: "examples",
  example: "examples",
  约束: "constraints",
  限制: "constraints",
  禁忌: "constraints",
  边界: "constraints",
  constraints: "constraints",
  rules: "constraints",
};

export function emptyPersonaSpec(): PersonaSpecShape {
  return {
    role: "",
    background: "",
    skills: [],
    workflow: [],
    examples: [],
    constraints: [],
  };
}

export function emptyPersonaExample(): PersonaExampleShape {
  return { situation: "", response: "" };
}

function stripBullet(line: string): string {
  return line.replace(/^\s*(?:[-*•·]|\d+[.)、])\s*/, "").trim();
}

function dedupe(items: string[]): string[] {
  return Array.from(new Set(items.map((s) => s.trim()).filter(Boolean)));
}

/** 把结构化人设编译为 Markdown 提示词（与后端 render_persona 对齐）。 */
export function compilePersona(spec: PersonaSpecShape): string {
  const sections: string[] = [];

  const role = spec.role.trim();
  if (role) sections.push(`## 角色\n${role}`);

  const background = spec.background
    .split("\n")
    .map(stripBullet)
    .filter(Boolean);
  if (background.length) {
    sections.push(`## 背景\n${background.map((line) => `- ${line}`).join("\n")}`);
  }

  const skills = dedupe(spec.skills);
  if (skills.length) sections.push(`## 技能\n${skills.map((s) => `- ${s}`).join("\n")}`);

  const workflow = dedupe(spec.workflow);
  if (workflow.length) {
    sections.push(`## 工作流\n${workflow.map((s, i) => `${i + 1}. ${s}`).join("\n")}`);
  }

  const examples = spec.examples.filter(
    (e) => e.situation.trim() || e.response.trim()
  );
  if (examples.length) {
    const blocks = examples.map((example, i) => {
      const lines = [`### 示例 ${i + 1}`];
      if (example.situation.trim()) lines.push(`- 输入：${example.situation.trim()}`);
      if (example.response.trim()) lines.push(`- 期望输出：${example.response.trim()}`);
      return lines.join("\n");
    });
    sections.push(`## 示例\n${blocks.join("\n\n")}`);
  }

  const constraints = dedupe(spec.constraints);
  if (constraints.length) {
    sections.push(`## 约束\n${constraints.map((c) => `- ${c}`).join("\n")}`);
  }

  return sections.join("\n\n").trim();
}

/** 从任意人设文本解析出结构化字段；识别不到标题时整段视为「角色」。 */
export function parsePersonaText(text: string): PersonaSpecShape {
  const spec = emptyPersonaSpec();
  const raw = (text || "").trim();
  if (!raw) return spec;

  const buckets: Record<string, string[]> = {};
  let current: keyof PersonaSpecShape = "role";
  let matched = false;

  for (const line of raw.split("\n")) {
    const m = /^\s{0,3}##\s+(.+?)\s*$/.exec(line);
    const key = m ? SECTION_ALIASES[m[1].replace(/[\s_]/g, "").toLowerCase()] : undefined;
    if (key) {
      current = key;
      matched = true;
      buckets[key] = buckets[key] ?? [];
      continue;
    }
    buckets[current] = buckets[current] ?? [];
    buckets[current].push(line);
  }

  if (!matched) {
    spec.role = raw;
    return spec;
  }

  spec.role = (buckets.role ?? []).join("\n").trim();
  spec.background = (buckets.background ?? [])
    .map(stripBullet)
    .filter(Boolean)
    .join("\n");
  spec.skills = dedupe((buckets.skills ?? []).map(stripBullet));
  spec.workflow = dedupe((buckets.workflow ?? []).map(stripBullet));
  spec.constraints = dedupe((buckets.constraints ?? []).map(stripBullet));

  const exampleLines = buckets.examples ?? [];
  const blocks: string[][] = [];
  let currentBlock: string[] = [];
  for (const line of exampleLines) {
    if (/^\s{0,3}###\s+/.test(line)) {
      if (currentBlock.length) blocks.push(currentBlock);
      currentBlock = [];
      continue;
    }
    currentBlock.push(line);
  }
  if (currentBlock.length) blocks.push(currentBlock);

  for (const lines of blocks) {
    let situation = "";
    let response = "";
    let target: "situation" | "response" | null = null;
    for (const line of lines) {
      const input = /^\s*(?:[-*•·]\s*)?(?:输入|场景|情况|input)\s*[：:]\s*(.*)$/i.exec(line);
      const output =
        /^\s*(?:[-*•·]\s*)?(?:期望输出|输出|应做|做法|output|expected)\s*[：:]\s*(.*)$/i.exec(
          line
        );
      if (input) {
        target = "situation";
        situation = input[1].trim();
      } else if (output) {
        target = "response";
        response = output[1].trim();
      } else if (line.trim() && target === "situation") {
        situation = `${situation} ${stripBullet(line)}`.trim();
      } else if (line.trim() && target === "response") {
        response = `${response} ${stripBullet(line)}`.trim();
      }
    }
    if (situation || response) spec.examples.push({ situation, response });
  }

  return spec;
}
