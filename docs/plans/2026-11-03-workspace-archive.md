# 方案：任务沙盒回收前将代码归档到 artifacts（zip）

- 日期：2026-11-03
- 状态：待确认
- 需求：未绑定 git 仓库的任务，开发代码只存在 `data/missions/<id>/workspace/`（一次性沙盒），任务终态（DONE/STOPPED）或 TTL 回收时整个目录被删，代码丢失。要求沙盒回收前把编写的代码打包成压缩包归档到 artifacts 目录下，前端可查看/下载。

## 一、需求背景（现状链路）

- 沙盒位置：`data/missions/<initiative_id>/workspace/`，由 `ensure_worktree` 创建（`src/sortie_deck/plugins/coding.py:467`），git worktree 语义已移除，未绑定仓库时 agent 直接在沙盒根写代码。
- 回收点（共 3 处会删 workspace，另 1 处不涉及）：
  1. 终态 DONE：`src/sortie_deck/orchestrator.py:2030` → `_cleanup_mission_workspace`
  2. 终态 STOPPED：`src/sortie_deck/orchestrator.py:2034` → `_cleanup_mission_workspace`
  3. 用户手动停止：`src/sortie_deck/orchestrator.py:2492` → `_cleanup_mission_workspace`
  4. TTL sweep 每小时回收非活跃沙盒：`git_publish.sweep_worktrees`（`src/sortie_deck/git_publish.py:340` 直接调 `cleanup_workspace`）
- `delete_initiative`（`src/sortie_deck/orchestrator.py:1680`）删整个任务目录，artifacts 一起没了，归档无意义 → 不处理。
- 绑定仓库的任务代码在 `workspace/repos/<name>/`（已 push 到远端 `sortie/<id>` 分支），归档时排除 `repos/` 即可，对全部任务统一处理。

## 二、业内调研

| 产品 | 做法 | 来源 |
|---|---|---|
| OpenHands | 提供 `GET /api/conversations/{id}/zip-directory` 把整个 workspace 打包 zip 下载；沙箱销毁后转只读归档视图 | https://docs.openhands.org.cn/api-reference/download-workspace-archive ；https://opendeep.wiki/All-Hands-AI/OpenHands/conversation-and-agent-execution.export-archive-and-sharing |
| Devin (Cognition) | 云 Devbox 本身 ephemeral，持久化靠 git 远端与云端会话档案，文件靠手动导出 | https://fast.io/resources/devin-ai-tool-file-workflows |
| Cursor Cloud Agents | 产出强制 PR 化（绑定 git 分支），完成后自动开 PR，不依赖沙盒留存 | https://cursor.com/docs/ai-features/background-agents |

共性结论：**优先 PR 化（绑定 git），未绑定时以 workspace zip 导出作为兜底持久化**。本项目已有 PR 化路径（repos 绑定 + `sortie/<id>` 分支 + push），本方案补齐未绑定场景，与 OpenHands 模式一致。

## 三、拟采用设计

### 1. 归档函数（核心，新增）

`src/sortie_deck/workspace.py` 新增 `archive_workspace_snapshot(workspace_dir, artifacts_dir, *, max_mb) -> dict`：

- 打包目标：`artifacts/code-archive/workspace-<UTC yyyyMMdd-HHmmss>.zip`（artifacts 下新建 `code-archive/` 子目录；不占用 stage 命名空间）。
- 内容：workspace 下全部文件，排除（与现有 `_count_impl_files` / `list_files` 的 skip 集合对齐）：
  - `repos/`（业务仓库 clone，代码已 push 远端）
  - `README.md`（沙盒说明文件，非用户代码）、`manifest.json`
  - 依赖/构建目录：`node_modules` `__pycache__` `.venv` `venv` `dist` `build` `.next` `.turbo` `.cache` `.git`
  - 注入产物：`.tdt/`（MCP 配置）、`.cursor/`、`.sortie-deck/`（知识包）
  - 软链与隐藏文件（`.` 开头）
- 大小防护：逐文件累计超过 `max_mb`（默认 200MB）即停止添加，manifest 标记 `truncated: true`，防止 agent 乱下依赖把 artifacts 撑爆。
- manifest：同目录写 `manifest.json`（initiative_id、归档时间、文件数、字节数、zip 文件名、truncated），审计用；每次归档覆盖更新。
- 幂等：fingerprint（文件数 + 总字节 + 最大 mtime）写 `artifacts/code-archive/.last-archive.json`，与上次一致则跳过（sweep 与终态重复触发不重复打包）；zip 缺失时强制重打。
- 空沙盒跳过：排除 README 后无任何有效文件时直接返回（不产 zip、不写 manifest）。实测本地 15 个现存 workspace 中 13 个只有 README（未跑到编码阶段），无条件归档会制造一堆空包。

### 2. 配置项

`src/sortie_deck/settings.py` 新增（env：`TDT_ARCHIVE_WORKSPACE_ON_CLEANUP` / `TDT_ARCHIVE_WORKSPACE_MAX_MB`）：

- `archive_workspace_on_cleanup: bool = True`
- `archive_workspace_max_mb: int = 200`

### 3. 回收点接入

- `orchestrator._cleanup_mission_workspace`（`src/sortie_deck/orchestrator.py:1575`）：`cleanup_workspace` 之前调用归档；归档失败只 warn，不阻断清理与状态流转（沿用现有 try/except 模式）。
- `git_publish.sweep_worktrees`（`src/sortie_deck/git_publish.py:340`）：TTL 回收前同样归档（artifacts 目录 = 沙盒目录的 `../artifacts`）。

### 4. 下载能力（前端可见可下载）

- 后端 `read_artifact`（`src/sortie_deck/api/app.py:2633`）目前只 `read_text`，读 zip 会 UnicodeDecodeError：增加二进制检测，命中时返回 `FileResponse`（`Content-Disposition: attachment`，浏览器直接下载）。
- 前端 `openArtifact`（`apps/web/src/App.tsx:1488`）：`.zip` 文件改为触发浏览器下载（fetch blob → `<a download>`，复用现有 token 传递），不再走文本预览弹窗；`i18n.ts` 补文案。

### 5. 与既有机制的关系（不破坏）

- `mission_workspace` 的 `artifact_files` 由 `list_files(art_dir)` 生成（`src/sortie_deck/workspace.py:219`），zip 放 `artifacts/code-archive/` 会自然出现在前端"实时产物"列表，无需改布局代码。
- `_index_mission_artifacts` 只索引 prd/tech_design 等 md（`src/sortie_deck/orchestrator.py:674`），zip 不会进知识库。
- `_sync_artifacts_from_upstream` 只登记 stage 产物引用，zip 不参与，不会污染 stage 产物区。
- S3 artifacts backend 部署下 `artifact_files` 视图本就是本地目录视角，zip 直接写本地目录即可被列出（与现状一致，本次不扩展 S3 二进制接口）。

## 四、文件清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/workspace.py` | 新增 `archive_workspace_snapshot`（zip 打包 + 排除 + 上限 + manifest + 幂等） |
| `src/sortie_deck/settings.py` | 新增 2 个配置项 |
| `src/sortie_deck/orchestrator.py` | `_cleanup_mission_workspace` 清理前归档 |
| `src/sortie_deck/git_publish.py` | `sweep_worktrees` 回收前归档 |
| `src/sortie_deck/api/app.py` | `read_artifact` 支持二进制下载 |
| `apps/web/src/App.tsx` / `i18n.ts` | zip 下载交互与文案 |
| `tests/test_workspace.py` | 归档单测：排除规则、上限截断、幂等跳过、无 workspace 时 no-op |

## 五、备选方案

1. **不打 zip，把 workspace 平移进 artifacts**：会混入 stage 产物视图且与 artifacts 语义（agent 交付物）冲突，放弃。
2. **只对未绑定 repos 的任务归档**：排除 `repos/` 后统一归档成本相同，统一处理代码更简单、绑定任务 workspace 根的散落文件也能留住，选择统一归档。
3. **沙盒改成不删除、仅 TTL 清理**：磁盘风险大，与"workspace 即一次性副本"的既有设计冲突，放弃。

## 六、状态

- [ ] 用户确认方案
- [ ] 实现代码
- [ ] 测试
