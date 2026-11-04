# 执行计划：云端 Git 两级凭据（TDD，逐步可验证）

- 日期：2026-11-04
- 设计依据：[`docs/plans/2026-11-03-git-credentials-two-tier.md`](./2026-11-03-git-credentials-two-tier.md)
- 策略：**每步先写测试（红）→ 实现（绿）→ 全量回归 → lint/typecheck**，每步结束仓库保持可运行、可部署
- 决策默认值：D1 创建者快照、D2 硬失败、D3 保留禁用、D4 软删、D5 允许但探活、**D6 分支统一 `sortie/<id>-<repo>`**、**D7 重跑复用分支**（如有异议，在对应步骤开工前提出）

---

## 0. 总览

| 步骤 | 主题 | 主要产出 | 前置 | 预估 |
|---|---|---|---|---|
| S0 | 准备 | 分支、密钥、测试基线 | — | 0.2d |
| S1 | 凭据存储层 | `git_cred.py`（加解密 + CRUD） | S0 | 0.4d |
| S2 | 仓库注册表 | 工作区仓库 / 个人仓库清单 | S1 | 0.3d |
| S3 | 凭据解析与注入 | `resolve_git_env()`、askpass | S1,S2 | 0.4d |
| S4 | 执行链路接入 | clone/push 不再拼 token | S3 | 0.3d |
| S5 | 绑定与服务层校验 | `bind_repos` + preflight | S2,S3 | 0.3d |
| S6 | API 路由 | §6 全部端点 | S5 | 0.3d |
| S7 | 前端 | 设置页 / 管理页 / 角色级选择器 | S6 | 1.0d |
| S8 | 端到端联调 | 真实仓库链路验证 | S7 | 0.4d |
| S9 | 云端部署 | api 镜像更新 | S8 | 0.2d |

每步验证命令统一为：

```bash
cd /Library/git-root/sortie-deck-todos
.venv/bin/python -m pytest tests -q                 # 全量回归（当前基线约 95 个文件，需全绿）
.venv/bin/python -m ruff check src tests            # lint
cd apps/web && npm run typecheck                    # S7 起追加
```

---

## S0 准备（不改业务代码）

- [ ] 建分支：`git checkout -b feat/git-credentials-two-tier`
- [ ] 生成加密主密钥并写入本地 `.env`（**不进仓库**）：
      `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`
      → `.env` 追加 `TDT_GIT_SECRET_KEY=<值>`
- [ ] 记录测试基线：全量 `pytest -q` 通过数（作为后续回归对比基准）

**DoD**：基线全绿；`.env` 未被 git 跟踪（`git status` 干净）。

---

## S1 凭据存储层

**先写测试** `tests/test_git_credentials.py`：

| 用例 | 断言 |
|---|---|
| `test_encrypt_roundtrip` | 写入后 `get_secret()` 能还原明文；库内 `secret_cipher` 字节不含明文子串 |
| `test_missing_secret_key_refuses_write` | 未配置 `TDT_GIT_SECRET_KEY` 时 `create()` 抛 `GitCredentialError`，库中无记录 |
| `test_fingerprint_masks_secret` | `fingerprint` 形如 `sk-…a1b2`，长度受限，不含完整 token |
| `test_list_never_returns_secret` | `list_for_owner()` / `to_public()` 的结果字段不含 `secret` |
| `test_unique_host_constraint` | 同 `(scope, owner, host, scheme, username)` 二次写入抛冲突 |
| `test_scope_isolation` | 同 host 的 workspace 与 user 凭据可共存，互不覆盖 |
| `test_disabled_credential_not_resolved` | `disabled_at` 非空时解析返回不可用 |

**实现**：

- 新增 `src/sortie_deck/git_cred.py`：
  - `GitCredential` 模型（对外脱敏视图 `to_public()`）
  - `GitCredentialStore`：sqlite（WAL + `busy_timeout` + 线程锁，参照 `/Library/git-root/sortie-deck-todos/src/sortie_deck/api_keys.py:1-13`）
  - `Fernet` / `MultiFernet`（密钥轮换），`encrypt()` / `decrypt()`
- `pyproject.toml` 显式声明 `cryptography>=43`

**验证**：`pytest tests/test_git_credentials.py -q` + 全量回归 + `ruff check`。

**DoD**：7 个用例通过；密钥缺失时明确报错而非降级明文。

---

## S2 仓库注册表

**先写测试** `tests/test_git_registry.py`：

| 用例 | 断言 |
|---|---|
| `test_workspace_repo_crud` | 增删改查；`enabled=0` 不出现在 `list_enabled()` |
| `test_workspace_repo_requires_credential` | 绑定不存在的 `credential_id` 被拒绝 |
| `test_user_repo_owner_isolation` | A 登记的仓库 B 查不到（返回空而非 403 泄露） |
| `test_user_repo_unique_url` | 同一用户重复 URL 被拒绝 |

**实现**：`git_cred.py` 增加 `WorkspaceRepoStore` / `UserRepoStore`（同库两表，DDL 见设计文档 §5）。

**验证**：`pytest tests/test_git_registry.py -q` + 全量回归。

---

## S3 凭据解析与注入（核心）

**先写测试** `tests/test_git_cred_resolve.py`：

| 用例 | 断言 |
|---|---|
| `test_resolve_workspace_source` | `source=workspace` 用条目绑定的凭据，忽略 actor 个人凭据 |
| `test_resolve_custom_source` | `source=custom` 按 `host+scheme` 命中 actor 的个人凭据 |
| `test_resolve_missing_raises` | 无匹配抛 `CredentialMissing`，错误信息只含 host（不含密钥） |
| `test_askpass_script_permissions` | 生成脚本权限 `0o700`，内容不含 token 明文 |
| `test_askpass_env_shape` | 返回 env 含 `GIT_ASKPASS` / `GIT_TERMINAL_PROMPT=0` / `GIT_USERNAME` / `GIT_PASSWORD` |
| `test_askpass_cleanup` | 上下文退出后临时文件被删除 |
| `test_command_has_no_secret` | `_safe_command()` 处理后的命令串不含 token |

**实现**：

- `git_cred.py` 增加：`resolve_for_repo(repo, actor_user_id) -> ResolvedCredential`、`askpass_env(cred) -> dict`、`cleanup()`
- 明确「解析不读 mission payload 里的旧 token」

**验证**：`pytest tests/test_git_cred_resolve.py -q` + 全量回归。

**DoD**：解析规则 100% 覆盖 §4.2；任何路径都不返回明文给调用方以外的地方。

---

## S4 执行链路接入

**先写测试** `tests/test_coding_git_injection.py`：

| 用例 | 断言 |
|---|---|
| `test_clone_args_have_no_token` | monkeypatch `_run_cmd`：clone 的 `args` 里 URL 与 `repo.url` 完全一致，不含 `@` 凭据段 |
| `test_clone_passes_askpass_env` | clone 调用带 `extra_env`，含 `GIT_ASKPASS` |
| `test_origin_url_reset_without_credential` | clone 后 `git remote set-url origin` 使用裸 URL（回归现有行为） |
| `test_push_args_have_no_token` | `_push_branch` 的 `args` 不含凭据，凭据只在 env |
| `test_branch_naming_unified` | 单仓/多仓均为 `sortie/<id>-<repo>`（D6） |

**实现**：

- `/Library/git-root/sortie-deck-todos/src/sortie_deck/plugins/coding.py:356`：`_clone_url()` 移除 token 拼接（保留函数名做兼容，返回 `repo.url`）
- `coding.py:427-430`：clone 增加 `extra_env=askpass_env(...)`；`clone_repos()` 增加 `actor_user_id`
- `/Library/git-root/sortie-deck-todos/src/sortie_deck/git_publish.py:130`：push 同样改 env 注入
- `git_publish.py:48-53`：`mission_branch()` 统一带 repo 后缀（D6），调用处同步
- 更新受影响旧测试：`tests/test_coding_branching.py`、`tests/test_git_publish.py`、`tests/test_git_publish.py` 里的分支名断言

**验证**：新增用例 + 全量回归（重点看 git 相关 4 个测试文件）。

**风险**：分支名变更影响历史任务重跑（旧分支 `sortie/<id>` 不再被复用）→ 上线说明里注明；本地旧数据无影响。

---

## S5 绑定与服务层校验

**先写测试** `tests/test_bind_repos.py`：

| 用例 | 断言 |
|---|---|
| `test_bind_workspace_repo_ok` | 存在且启用的工作区仓库可绑定 |
| `test_bind_workspace_repo_disabled` | 禁用条目绑定返回 400，错误含仓库名 |
| `test_bind_custom_requires_user_credential` | 用户无匹配 host 凭据 → 400，提示去设置 |
| `test_bind_preflight_failure` | mock `ls-remote` 失败 → 400，错误区分「鉴权失败/网络不可达/仓库不存在」 |
| `test_legacy_token_field_readonly` | 传入 `token` 被忽略并记录 warn 事件 |

**实现**：`/Library/git-root/sortie-deck-todos/src/sortie_deck/service.py:82`（`_clean_role_repos`）、`service.py:494`（`bind_repos`）、`/Library/git-root/sortie-deck-todos/src/sortie_deck/models.py:170`（`RepoConfig` 加 `source`/`credential_id`）。

**验证**：新增用例 + `tests/test_mission_assembly.py`、`tests/test_cli_mission.py` 回归。

---

## S6 API 路由

**先写测试** `tests/test_git_api.py`（FastAPI `TestClient`，参考 `tests/test_auth.py` 的登录夹具）：

| 用例 | 断言 |
|---|---|
| `test_admin_crud_workspace_repos` | admin 可增删改；响应不含凭据明文 |
| `test_member_readonly_workspace_repos` | 普通用户 GET 成功、POST/PATCH/DELETE 403 |
| `test_user_credential_owner_only` | A 建凭据，B `GET` 列表不含；B 直接 `DELETE /{id}` 返回 404 |
| `test_admin_cannot_read_user_secret` | admin 无任何接口能取回明文（断言所有响应字段集合） |
| `test_preflight_endpoint` | mock `ls-remote`：成功/失败两种返回 |
| `test_response_never_contains_secret` | 遍历所有 git 相关响应，断言不含写入时的明文 |

**实现**：`/Library/git-root/sortie-deck-todos/src/sortie_deck/api/app.py` 新增 §6 全部路由，沿用 `/api/admin/users` 的依赖风格（`app.py:678-718`）。

**验证**：`pytest tests/test_git_api.py -q` + `tests/test_auth.py` 回归。

---

## S7 前端

**验证方式**（无前端单测框架）：`npm run typecheck` + `npm run build` + 手工用例清单。

**实现**：

1. 设置页新增「Git 凭据」：凭据列表 / 新建（host、类型、密钥、可选用户名）/ 删除 / 探活；密钥只写不读。
2. 设置页新增「我的仓库」：登记 name + url + 默认分支。
3. 管理页新增「工作区仓库」：列表、增删改、写入/覆盖凭据、启用开关。
4. `/Library/git-root/sortie-deck-todos/apps/web/src/components/MissionBriefingChat.tsx:203-231`：`{url, branch}` 输入行 → 角色级仓库选择器（分组下拉 + 分支 + 只读凭据徽标 + 多选上限 3 + 缺凭据置灰）。

**手工用例（逐条打勾）**：

- [ ] 普通用户能看到工作区仓库列表，看不到任何密钥
- [ ] 缺凭据的选项置灰，点击跳设置页
- [ ] 可多选 3 个仓库并分别填分支
- [ ] 保存时预检失败 → 行内标红 + 明确原因
- [ ] 同一角色两个节点显示同一份仓库配置（联动）
- [ ] 个人凭据只有本人可见（换账号验证）
- [ ] 选择面板全程无密钥输入框
- [ ] 窄屏（<900px）布局不破

**DoD**：typecheck + build 通过；8 条手工用例全过。

---

## S8 端到端联调（本地）

**准备**：本地起 api（`sortie api --port 8787`）+ 一个可达的测试仓库。

| 场景 | 期望 |
|---|---|
| 工作区仓库 + 工作区凭据 | 建任务 → coding 阶段 clone 成功 → 生成 `sortie/<id>-<repo>` → push 到远端同名分支 → MR 创建（或软降级原因可读） |
| 个人凭据（custom） | 同上，且只有本人可用；换账号后该仓库绑定失败退款并提示缺凭据 |
| 凭据错误 | 绑定阶段 400，提示鉴权失败，不进入 coding |
| 网络不可达 | 绑定阶段 400，提示网络不可达（不挂到超时） |
| 日志审计 | api/worker 日志、事件流、span 中 grep 明文 token → 0 命中 |
| 分支语义 | 远端只多出任务分支；目标分支 HEAD 未变；`git log --oneline` 无 merge commit |

**DoD**：以上 6 项全部符合；`grep -r "<明文token片段>"` 在日志目录无命中。

---

## S9 云端部署（工作区凭据就绪后）

复用已验证的发布流程：

1. `git diff > /tmp/git-cred.patch` → `workbench upload` → 服务器 `git apply --check && git apply`
2. 服务器 `.env` 增加 `TDT_GIT_SECRET_KEY`（先备份）
3. `docker compose build api` → `docker compose up -d --no-deps api`
4. 验证：`/api/health` ok、前端产物哈希更新、`/api/git/workspace-repos` 401（未登录）

**DoD**：公网页面可用；admin 配一条工作区仓库后普通账号能选中并预检通过。

---

## 回滚策略

- S1–S3 为纯新增文件，回滚 = 删除文件 + 恢复 `pyproject.toml`；
- S4–S6 改动集中在 5 个文件，回滚 = `git revert` 对应提交（每步单独提交）；
- 上线后如需紧急回退：镜像 tag 回滚（`sortie-deck:local` 重新 build 上一提交）+ 恢复 `.env`。

---

## 提交节奏

每步一个 commit（便于独立回滚）：

```text
feat(git-cred): 凭据存储层（加密 + CRUD）
feat(git-cred): 工作区仓库与个人仓库注册表
feat(git-cred): 凭据解析与 askpass 注入
refactor(coding): clone/push 改用 env 注入凭据，分支统一带 repo 后缀
feat(git-cred): 绑定校验与 ls-remote 预检
feat(api): git 凭据与仓库管理接口
feat(web): 凭据设置、工作区仓库管理与角色级仓库选择器
```

---

## 状态

- [x] S0 准备（基线 **753 passed**；改动直接在 `dev` 上完成，不另开分支；本地 `.env` 增加 `TDT_GIT_SECRET_KEY`）
- [x] S1 凭据存储层（`git_cred.py`，7 用例：加密往返/缺钥拒写/指纹脱敏/唯一约束/scope 隔离/禁用失效）
- [x] S2 仓库注册表（`workspace_repos` + `user_git_repos`，5 用例：CRUD/启用过滤/owner 隔离/URL 唯一）
- [x] S3 凭据解析与注入（来源决定凭据 + askpass，9 用例：workspace/custom/缺失报错/脚本 0700/清理/命令无密钥）
- [x] S4 执行链路接入（clone/push 改 env 注入，分支统一带 repo 后缀；5 用例 + 修正 4 个旧断言）
- [x] S5 绑定与服务层校验（`_validate_repo_credentials` + `preflight_role_repos`，8 用例：凭据归属/预检分类）
- [x] S6 API 路由（11 个端点，7 用例：管理员 CRUD/全员可读/个人凭据 404/响应无明文）
- [x] S7 前端（`GitCredentialsPanel` / `MyReposPanel` / `WorkspaceReposPanel` + 角色级仓库选择器；`tsc -b` 与 `vite build` 通过）
- [x] S8 端到端联调（真实 git：clone → `sortie/<id>-<repo>` → commit → push；远端仅多任务分支，main 未变）
- [x] S9 云端部署（**api 已上线**；worker 因有任务在跑未重启，待 `ini_764bd3aed186` 结束后执行 `docker compose up -d --no-deps worker`）

### 实测记录（2026-11-04）

- 全量回归：**795 passed**（基线 753 + 42 新增/调整用例），改动文件 `ruff check` 全绿。
- 云端验证：`/api/health` ok；`/assets/index-Dr58zR1Y.js` 已生效；`/api/git/workspace-repos`、`/api/me/git-credentials`、`/api/me/git/repos` 未登录均返回 401；容器内 `TDT_GIT_SECRET_KEY` 已注入且 `stores_from_settings()` 可构造。
- 注意：云端 worker 仍跑旧镜像（clone/push 尚未走凭据链路），正式使用前需重启 worker。
