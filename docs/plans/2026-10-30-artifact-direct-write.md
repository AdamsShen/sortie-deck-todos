# 契约产物直写 artifacts 目录（workspace 不再存放真文档）

日期：2026-10-30
状态：待确认（未动代码）
关联问题：`data/missions/ini_e9b14a29dc96/artifacts/product_prd/prd.md` 存的是 harness 回复包装，真 PRD 只在 `workspace/prd.md`，且 workspace 在任务终态被无条件删除。

## 1. 需求背景

现状：

- 所有 harness 阶段（product_prd / qa_cases / eng_*_tech_design / eng_implement / qa_verify）的契约产物由回复文本合成：
  ```971:997:src/sortie_deck/harness_providers/executor.py
  artifacts[name] = (
      f"# {name} — stage `{ctx.stage}` (harness `{harness}`)\n\n"
      f"{reply.text}\n\n## Log\n{log[:2000]}\n"
  )
  ```
- 真实文件写在 `data/missions/<id>/workspace/`，任务终态被整目录删除（```253:262:src/sortie_deck/git_publish.py```）。
- 结果：211 个任务只剩 7 个保留 workspace；其余任务的真 PRD/技术方案/用例已不可恢复，只剩过程日志。

目标：

1. 阶段运行过程中，契约文档直接落到 `artifacts/<stage>/`，任务档案里就是真内容；
2. `workspace/` 只保留代码工作副本（`repos/` clone、agent 写的源码/补丁），不承载真文档；
3. 任何 harness（claude_code / codex / cursor / opencode / mock）在"没写文件"或"写偏目录"时仍能兜底，不破坏阶段契约。

## 2. 业内调研

| 方案 | 做法 | 来源 |
|---|---|---|
| CrewAI | Task 声明 `output_file`，框架在 guardrail 通过后把最终输出写到指定路径（`create_directory` 控制自动建目录），agent 不直接管档案目录 | https://docs.crewai.com/en/concepts/tasks |
| LangGraph / 状态共享系 | 共享 State 传结构化结果，跨工作流产物交给 artifact store 统一管理，不依赖 agent 把文件写进某个约定目录 | https://zread.ai/mengdehong/ResearchCopilot/11-artifact-store-and-cross-workflow-state |
| 多 Agent 协作工程实践 | 每个 agent 的工作文件隔离，交接时传"带版本/带归属的产物"，避免共享目录互相覆盖 | https://nirvanalabs.io/blog/share-persistent-state-multiple-ai-agents |
| Claude Agent SDK | `ClaudeAgentOptions` 提供 `cwd` 与 `add_dirs`（额外可访问目录，逐项转成 CLI `--add-dir`） | https://docs.claude.com/en/api/agent-sdk/python |

共性结论：

- 主流做法都是「**编排层指定产物路径**」，而不是让 agent 在共享工作目录里自由发挥；
- 交付物与工作副本分离（一个归档、一个可回收）是共识；
- 但"完全相信 agent 会写到指定路径"没人这么干——都有框架侧的校验/落盘动作。

因此本项目的合理形态 = **直写（编排层指定 output dir）+ 收尾归档兜底**，而不是二选一。

## 3. 技术设计

### 3.1 目录职责

| 目录 | 内容 | 生命周期 |
|---|---|---|
| `artifacts/<stage>/` | 本阶段契约产物（prd.md、acceptance.json、tech_design.md、test_cases.md…） | 永久保留（任务档案） |
| `workspace/` | `repos/<name>/` clone、agent 写的源码/补丁/临时文件 | 编码阶段用，push 后清理 clone，终态整目录删除 |

### 3.2 直写（运行期，按阶段决定 cwd）

关键点：**extra_dirs 只有 claude_code / codex 支持，不能作为"实时可见"的主路径**。实时可见的前提是「写入位置本身就是档案目录」，因此按阶段切换 cwd：

| 阶段类型 | 阶段 | cwd | 契约文档落点 |
|---|---|---|---|
| 文档型 | `product_prd`、`qa_cases`、`design_ui`、`eng_*_tech_design` | `artifacts/<stage>/` | 直接写到 cwd，运行中即进档案 |
| 编码型 | `eng_implement`、`eng_backend/web/ios/android`、`qa_verify` | `workspace/` | extra_dirs（claude/codex）+ 收尾归档兜底 |

文档型阶段需要读仓库时（`eng_*_tech_design`）：在档案目录内建只读软链 `repos -> <workspace>/repos`，agent 按相对路径读，不写入；写操作只允许发生在 cwd（档案目录）。

配套：

1. `_run_harness()` 阶段开始时确保 `ctx.artifact_dir` 存在（`graph.py:1333` 已算出并注入 `StageContext.artifact_dir`，harness 路径当前未使用）。
2. `HarnessProvider.send(..., extra_dirs: list[str] | None = None)` 新增可选参数：`claude_code` 传 `ClaudeAgentOptions(add_dirs=...)`（本机 SDK 0.2.165 已确认含该字段）、`codex` 传 `--add-dir`，cursor/opencode 接受但忽略。
3. prompt 增加输出契约段（文档型阶段 cwd 就是档案目录，直接说明文件名）：
   ```
   Output dir (= cwd, contract artifacts MUST be written here): <artifact_dir>
   - 契约文件：<本阶段 REQUIRED 名单>，直接写在该目录根下
   - repos/ 为只读软链，不要在其中写入
   ```
4. 阶段开始记录「契约文件基线」：`{filename: (mtime, size)}`，写入 `meta`，用于 3.3 判断是否为本次新写。

### 3.3 收尾归档兜底（必须保留）

`_artifacts()` 改为三级优先：

1. **artifact_dir 里本次新写**的文件（mtime > 阶段开始时间，或内容与基线不同）；
2. **workspace 同名文件**（老行为，agent 不听话时）：读出来写进 artifact_dir，并记 `meta.artifact_source="workspace"`；
3. **回复包装**（最后兜底）：保留现有实现，但打 `meta.artifact_degraded=True` + 时间线事件，便于发现 harness 又在"只说不写"。

另外：

- 阶段重跑前，把 `artifacts/<stage>/` 里已有契约文件移到 `artifacts/<stage>/.runs/<timestamp>/`，避免旧文件让失败阶段通过契约校验；
- 只把契约名单 + `reply.md` / `consult_log.md` 纳入产物索引，agent 顺手写的笔记留在目录里但不进索引；
- `_salvage_stage_artifacts()`（超时/失败兜底）改为同时扫 artifact_dir 与 workspace，语义不变。

### 3.4 实时可见性（运行中就能看到真产物）

后端已有能力，缺的是"展示 + 触发"：

- `/api/initiatives/{id}/workspace` 同时返回 `artifact_files` 与 `worktree_files`（```159:169:src/sortie_deck/workspace.py```）；
- 前端已每 4s 轮询该接口（```915:923:apps/web/src/App.tsx```），但只渲染 `worktree_files`（```3473:3483:apps/web/src/App.tsx```）。

改动：

1. 档案面板同时渲染 `workspace.artifact_files`（按 `stage/name` 分组），点击复用现有 `openArtifact(path)` 预览（`App.tsx:1311-1317`）；
2. 阶段运行中（`status=running`）时把轮询间隔从 4s 降到 2s，并在阶段头上显示"本阶段产物实时刷新"；
3. 文档型阶段因为 cwd 就是档案目录，写入即被 `list_files` 扫到 → 运行中可见 `prd.md` 逐步增长；
4. 编码型阶段的契约文档（`implementation.md` / `tasks.json`）走 extra_dirs 或收尾归档，实时性由 `worktree_files` 列表覆盖（代码与文档都在 workspace 里可见）。

不做 fs watcher 的理由：需要额外常驻进程，且 agent 多为"直接覆写"而非"写临时文件再 rename"，watcher 会把半成品同步进档案，反而污染交付物。

### 3.5 兼容 / 迁移

- 已结束的 204 个任务无法回填（真内容已删，只能从 `reply.md` 人工恢复）；
- 现存的 7 个 workspace 可跑一次性迁移脚本：把契约文件复制进对应 `artifacts/<stage>/`，覆盖包装版本，并在原文件旁保留 `.from-workspace` 备份；
- 后续任务按新逻辑执行，不再需要迁移。

### 3.6 文件与改动范围

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/harness_providers/base.py` | `HarnessProvider.send` 增加 `extra_dirs` |
| `src/sortie_deck/harness_providers/executor.py` | 按阶段决定 cwd、建 artifact_dir、下传 extra_dirs、prompt 输出契约段、`_artifacts()` 三级解析 + 基线判断 |
| `src/sortie_deck/harness_providers/claude_code.py` | `add_dirs` |
| `src/sortie_deck/harness_providers/codex.py` | `--add-dir` |
| `src/sortie_deck/harness_providers/{cursor,opencode}.py` | 接受参数但忽略（注释说明） |
| `src/sortie_deck/plugins/coding.py` | `clone_repos` 支持把 `repos/` 软链进调用方指定目录（供文档型阶段只读） |
| `src/sortie_deck/graph.py` | 阶段开始建目录/移旧产物；`_salvage_stage_artifacts` 兼容 artifact_dir |
| `apps/web/src/App.tsx` | 档案面板渲染 `artifact_files`；运行中 2s 轮询 |
| `tests/test_stage_artifact_salvage.py` / 新增 `tests/test_stage_artifacts_resolution.py` | 三级优先级、旧文件隔离、JSON 产物、失败阶段不得复用旧文件、文档型阶段 cwd 断言 |

不涉及：`contracts.REQUIRED` 名单、`artifact_store` 写入接口、产物索引数据结构。

## 4. 备选方案

| 方案 | 取舍 |
|---|---|
| A. 仅收尾归档（从 workspace 复制进 artifacts） | 改动最小、零权限风险；但运行中看不到真产物，且依赖 workspace 存在 |
| B. 仅 extra_dirs 直写 | 实时性只覆盖 claude/codex；cursor/opencode 仍只能收尾归档 |
| C. **按阶段切 cwd（推荐）** | 文档型阶段写入即进档案，与 provider 无关；代价是 tech_design 需只读软链 `repos/`，需验证各 provider 是否跟随软链 |
| D. fs watcher 实时镜像 | 全 provider 覆盖；但暴露半成品、需要常驻进程，不推荐 |
| E. C + 收尾归档兜底（推荐组合） | 满足实时可见与档案保真，同时保留失败兜底 |

## 5. 状态

已实施（2026-10-30），采用方案 E。

### 5.1 实施记录

| 改动 | 说明 |
|---|---|
| `harness_providers/executor.py` | 文档型阶段 cwd 指向 `artifacts/<stage>/`；准备 `.upstream/` 上游镜像与 `workspace` 只读软链；`_artifacts()` 三级解析（产物目录 → workspace 归档 → 回复包装）并打 `artifact_sources` / `artifact_degraded` |
| `harness_providers/base.py` + 各 provider | `send(..., extra_dirs=...)`；claude_code 走 `add_dirs`、codex 走 `--add-dir`、cursor/opencode/mock 接受但忽略 |
| `graph.py` | `_stage_artifact_dir` 统一产物目录解析（S3 后端返回 None，退回旧行为）；失败/超时回收同时扫描产物目录 |
| `workspace.py` | 文件列表跳过 `.runs/.upstream` 等隐藏目录与软链 |
| `apps/web/src/App.tsx` + `i18n.ts` | 产物面板新增「产物目录（实时）」；运行中轮询 2s |
| `scripts/backfill_stage_artifacts.py` | 存量回填（默认 dry-run，`--apply` 覆盖并保留 `.from-workspace` 备份） |
| `src/sortie_deck/graph.py` + `layout.py` | 过程文件（`reply.md` / `consult_log.md` / `toolkit_meta.json` / `skill_runs.json` / `mcp_calls.json`）改落 `missions/<id>/logs/<stage>/`，产物目录只留交付文档，且不进下游 upstream 提示 |
| `scripts/move_process_artifacts.py` | 存量清理：过程文件迁入 `logs/`、`.runs` 迁为 `logs/<stage>/runs`、`.from-workspace` 迁入 `legacy/`，并清理产物索引中的过程/失效引用 |

### 5.2 存量回填结果

`--apply` 覆盖 13 个文件（3 个仍保留 workspace 的任务），其中 `ini_e9b14a29dc96`
（加一个会话接口）的 `product_prd/prd.md`、`qa_cases/test_cases.md` 等已从回复包装
替换为真实文档。其余 204 个任务 workspace 已删，无法恢复。

### 5.3 产物区 / 日志区划分

| 目录 | 内容 | 是否对用户展示 |
|---|---|---|
| `artifacts/<stage>/` | `prd.md`、`acceptance.json`、`tech_design.md`、`test_plan.md`、`test_cases.md`、`test_report.json`、`implementation.md`、`tasks.json`、`diff.patch` | 是 |
| `logs/<stage>/` | `reply.md`（阶段回复全文）、`consult_log.md`（上下游沟通）、`toolkit_meta.json` / `skill_runs.json` / `mcp_calls.json`（工具审计）、`legacy/` 回填备份、`runs/` 历史版本 | 否 |

存量清理结果：搬迁 80 项过程文件，清理 12 个任务的 63 条无效产物引用。

### 5.4 已知限制

- cursor-agent / opencode 没有额外目录参数：编码型阶段仍依赖收尾归档；
- 文档型阶段读仓库依赖 `workspace` 软链，provider 沙箱若不跟随软链，需要改为在阶段内镜像仓库；
- 全量 pytest 在本机有两处与本次改动无关的历史失败（`test_pipeline_hitl_and_qa_loop`、`test_knowledge_mission…auto_indexed` 在 HEAD 上同样失败，属 `.env` 里的 LLM/存储配置导致）。
