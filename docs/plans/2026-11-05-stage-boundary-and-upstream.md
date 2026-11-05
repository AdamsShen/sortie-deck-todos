# 阶段边界与上游可读性修复（2026-11-05）

## 1. 背景与问题

任务 `ini_937080976c38`（「帮我写一个可运行贪吃蛇小游戏，支持用户登陆注册」）暴露了三处阶段边界缺陷：

| # | 现象 | 证据 |
|---|---|---|
| 1 | 编码阶段读不到正式上游 | `eng_implement` 会话里 `Read artifacts/product_prd/prd.md` 返回 `is_error`「requested permissions to read ... haven't granted it yet」 |
| 2 | 旧版草稿盖住正式版 | `workspace/prd.md` 是 intake 留下的 v1.0（43 条），正式版是 `artifacts/product_prd/prd.md` v1.1（54 条）；`implementation.md` 自述「依据 prd.md v1.0（43 条）」，54 条里 18 条未出现（含 B-8） |
| 3 | intake 越界生产 PRD | intake 产出 `prd.md` 24,645B + `acceptance.json` 16,969B（43 条），20 轮全部用于工具调用、0 字符文本，最后因轮次上限 fail |

根因是两处结构性缺口叠加：

- **契约表只登记「需要机器判定」的产物**：`contracts.REQUIRED` 没有 intake 项，`_contract_names("intake")` 返回空，prompt 退化成含糊的「write the contract files there too」，模型只能自己去找规格；
- **上游镜像只服务文档阶段**：`_prepare_stage_output()` 把上游复制进 `artifact_dir/.upstream/`，但非文档阶段 `cwd=workspace`、`extra_dirs=[artifact_dir]`，上游目录既没镜像也没授权，agent 只能读 workspace 里的遗留文件。

## 2. 业内做法调研（结论摘要）

- **LangGraph / CrewAI / AutoGen**：阶段间靠显式 state 传参（文件路径 + 摘要），不依赖 agent 自行到磁盘上「找文件」；产物落盘位置由编排层决定，agent 只拿得到「可读引用」。
- **OpenAI Agents SDK / MetaGPT**：每个角色有固定 `output schema`，缺产物即判失败，不让上一步的半成品以同名文件形式流到下一边。
- **共同模式**：① 每阶段声明唯一产出清单；② 上游以「只读副本 + 摘要」注入；③ 阶段目录隔离，跨阶段同名文件不共处一地。

本项目的问题正是这三条都没有完全落地（契约表缺 intake、上游未镜像、workspace 共用地）。

## 3. 方案与改动清单

### 3.1 上游可读性（最要紧）

- 抽出 `_mirror_upstream(target_dir, upstream)`（`src/sortie_deck/harness_providers/executor.py`），文档阶段与编码阶段共用；
- 非文档阶段新增：镜像到 `workspace/.upstream/`，并 `ctx = ctx.model_copy(update={"upstream_paths": local_upstream})`，prompt 里的路径直接指向镜像文件（provider 无关，claude/codex/opencode/cursor 全部生效）；
- prompt 明确「`.upstream/` 是唯一权威上游；workspace 其他位置的同名文件是遗留物，禁止作为依据」；
- `plugins/coding.py`：`_count_impl_files` 的 skip 集合加 `.upstream` / `.archive`；`_git_diff` 无 git 分支排除这两个目录，避免污染 `files_written` 与 `diff.patch`。

### 3.2 清掉跨阶段遗留

- 新增 `_sweep_stale_contracts(workspace, stage, artifact_dir)`：待清理集合 = `REQUIRED` 全部文件名并集 − 本阶段契约名；只扫 workspace 根层（跳过 `.upstream` / `.archive` / 不递归 `repos/`），命中移到 `workspace/.archive/<stage>-<时间戳>/`；
- 文档阶段在 `_prepare_stage_output` 之后调用（草稿先镜像进 `.upstream`，不丢审校入口）；非文档阶段在镜像前调用；
- `meta["stale_moved"]` 记录，graph 侧追加一条时间线 `info` 事件，便于追溯「这阶段为什么没读到旧版」。

### 3.3 intake 轻契约

- `contracts.REQUIRED` 新增 `"intake": ["intake.md"]`；
- `_DOC_STAGE_NAMES` 加入 `intake`：cwd 直接是 `artifacts/intake`，workspace 只作只读输入，从源头消灭 `workspace/prd.md`；
- 新增 `_INTAKE_SPEC` 提示词（仅 intake 注入）：固定骨架（需求复述 ≤10 行 / 范围内外 / 关键假设 / 待确认 / 交接要点），明确禁止写 `prd.md`、`acceptance.json`、设计/用例，要求先落盘再补细节、10 轮内收尾；
- 四个航线模板（`standard/default/express/research`）与 `pipeline.py` 的 intake catalog 补 `output_contract: ["intake.md"]`；
- **硬失败**：新增 `settings.strict_contract_stages = "intake"`，缺 `intake.md` 时阶段直接 fail（不再用回复包装冒充产物）；其余阶段保留历史降级行为，避免破坏 mock harness 兼容性。

### 3.4 边界与提速（按用户决定收敛）

- 新增 `_SCOPE_GUARD` 提示词（所有阶段）：只写本阶段契约、禁写他阶段契约名、workspace 为空时禁反复遍历、禁读宿主仓库与 `~/.claude`、禁加载无关 skill、必须 `## Result`；
- `stage_max_turns` 保持 `intake=20`（不下调）；
- **不做软成功**：撞轮次上限一律 fail，`claude_code.py` 的异常映射语义不变。

## 4. 备选方案

| 方案 | 取舍 |
|---|---|
| 只给 claude 的 `add_dirs` 加上游目录 | 改动更小，但只覆盖 Claude，codex/opencode 仍读不到；且 `add_dirs` 同时授予写权限 |
| intake 保持无契约 + prompt 声明「不产文件」 | 少了契约校验，无法阻止模型再次脑补 PRD，也无法回收产物 |
| 全局硬失败（所有阶段缺件即 fail） | 边界最紧，但 mock harness 与历史任务依赖「回复包装」兼容，回归测试出现 3 处既存用法失败，故收敛为 `strict_contract_stages` 白名单 |

## 5. 验证

- 新增 `tests/test_stage_boundary.py`（8 例）：上游镜像 / `.upstream` 不计入实现文件 / diff 排除 / 遗留清理 / 保留本阶段产物 / intake 唯一契约 / intake 提示词禁写 PRD / 缺件硬失败；
- 全量回归：`pytest tests/ -q --ignore=tests/test_llm_chain.py` → **803 passed**；
- 编排链路：`scripts/verify_collab.py --harness mock --scenario all` → 5 passed / 3 failed，其中 C1/C3/C5 在改动前的 HEAD 上同样失败（已用 `git worktree` 对照验证），非本次回归；
- 剧本化 provider 现在按 `REQUIRED` 写占位契约文件，避免「编排能力通过但 intake 契约失败」的误判。

## 6. 状态

- 已实现并自测通过，尚未提交 commit、未部署云端。
- 正在运行的任务 `ini_937080976c38` 不做数据迁移；改动对新任务生效。
