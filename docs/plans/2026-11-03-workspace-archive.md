# 方案：未绑定仓库任务的代码归档到 artifacts（zip）

- 日期：2026-11-03（v2，按用户反馈修订）
- 状态：待确认
- 需求：
  1. **只处理未绑定 git 仓库的任务**；绑定了仓库的任务不归档（代码在远端 git）。
  2. 未配置仓库时，agent 编写的代码直接写进 `workspace/repos/` 目录（与绑定仓库的 clone 位置一致）。
  3. 任务完成（终态）时把 `workspace/repos/` 打包压缩到 `artifacts/code-archive/`。
  4. 前端可点击下载。
  5. 单包大小上限 `max_mb` 默认 **500MB**。

## 一、需求背景（现状链路）

- 沙盒位置：`data/missions/<initiative_id>/workspace/`，由 `ensure_worktree` 创建（`src/sortie_deck/plugins/coding.py:467`），git worktree 语义已移除。
- **绑定仓库**：编码阶段 `clone_repos` 把业务仓库 clone 到 `workspace/repos/<name>/`（`src/sortie_deck/plugins/coding.py:363`），agent 在任务分支 `sortie/<id>` 上迭代，push 后建 MR——代码持久化在远端 git，无需归档。
- **未绑定仓库**：当前 prompt 明确指引 agent "把实现写在 workspace 根"（`src/sortie_deck/plugins/coding.py:558`），代码散落沙盒根；终态或 TTL 回收时整个 workspace 被删，**代码丢失**。
- 回收点（会删 workspace 的 4 处）：
  1. 终态 DONE：`src/sortie_deck/orchestrator.py:2030` → `_cleanup_mission_workspace`
  2. 终态 STOPPED：`src/sortie_deck/orchestrator.py:2034` → `_cleanup_mission_workspace`
  3. 用户手动停止：`src/sortie_deck/orchestrator.py:2492` → `_cleanup_mission_workspace`
  4. TTL sweep 每小时回收非活跃沙盒：`git_publish.sweep_worktrees`（`src/sortie_deck/git_publish.py:340` 直接调 `cleanup_workspace`）
- `delete_initiative`（`src/sortie_deck/orchestrator.py:1680`）删整个任务目录，artifacts 一起没了，归档无意义 → 不处理。
- workspace 实测构成（本地 15 个现存样本）：13 个只有 README（空沙盒）；2 个绑定仓库任务为 repos/ clone（6712 文件 / 118MB）+ 根下若干阶段文档。未绑定任务跑完编码阶段后 workspace 被清理，故本地无留存样本。

## 二、业内调研

| 产品 | 做法 | 来源 |
|---|---|---|
| OpenHands | 提供 `GET /api/conversations/{id}/zip-directory` 把整个 workspace 打包 zip 下载；沙箱销毁后转只读归档视图 | https://docs.openhands.org.cn/api-reference/download-workspace-archive ；https://opendeep.wiki/All-Hands-AI/OpenHands/conversation-and-agent-execution.export-archive-and-sharing |
| Devin (Cognition) | 云 Devbox 本身 ephemeral，持久化靠 git 远端与云端会话档案，文件靠手动导出 | https://fast.io/resources/devin-ai-tool-file-workflows |
| Cursor Cloud Agents | 产出强制 PR 化（绑定 git 分支），完成后自动开 PR，不依赖沙盒留存 | https://cursor.com/docs/ai-features/background-agents |

共性结论：**优先 PR 化（绑定 git），未绑定时以 workspace zip 导出作为兜底持久化**。本项目绑定仓库已走 PR 化，本方案只补齐未绑定场景，与 OpenHands 模式一致。

## 三、拟采用设计

### 1. 未绑定任务的代码目录约定（prompt 指引）

实现收敛在 `clone_repos` 的空分支（`src/sortie_deck/plugins/coding.py`）：`ctx.repos` 为空时预创建 `workspace/repos/` 并返回 prompt 指引段（"产出代码统一写入 repos/，任务结束归档，写在外面会丢"）。`repo_section` 是三条执行路径共用的一段 prompt（harness 主路径 `harness_providers/executor.py`、ClaudeCodeCli 与 CursorCli 插件），因此无需逐 provider 改 prompt 组装点。

- **同步修正两处受影响逻辑**：
  - `_count_impl_files`（`src/sortie_deck/plugins/coding.py`）：新增 `has_repos` 参数——绑定仓库时维持现状排除 repos（业务 clone）；未绑定时 repos/ 是约定产出目录，必须计入，否则触发 "exit=0 但没写代码" 误判。调用点（coding.py 两处 + executor.py 两处）均传 `has_repos=bool(ctx.repos)`。
  - `_git_diff`：无 git 分支的文件清单用 `workspace.rglob`，天然包含 repos/ 产出，无需改动。
- 防不听话兜底：agent 若仍把文件写在 workspace 根，归档时一并打包（见 §2 打包内容第 2 条）。

### 2. 归档函数（核心，新增）

`src/sortie_deck/workspace.py` 新增 `archive_repos_snapshot(workspace_dir, artifacts_dir, *, max_mb) -> dict | None`：

- **绑定与否判定**：`workspace/repos/` 下存在含 `.git` 的子目录（clone 特征 `repos/<name>/.git`）→ 视为绑定仓库任务，跳过归档（`skipped_reason="bound_repo"`）。该判定对终态与 TTL sweep 两条路径统一适用，且语义本质：repos 下的内容是 git clone（代码在远端）就不归档；不是 clone（agent 直接写入的产出）才归档。不依赖 ini 元数据（sweep 拿不到任务对象）。
- 打包目标：`artifacts/code-archive/code-archive-<UTC yyyyMMdd-HHmmss>.zip`（artifacts 下新建 `code-archive/` 子目录；不占用 stage 命名空间）。
- 打包内容（按序判定）：
  1. `workspace/repos/` 存在且排除后仍有有效文件 → 打包整个 `repos/`（zip 内路径保留 `repos/` 前缀，如 `repos/app.py`）；
  2. 否则 workspace 根有散落有效文件（agent 不听话兜底）→ 打包根目录有效文件；
  3. 两者皆无 → **空跳过**（不产 zip、不写 manifest）。
- 排除清单（对任何路径段生效，与 `_count_impl_files` / `list_files` 对齐）：
  - `.git` `node_modules` `__pycache__` `.venv` `venv` `dist` `build` `.next` `.turbo` `.cache`
  - 注入产物：`.tdt/`（MCP 配置）、`.cursor/`、`.sortie-deck/`（知识包）
  - 沙盒说明 `README.md`（仅根目录那份）、软链与隐藏文件（`.` 开头）
- 大小防护：逐文件累计超过 `max_mb`（默认 **500MB**）即停止添加，manifest 标记 `truncated: true`。
- manifest：同目录写 `manifest.json`（initiative_id、归档时间、文件数、字节数、zip 文件名、truncated、打包来源 repos/root），每次归档覆盖更新。
- 幂等：fingerprint（文件数 + 总字节 + 最大 mtime）写 `artifacts/code-archive/.last-archive.json`，与上次一致则跳过（sweep 与终态重复触发不重复打包）；zip 缺失时强制重打。

### 3. 配置项

`src/sortie_deck/settings.py` 新增（env：`TDT_ARCHIVE_WORKSPACE_ON_CLEANUP` / `TDT_ARCHIVE_WORKSPACE_MAX_MB`）：

- `archive_workspace_on_cleanup: bool = True`
- `archive_workspace_max_mb: int = 500`

### 4. 回收点接入

- `orchestrator._cleanup_mission_workspace`（`src/sortie_deck/orchestrator.py:1575`）：`cleanup_workspace` 之前调用归档；归档失败只 warn，不阻断清理与状态流转（沿用现有 try/except 模式）。绑定与否由归档函数内部按 `.git` 特征判定，orchestrator 无需传元数据。
- `git_publish.sweep_worktrees`（`src/sortie_deck/git_publish.py:340`）：TTL 回收前同样归档（artifacts 目录 = 沙盒目录的 `../artifacts`）。

### 5. 下载能力（前端可见可下载）

- 后端 `read_artifact`（`src/sortie_deck/api/app.py:2633`）目前只 `read_text`，读 zip 会 UnicodeDecodeError：增加二进制检测，命中时返回 `FileResponse`（`Content-Disposition: attachment`，浏览器直接下载）。
- 前端 `openArtifact`（`apps/web/src/App.tsx:1488`）：`.zip` 文件改为触发浏览器下载（fetch blob → `<a download>`，复用现有 token 传递），不再走文本预览弹窗；`i18n.ts` 补文案。

### 6. 与既有机制的关系（不破坏）

- `mission_workspace` 的 `artifact_files` 由 `list_files(art_dir)` 生成（`src/sortie_deck/workspace.py:219`），zip 放 `artifacts/code-archive/` 会自然出现在前端"实时产物"列表，无需改布局代码。
- `_index_mission_artifacts` 只索引 prd/tech_design 等 md（`src/sortie_deck/orchestrator.py:674`），zip 不会进知识库。
- `_sync_artifacts_from_upstream` 只登记 stage 产物引用，zip 不参与，不会污染 stage 产物区。
- `git_publish.sweep_worktrees` 的 dirty 判定（`src/sortie_deck/git_publish.py:265`）按 `repos/*/.git` 找 clone——未绑定任务产出直接写 `repos/` 根、无 clone 子目录，不影响 TTL 判定。
- 绑定仓库任务的 `publish_repos` push 流程、`cleanup_repos`（push 后删 clone）均不受影响。
- S3 artifacts backend 部署下 `artifact_files` 视图本就是本地目录视角，zip 直接写本地目录即可被列出（与现状一致，本次不扩展 S3 二进制接口）。

## 四、文件清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/plugins/coding.py` | `clone_repos` 未绑定分支（预创建 repos/ + prompt 指引）+ `_count_impl_files` 增加 `has_repos` 语义 |
| `src/sortie_deck/harness_providers/executor.py` | `_count_impl_files` 调用点传绑定标记 |
| `src/sortie_deck/workspace.py` | 新增 `archive_repos_snapshot`（绑定判定 + zip 打包 + 排除 + 上限 + manifest + 幂等 + 空跳过 + 根目录兜底） |
| `src/sortie_deck/settings.py` | 新增 2 个配置项（max_mb 默认 500） |
| `src/sortie_deck/orchestrator.py` | `_cleanup_mission_workspace` 清理前归档；`_sweep_worktrees_once` 传绑定任务集合 |
| `src/sortie_deck/git_publish.py` | `sweep_worktrees` 回收前归档（bound_ids / archive_max_mb 参数） |
| `src/sortie_deck/api/app.py` | `read_artifact` 捕获 UnicodeDecodeError 转 `FileResponse` 附件下载 |
| `apps/web/src/api.ts` / `App.tsx` | `downloadMissionArtifact` 二进制下载 + `openArtifact` zip 分支 |
| `tests/test_workspace.py` | 归档单测：绑定跳过、排除规则、上限截断、幂等、空沙盒跳过、根目录兜底、无 workspace no-op |
| `tests/test_coding_branching.py` | clone_repos 未绑定分支 + `_count_impl_files` 语义测试 |

## 五、备选方案

1. **统一归档（含绑定仓库任务）**：初版方案曾采用；用户已明确否决——绑定任务代码在远端 git，归档 clone 无意义且浪费（实测单任务 repos/ 达 118MB）。改为按 `.git` clone 特征跳过。
2. **不打 zip，把 workspace 平移进 artifacts**：会混入 stage 产物视图且与 artifacts 语义（agent 交付物）冲突，放弃。
3. **沙盒改成不删除、仅 TTL 清理**：磁盘风险大，与"workspace 即一次性副本"的既有设计冲突，放弃。
4. **未绑定任务把 agent cwd 直接切到 `repos/`**：影响 executor 内 checklist/diff 等相对路径逻辑，风险大于收益，改用 prompt 指引（cwd 保持 workspace 根，与绑定仓库行为一致）。

## 六、状态

- [x] 用户确认方案
- [x] 实现代码（2026-11-03）
- [x] 测试（tests/test_workspace.py 9 项归档测试 + tests/test_coding_branching.py 2 项，相关回归 86+35 通过；前端 tsc 通过）
