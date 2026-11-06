# 仓库凭据绑定改造：仓库为主体、任务只选仓库与分支

- 日期：2026-11-06
- 状态：待确认（确认后才动代码）
- 关联：`docs/plans/2026-11-03-git-credentials-two-tier.md`

---

## 1. 需求（用户已确认）

1. **私有仓库也要绑定凭据**，可选「工作区凭据」或「个人凭据」。
2. **建任务时**：规划师产出 agent 后，开发角色多选要绑定的仓库；执行时用仓库绑定的凭据 clone。
3. **任务绑定时必须写仓库分支**；分支属于任务级信息，不写死在仓库注册表。
4. **host 强校验**：仓库 URL 的 host 与 scheme 必须与凭据完全一致，否则拒绝。

## 2. 目标模型

```
workspace_repos: id, name, url, default_branch, credential_id
user_git_repos:  id, owner_id, name, url, default_branch, credential_id   # 新增
                              │
                              ▼
任务 role_repos: [{ repo_id, source, branch }]   # 不再复制 credential_id
                              │
                              ▼
执行期：repo_id → 注册表 → credential_id → 解密 → clone/push
```

- 凭据 scope 可为 `workspace` 或 `user`；仓库可任选。
- 唯一的硬约束：`credential.host == host_of(repo.url)` 且 `credential.scheme == scheme_of(repo.url)`。
- 执行期按注册表现查凭据，管理员轮换后未完成任务自动生效。

### 2.1 为什么必须 host 强校验

工作区凭据是管理员配、全员可用的共享密钥。若允许跨 host 绑定，成员可注册 `https://evil.example/x.git` 并绑上管理员的 GitHub 凭据，任务执行时 token/私钥会被发到该域名。校验必须放在后端绑定与执行两条路径，不能只靠前端。

### 2.2 分支规则

- 选中仓库时用 `repo.default_branch` 预填，可改。
- `branch` 为空 → 绑定直接 400，提示「请填写分支」。
- `branch` 用于 clone 与任务分支 `sortie/<mission_id>-<repo>` 的基线。
- preflight 用该分支做 `git ls-remote --heads <url> <branch>`，分支不存在即失败。

## 3. 数据层改动

### 3.1 `user_git_repos` 增加列

文件：`src/sortie_deck/git_cred.py`

- `UserRepoStore.__init__` 建表语句加 `credential_id TEXT`；对已存在的库执行幂等迁移：
  ```sql
  ALTER TABLE user_git_repos ADD COLUMN credential_id TEXT
  ```
  用 `PRAGMA table_info(user_git_repos)` 判断列是否存在；异常只忽略 duplicate column。
- `UserRepo`（约 :454）增加 `credential_id: str | None = None`，`to_public()` 返回该字段。
- `_SELECT` / `_row` / `create` / `list_for_owner` 同步；`create` 增加 `credential_id` 参数。
- 新增 `update(repo_id, *, owner_id, credential_id=None, default_branch=None, name=None)`，只允许更新本人记录；私有仓库当前没有 PATCH，需要它来补绑/改分支。

### 3.2 `workspace_repos`

已有 `credential_id` 与 `_UPDATABLE`（约 :477），无需加列；补一条启动时校验：`credential_id` 非空且 host 匹配。

### 3.3 模型

文件：`src/sortie_deck/models.py:170`

- `RepoConfig` 增加 `repo_id: str | None = None`。
- 保留 `source`、`branch`、`credential_id`（历史 payload 兼容）。
- 新增校验语义（在 service 层做，不在 pydantic 里拒绝历史数据）：新绑定的 `repo_id` 必填、`branch` 必填。

## 4. 后端改动

### 4.1 仓库写入校验（新增函数）

文件：`src/sortie_deck/git_cred.py`

新增 `validate_repo_credential(repo_url: str, credential_id: str, *, allowed_owner: str | None) -> GitCredential`：

1. 凭据存在且 `is_active()`；
2. `host_of(repo_url) == credential.host` 且 `scheme_of(repo_url) == credential.scheme`；
3. `allowed_owner`：
   - 工作区仓库：`credential.scope == workspace`；
   - 私有仓库：`credential.scope == user and credential.owner_id == 本人`，**或** `credential.scope == workspace`（同 host 才允许）。
4. 不一致抛 `GitCredentialError`，错误文案包含两侧 host，便于排查。

调用点：
- `POST /api/admin/git/workspace-repos`（app.py:2115 附近，当前只校验 credential 存在）；
- 新增的工作区仓库凭据更新 `PUT /api/admin/git/workspace-repos/{id}/credential`；
- `POST/PATCH /api/me/git/repos`（新增 credential_id 后）。

### 4.2 任务绑定校验

文件：`src/sortie_deck/service.py`

- `_clean_role_repos`（:82）：保留 `repo_id`；`branch` 去空格后若为空直接抛 `ServiceError("validation", "请填写分支")`。
- `_validate_repo_credentials`（:121）重写为按 `repo_id` 校验：
  ```
  source=workspace → workspace_repos.get(repo_id)
  source=custom    → user_repos.get(repo_id) 且 owner_id == 操作人
  取到仓库行后 → validate_repo_credential(repo.url, repo.credential_id, ...)
  ```
  历史 payload 无 `repo_id`：走旧逻辑（workspace 看 payload.credential_id；custom 按 host+scheme 找本人凭据），仅记 warning，不阻断。
- `bind_repos`（:580）：写入 `meta.role_repos` 时保留 `repo_id/source/branch`；不再要求 payload 带 credential_id。
- 新增 `preflight_role_repos` 的分支校验：`ls-remote --heads URL branch`，无输出视为分支不存在。

### 4.3 解析层

文件：`src/sortie_deck/git_cred.py:758`

- `GitCredentialResolver.resolve` 改为：
  1. `repo.repo_id` 存在 → 按 `source` 查 `workspace_repos` / `user_git_repos`，取 `credential_id`；
  2. 仓库行不存在 → `CredentialMissing`（明确报「仓库已停用或不存在」）；
  3. 凭据不存在/停用 → `CredentialMissing`；
  4. 无 `repo_id` 的历史任务 → 旧逻辑兜底。
- 移除 custom 分支的 `find_for_host` 猜测（仅历史兜底保留）。
- 解析后再次做 host/scheme 比对，防止绕过绑定接口直接写库。

### 4.4 API

文件：`src/sortie_deck/api/app.py`

- `UserRepoBody`（:2080）加 `credential_id: str | None = None`。
- `POST /api/me/git/repos` 接收并校验 credential_id；返回体带 credential_id。
- 新增 `PATCH /api/me/git/repos/{repo_id}`：可改 `name/default_branch/credential_id`，只允许本人。
- `GET /api/me/git/repos` / `GET /api/git/workspace-repos` 返回 credential_id。
- `/api/git/preflight` 支持 `repo_id` + `branch`：有 repo_id 时按注册表解析；保留旧字段。
- 工作区仓库接口补 host 校验；管理员私有凭据仍不可读。

## 5. 前端改动

### 5.1 设置页（`GitSettingsPanel.tsx`）

- 私有仓库表单增加「绑定凭据」`<select>`：
  - 选项分组：工作区凭据 / 我的凭据；
  - 仅列出 host+scheme 与仓库 URL 匹配的凭据；无匹配时提示「该主机还没有可用凭据」并提供「去添加」。
- 私有仓库行展示「绑定凭据」；新增「编辑」入口（调用 PATCH），可改凭据与默认分支。
- 历史无 credential_id 的仓库显示「未绑定」红标；点击编辑时必须选凭据才能保存。
- 工作区仓库表单同样只列出 host 匹配的凭据。
- 匹配预览改为按注册表 `credential_id` 显示，不再按 host 猜；未绑定行标红。

### 5.2 任务页（`MissionBriefingChat.tsx`）

- 开发角色仓库选择改为统一多选列表（工作区 + 我的），选项文案 `仓库名 · 凭据名`；无凭据的仓库禁用并提示先去设置绑定。
- 选中后用 `default_branch` 预填分支输入框；分支必填，空值禁用「确认」并提示。
- payload 改为 `{ repo_id, source, branch }`，不再传 credential_id；手填 URL 的临时仓库走 `source=custom, repo_id=null`，沿用旧兜底（仅本机/兼容）。
- 同一角色内按 repo_id 去重；已选仓库再次选择时不重复添加。

## 6. 兼容与迁移

1. **表迁移**：启动时幂等 `ALTER TABLE user_git_repos ADD COLUMN credential_id TEXT`；旧行保持 NULL。
2. **旧私有仓库**：
   - 读取正常；
   - 建任务时若未绑定，前端禁止选择并提示「请先在设置中绑定凭据」；
   - 设置页标「未绑定」，编辑时强制补绑。
3. **旧任务**：`meta.role_repos` 无 `repo_id` → resolver 走旧逻辑，保证历史任务可重跑。
4. **旧 payload 有 credential_id 但无 repo_id**：保留读取，不写回。
5. 迁移不做数据回填（避免按 host 猜错）；如需批量回填，另写脚本并人工确认。

## 7. 分阶段执行

### P0 · 数据与校验（不接 UI，先让后端语义正确）

1. `user_git_repos` 加列 + `UserRepo` / store CRUD + PATCH API。
2. 新增 `validate_repo_credential`，接入工作区仓库与私有仓库写入。
3. `RepoConfig.repo_id`；`_clean_role_repos` 支持 repo_id 与 branch 必填。
4. `GitCredentialResolver` 按 repo_id 解析 + 历史兜底。
5. `_validate_repo_credentials` 重写；绑定与启动前 preflight 覆盖分支存在性。
6. 单测：迁移、host 不匹配、repo_id 解析、branch 为空、历史兼容。

验收：用 API 直接绑定 `{repo_id, source, branch}`，删除 payload 里的 credential_id 仍可成功 clone；跨 host 绑定返回 400。

### P1 · 设置页

1. 私有仓库表单/行增加凭据绑定与编辑（PATCH）。
2. 凭据下拉按 host+scheme 过滤；未绑定标红。
3. 匹配预览按注册表 credential_id 展示。
4. 工作区仓库表单同样过滤。

验收：私有仓库可绑工作区凭据（同 host）；跨 host 选项不出现且强行提交 400；未绑定仓库不能用于任务。

### P2 · 任务页

1. 开发角色仓库统一多选，选项显示凭据名。
2. 分支必填、默认带出、空值拦截。
3. payload 改 `{repo_id, source, branch}`。
4. 已选仓库去重；无凭据仓库禁用。

验收：一个任务里两个开发角色各选多个仓库、各自分支，执行时按仓库当前凭据 clone；管理员轮换凭据后新任务生效。

### P3 · 回归与文档

1. 全量回归：`tests/test_git_repo_lifecycle.py`、`tests/test_artifacts.py`、新增 `tests/test_repo_credential_binding.py`。
2. 前端 `npm run typecheck` + `npm run build`。
3. 更新 `docs/plans/2026-11-03-git-credentials-two-tier.md` 的匹配规则（改为 repo_id 解析）。
4. 更新设置页文案：不再说「按 host 自动匹配」。

## 8. 测试清单

| 编号 | 场景 | 期望 |
|---|---|---|
| T1 | 旧库启动 | 自动加列，旧数据可读，credential_id 为 NULL |
| T2 | 私有仓库绑同 host 个人凭据 | 成功 |
| T3 | 私有仓库绑同 host 工作区凭据 | 成功 |
| T4 | 任意仓库绑跨 host 凭据 | 400，错误含两侧 host |
| T5 | 绑定任务 branch 为空 | 400「请填写分支」 |
| T6 | branch 在远端不存在 | 绑定 preflight 失败 |
| T7 | 任务 payload 只有 repo_id/source/branch | 执行期解析到仓库当前凭据 |
| T8 | 管理员换绑仓库凭据后 | 未启动任务使用新凭据 |
| T9 | 历史任务无 repo_id | 走旧逻辑仍可执行 |
| T10 | 私有仓库 PATCH 改凭据 | 仅本人可改，返回新 credential_id |
| T11 | 同 host 多把个人凭据 | 不再随机取最新，按仓库绑定值 |
| T12 | 仓库被停用后任务执行 | 明确报「仓库已停用或不存在」 |

## 9. 风险与对策

- **权限扩大**：允许私有仓库用工作区凭据，必须依赖 host 强校验；若未来要支持多 host，需要显式白名单，不能放开。
- **历史数据**：不做自动回填，避免把错误凭据固化；用 UI 标「未绑定」推动人工确认。
- **SSH 私钥**：仍只写不读；临时文件 0600，`GIT_SSH_COMMAND` 指定 `IdentitiesOnly=yes`。
- **同仓库多分支**：同一任务内同一 repo_id 只允许一个 branch；跨角色不同分支会共用 checkout，本期先禁止重复绑定，后续再拆独立目录。
- **迁移失败**：ALTER TABLE 幂等，失败只记录日志不阻断启动；但写仓库时若列不存在会报错，启动自检需覆盖。

## 10. 完成定义

- 任务绑定请求中不再出现 credential_id，只有 `repo_id/source/branch`。
- 任何跨 host 绑定在 API 层被拒绝，且执行层二次校验。
- 私有仓库可在设置中绑定工作区或个人凭据，并能改绑。
- 分支必填，preflight 校验分支存在。
- 全部测试通过，前端 typecheck/build 通过。

