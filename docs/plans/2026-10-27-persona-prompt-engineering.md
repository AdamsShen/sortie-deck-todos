# 角色工坊：角色提示词（Persona）提示词工程重构

- 日期：2026-10-27
- 状态：待确认（未开始编码）
- 关联模块：角色工坊（Agent 角色工坊 / Agent Forge）、persona 模板、阶段提示词注入

## 一、需求背景

当前角色提示词是**一段自由文本**：

- `packages/templates/personas/*.md` 正文即 system prompt，例如
  `/Library/git-root/sortie-deck-todos/packages/templates/personas/product.md:10-13`
  只有 3 行 "You are a product manager…"，没有背景、技能、示例、约束。
- 角色工坊（`apps/web/src/App.tsx:4544-4596`）与 Forge 审核台
  （`apps/web/src/components/ForgeReview.tsx:300-338`）都只提供一个 persona 文本框。
- 大模型生成角色（`/Library/git-root/sortie-deck-todos/src/sortie_deck/agent_forge.py:28-40`）
  只要求 "persona: 2-6 short lines"，产出的角色同样没有结构、没有资深度表达。

目标：

1. 角色提示词统一按提示词工程的结构化范式重构：**角色 / 背景 / 技能 / 示例 / 约束**。
2. 所有角色统一为「10 年经验资深专家」人设（产品例：`你是拥有 10 年经验的资深产品专家`）。
3. 大模型后续自动生成的角色（forge / 建队缺角色）**必须产出同样结构**，并由自检规则把关。
4. 运行时注入链路保持不动：最终仍以单一 persona 字符串进入 system prompt。

## 二、业内调研

| 来源 | 关键结论 | 对本项目的取舍 |
|---|---|---|
| Anthropic《Prompting best practices》<https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/system-prompts> | ① 在 system prompt 里一句话设定角色即可显著收敛行为与语气；② 用 XML/具名标签按内容性质分段（`<instructions>`、`<context>`、`<example(s)>`、具名约束块）；③ 示例 3–5 个最佳，需相关、多样、结构化；④ 约束要写"为什么"，否则无法泛化；⑤ 强约束措辞（CRITICAL/MUST）在新型号上会过触发，应软化。 | 采用「五段式 + 每段语义标题」；约束写成带原因的可执行条目，避免全大写强命令；示例取 1–3 条（角色提示词不是 few-shot 分类任务，数量取 1–3）。 |
| CrewAI：`Agent(role=…, goal=…, backstory=…)` <https://docs.crewai.com/concepts/agents> | `backstory` 看似装饰，实为 system prompt 的一部分——"描述字段就是 prompt"；角色/目标/背景三元组是该框架最被验证的角色定义方式。 | 证明「背景」不是可选装饰，应作为独立段落进入 prompt。 |
| LangGraph / OpenAI Agents SDK 对比 <https://aitechconnect.in/tips/langgraph-vs-crewai-vs-openai-agents-sdk-2026> | 显式图框架只接受一段 `instructions`，结构化由开发者自行约定；换框架时结构化字段需编译为单串。 | **不把结构化字段塞进运行时 API**：结构化 spec 在写入/编辑时编译成 `persona` 字符串，图与执行器继续只读 `persona`，零侵入、可回滚。 |
| Prompt Engineering Guide《Prompt Elements》<https://www.promptingguide.ai/introduction/elements> | 通用元素：Role / Instruction / Context / Input / Output format。 | 五段式覆盖 Role（角色）、Context（背景）、Instruction+Skill、Examples、Constraints；输出格式已由 executor 的 `## Result` 契约承载，不重复。 |
| DeepSeek 官方提示词库 <https://api-docs.deepseek.com/zh-cn/prompt-library/> | 13 个职能模板：代码改写 / 代码解释 / 代码生成 / 内容分类 / 结构化输出 / 角色扮演（自定义人设）/ 角色扮演（情景续写）/ 散文写作 / 诗歌创作 / 文案大纲生成 / 宣传标语生成 / 模型提示词生成 / 中英翻译专家。内容型模板体例为「定位 → 能力 → 知识储备 → 使用说明（输入/输出）」；代码型模板要求「先解释问题与方案，再给改写/生成结果」；元提示词模板输出「# 角色名提示词 + Role / Skills / Workflow / Output Format / Constraints」的 Markdown 分段。 | **按职能选模板**（见 3.1.1）：决策写作型走定位/能力/知识储备体例；工程型强制「先方案后代码」；结构化产出型写死输出 schema；角色生成型按元提示词体例产出。据此在五段式上补 `workflow`（工作流）一段。 |

> 说明：DeepSeek 提示词库首页为前端渲染，静态抓取只能确认 13 个模板名称与分类，正文体例依据公开转载与站点摘要归纳，落地时按上述共性设计，不逐字照抄。

归纳出的共性：**角色一句话定身份 → 背景说明所处环境与上下游 → 技能列出可验证能力 → 工作流给出执行顺序 → 示例给出输入/期望输出 → 约束写明边界与失败处理**；结构化字段必须在调用前编译为单一字符串。

## 三、技术设计

### 3.1 统一 Persona 规范（六段式）

`packages/templates/personas/*.md` 正文改为固定二级标题（front-matter 契约不变）：

```markdown
## 角色
你是拥有 10 年经验的资深产品专家（Product Manager），长期负责 B 端办公与 AI 原生产品的需求定义。

## 背景
- 你所在的 AI Native 小队在执行办公迭代：上游是业务方口述诉求，下游是设计与工程。
- 你的 PRD 与验收标准会被设计、实现、测试三个阶段直接消费，必须可执行、可验收。
- 单阶段预算 15 分钟，信息不足时按最小可行假设推进并标注风险。

## 技能
- 需求澄清：从模糊提问中提炼目标、用户、场景与边界。
- PRD 撰写：目标 / 范围 / 非目标 / 用户故事 / 可量化验收标准。
- 取舍判断：区分 MVP 与后续迭代，明确写出非目标。

## 工作流
1. 读 brief 与上游产物，列出 3 条以内的关键未知。
2. 用最小可行假设补齐未知，并在 PRD 中标注「待确认」。
3. 写 prd.md：目标、范围、非目标、用户故事、验收标准。
4. 写 acceptance.json：每条验收标准可测量、可复现。

## 示例
### 示例 1：模糊需求 → 可验收 PRD
- 输入：「做一个能自动发周报的功能」
- 期望输出：补齐关键假设（读者 / 数据源 / 频率），给出 PRD 骨架与 5 条可量化验收标准。

## 约束
- 只写 prd.md / acceptance.json，不写实现代码。
- 不臆造用户未提供的事实；信息不足时列「待确认假设」，不反问答疑。
- 验收标准必须可测量（数字、阈值、可观察行为）。
```

- `## 工作流` 为第六段，来自 DeepSeek 提示词库的 Workflow / 使用说明体例；可为空，空则渲染时省略。
- 语言：正文用中文（用户示例为中文，产物以中文为主）；front-matter 的 `role_key/title/inputs/...` 保持英文键不变。

#### 3.1.1 不同职能 Agent 的写法映射（参考 DeepSeek 提示词库）

| Agent 职能 | 对应角色 | 参考模板 | 额外写作要求 |
|---|---|---|---|
| 决策 / 写作型 | product、design、deploy | 内容分类、文案大纲生成 | 「定位 → 能力 → 知识储备 → 输入输出说明」；技能写清知识储备（如设计规范、发布检查单） |
| 评测 / 裁决型 | qa | 内容分类（判定标准） | 判定标准前置，pass/fail 依据必须可观测；失败必须给出可执行的复现路径 |
| 工程实现型 | eng、eng_ios、eng_android、eng_web、eng_backend、eng_agent | 代码改写、代码解释、代码生成 | 工作流固定为「读产物 → 给 tech_design / 问题与方案 → 最小 diff 实现 → 自检（边界、构建、测试）」；约束要求「先说明再动手」 |
| 结构化产出型 | 任一需要 JSON 契约的角色 | 结构化输出 | 在约束中写死字段、类型、缺失值约定与「只输出约定格式」 |
| 角色 / 人设生成型 | agent_forge、prompt enhance | 模型提示词生成、角色扮演（自定义人设） | 元提示词体例：输出带 Markdown 标题的完整人设；人设需含身份、经历、语言风格、知识边界 |

### 3.2 数据模型：结构化 spec + 编译后的 persona 字符串

新增纯数据层 `/Library/git-root/sortie-deck-todos/src/sortie_deck/persona.py`（不依赖 orchestrator/graph/plugins，避免循环导入）：

```python
class PersonaExample(BaseModel):
    situation: str   # 输入 / 场景
    response: str    # 期望输出 / 做法

class PersonaSpec(BaseModel):
    role: str = ""                 # 一句话身份，需含 10 年经验
    background: str = ""           # 多行，- 开头
    skills: list[str] = []
    workflow: list[str] = []       # 执行顺序（参考 DeepSeek Workflow 体例）
    examples: list[PersonaExample] = []
    constraints: list[str] = []
```

函数：

| 函数 | 作用 |
|---|---|
| `render_persona(spec) -> str` | 按六段式渲染为 Markdown 字符串（写入 `persona` 的唯一出口） |
| `parse_persona(text) -> PersonaSpec` | 从既有文本容错解析（中英文标题别名；无标题时整段视为 `role`），用于兼容历史数据与自由文本编辑 |
| `persona_quality_issues(spec) -> list[str]` | 质量自检：role 非空且包含年限（"10 年"/"10年"/"十年"）、skills ≥ 3、examples ≥ 1、constraints ≥ 3；仅产出 issue，不阻断 |
| `persona_is_structured(text) -> bool` | 是否已含六段标题（UI 判断是否可切结构化编辑） |

`persona` 字段仍是**唯一运行时来源**：`graph.py:1330`、`executor.py:812`、`plugins/coding.py:388`、`room_llm.py:24` 全部不动。

### 3.3 内置人设重写（10 个文件）

`packages/templates/personas/`：`product / design / eng / eng_ios / eng_android / eng_web / eng_backend / eng_agent / qa / deploy`

每个文件：front-matter 原样保留 → 按六段式重写正文，统一「10 年经验资深专家」句式，示例取该角色真实上游产物 → 产物的转换场景，并按 3.1.1 的职能映射调整侧重（工程角色必须含「先方案后代码」的工作流）。

`templates.py` 增加：
- `load_persona_spec(role) -> PersonaSpec`（读文件 → `parse_persona`）
- `compose_persona(role, *, role_line: str = "") -> str`（底座 spec + 覆盖角色首句 → 渲染，用于 `_SEED` 的专家变体）

### 3.4 已发布 Agent / Forge 草稿支持

`agents.py`：

- `PublishedAgent` 增加 `persona_spec: PersonaSpec | None = None`（旧 `data/agents.json` 无此字段可正常加载）。
- `CreateAgentRequest / UpdateAgentRequest` 增加 `persona_spec`；`persona` 改为可选，规则：
  - 传 `persona_spec` → `render_persona` 后写入 `persona`（spec 为源）；
  - 只传 `persona` → 原样保存，spec 置 `None`（自由文本模式）；
  - 两者都空 → 400。
- 读取时若无 spec，由 API 层用 `parse_persona(persona)` 补一份返回（不落库），保证前端结构化视图始终有内容。
- `_SEED`（`agents.py:93-198`）改为 `load_persona_spec(slot)` + 覆盖 `role` 首句，去掉英文前缀拼接。

`agent_forge.py`：

- `_FORGE_SYSTEM` 重写为要求输出结构化 persona 对象，并内置 3.1.1 的职能映射（按 capability 判断属于决策写作 / 工程实现 / 结构化产出 / 人设生成，对应调整侧重）：
  ```json
  {"persona": {"role": "...", "background": "...", "skills": ["..."],
               "workflow": ["..."],
               "examples": [{"situation": "...", "response": "..."}],
               "constraints": ["..."]}}
  ```
  并在 system prompt 内固化：role 必须以「你是拥有 10 年经验的资深<领域>专家」开头、skills 4-6 条、workflow 3-5 步、examples 1-3 条、constraints 4-6 条、语言与 capability 一致。
- `_draft_from_llm` 兼容两种返回：`persona` 为 dict → `PersonaSpec` → `render_persona`；为 str → `parse_persona` 兜底。
- `_TEMPLATE_PERSONA` 替换为 `_TEMPLATE_SPEC`（模板降级也产出六段式，保证 LLM 不可用时结构一致）。
- `ForgedAgentDraft` 增加 `persona_spec`；`update()` 允许改 `persona_spec`（改动后重编译 persona）。
- `_self_check()` 追加 `persona_quality_issues()` 结果，问题显示在审核台 03 SELF-CHECK。
- `approve()` 发布时透传 `persona_spec`。

### 3.5 提示词增强（prompt_enhance.py）

- `"persona"` 场景改写为「把任意人设文本重写为六段式」，明确要求 10 年经验、工作流分步、示例带输入/期望输出、约束带原因。
- 新增 `"persona_section"` 场景：`context` 传当前段名（角色/背景/技能/工作流/示例/约束），只优化该段、保持段格式，用于分段编辑器的单段「增强」按钮。
- `"forge"` 场景补充「资深专家（10 年经验）」定位。

### 3.6 前端

- `apps/web/src/types.ts`：新增 `PersonaSpecShape`、`PersonaExampleShape`；`PublishedAgent.persona_spec?`、`ForgeDraft.persona_spec?`。
- 新增共享组件 `apps/web/src/components/PersonaEditor.tsx`（App 工坊与 Forge 审核台复用）：
  - 「结构化 / 原文」模式切换；
  - 结构化：角色（input）、背景（textarea）、技能（每行一条）、工作流（每行一步）、示例（输入/期望输出成对，可增删）、约束（每行一条）；
  - 每段右上角「增强本段」按钮（scene=`persona_section`，context=段名）；
  - 原文模式：保留现有 textarea 与整体增强（scene=`persona`）。
- `App.tsx` 02 人设区、`ForgeReview.tsx` 草稿卡片替换为 `<PersonaEditor />`；保存时按当前模式只提交 `persona_spec` 或 `persona`。
- `i18n.ts`：新增 `personaRole/Background/Skills/Workflow/Examples/Constraints`、`personaModeStructured/Raw`、`personaAddExample` 等中英文案。

### 3.7 API

`api/app.py`：

- `ForgeProposeRequest` 不变；`ForgeUpdateRequest` 增加 `persona_spec: dict | None`。
- `CreateAgentRequest / UpdateAgentRequest` 增加 `persona_spec`。
- `GET /api/agents`、`GET /api/agents/{id}`、`GET /api/forge/drafts*` 返回体补 `persona_spec`（缺失时解析生成）。
- 不新增路由。

### 3.8 测试与文档

- 新增 `tests/test_persona.py`：render ↔ parse 往返、无标题文本兜底、质量自检（缺 10 年经验 / 技能不足 / 无示例）、全部内置 persona 可解析且质量通过。
- 更新 `tests/test_agent_contract.py:184`（`body.startswith("You are a release engineer")` → 新六段式断言）、新增六段完整性断言。
- `tests/test_roles_forge.py` 补：LLM 返回 dict persona 时草稿 `persona` 为渲染后的六段式、`persona_spec` 非空、质量 issue 能出现在 `issues`。
- 文档：`docs/zh/modules/agents.md`、`docs/zh/modules/collaboration.md`、`CHANGELOG.md` 同步说明六段式与 `persona_spec`。

### 3.9 改动文件清单

| 类型 | 文件 |
|---|---|
| 新增 | `src/sortie_deck/persona.py`、`tests/test_persona.py`、本计划 |
| 重写 | `packages/templates/personas/*.md`（10 个） |
| 修改 | `src/sortie_deck/templates.py`、`agents.py`、`agent_forge.py`、`prompt_enhance.py`、`api/app.py` |
| 前端 | `apps/web/src/types.ts`、`App.tsx`、`components/ForgeReview.tsx`、`i18n.ts` |
| 测试/文档 | `tests/test_agent_contract.py`、`tests/test_roles_forge.py`、`docs/zh/modules/agents.md`、`docs/zh/modules/collaboration.md`、`CHANGELOG.md` |

## 四、备选方案

| 方案 | 做法 | 取舍 |
|---|---|---|
| A（推荐） | spec + 编译成 persona 字符串，结构化字段仅存在于工坊数据层 | 运行时零改动、旧数据可读、可回滚；代价是 spec 与文本存在两份（以 spec→persona 单向编译约束一致性） |
| B | 只重写 10 个 persona 文案与 forge 提示词，不加字段 | 改动最小，但工坊无法分段编辑，质量无法自检，LLM 产出仍可能退化 |
| C | 运行时直接改读 `persona_spec`，删除 `persona` 字符串 | 最"干净"，但要改 graph/executor/plugins/room_llm 全部注入点并迁移 `data/*.json`，风险与工作量显著更高，收益不明显 |

## 五、风险与兼容

- 旧 `data/agents.json` / `data/forge.json` 无 `persona_spec` → pydantic 可选字段，读取不报错；API 返回时按需解析。
- 已发布角色文本不会自动变成五段式，只在用户进入结构化编辑并保存时编译；如需批量升级可后续加迁移脚本（不在本次范围）。
- 中文 persona 可能影响英文产物语气 → 可在约束中要求「产物语言跟随 brief / 上游产物语言」。
- 强约束措辞按 Anthropic 建议软化，避免新模型过触发。

## 六、状态

- [x] 需求澄清
- [x] 业内调研
- [x] 方案确认（用户补充 DeepSeek 提示词库作为参考）
- [x] 编码实现
- [x] 测试与文档（`pytest` 548 passed、`ruff` 本次改动文件通过、`npm run typecheck` 通过；`mkdocs build --strict` 仅剩 3 条既有 `TODO.md` 链接警告，与本次改动无关）
