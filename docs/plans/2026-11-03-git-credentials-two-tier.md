# 云端 Git 访问：两级凭据模型（工作区仓库 / 个人凭据）

- 日期：2026-11-03
- 状态：**待确认**（确认后才动代码）
- 关联问题：任务在云服务器上执行时没有代码仓库访问权限；用户点击 worktree 目录报 `xdg-open` 缺失（已修复上线，与本方案无关）

---

## 1. 需求（用户已拍板的两级模型）

1. **工作区仓库（管理员配置）**：管理员维护「可操作仓库清单」及其凭据；**所有登录用户都能看到并选用**这些仓库，执行时统一使用管理员配置的凭据。
2. **个人凭据（用户自配）**：用户在个人设置里自行填写 git 地址与对应密钥；**只有本人可见可改**，其他人（含管理员）无权查看和修改。

不做第三级（仓库级 deploy token 独立成一级）。

---

## 2. 现状与硬约束（实测）

### 2.1 代码现状

| 位置 | 现状 |
|---|---|
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/models.py:170` | `RepoConfig(url, branch, name, token)`，`token` 直接挂在任务 payload 里 |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/plugins/coding.py:356` | `_clone_url()` 把 `token` 拼成 `https://token@host/...` 传给 `git clone` |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/plugins/coding.py:363` | `clone_repos()` 逐个仓库 clone 到 `workspace/repos/<name>` |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/git_publish.py:130` | push 复用同一套 URL 注入 |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/service.py:82` | `_clean_role_repos()` 原样保留 token → 明文落 `initiatives.sqlite` 的 payload |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/service.py:494` | `bind_repos()` 只校验任务状态与操作权限，不校验连通性 |
| `/Library/git-root/sortie-deck-todos/src/sortie_deck/settings.py:291-304` | 只有 `gitlab_base_url` / `gitlab_api_token`，无默认仓库凭据、无代理、无 SSH key 配置 |
| `/Library/git-root/sortie-deck-todos/apps/web/src/components/MissionBriefingChat.tsx:203-231` | 绑仓库 UI 只有 `{url, branch}` 两栏手填，无凭据概念 |

已确认的三个安全缺口：

1. **明文落盘**：token 存在 mission payload（`initiatives.sqlite`），任何能读库的人都能拿到。
2. **命令行泄露**：拼接后的 URL 会进 `process.command` span（`/Library/git-root/sortie-deck-todos/src/sortie_deck/plugins/coding.py:255` 的 `_safe_command()` 只打码 `--token` 这类 flag，不管 URL 里的 `user:pass@`），`ps`/`/proc/<pid>/cmdline` 同样可见。
3. **失败时机太晚**：凭据错/网络不通要等到 coding 阶段 clone 超时才暴露。

### 2.2 云端实测（2026-11-03，`i-j6cazclc2tnx3fdg38g8`）

- 容器内 `gitlab.whsaihui.cn` 解析到 `10.217.0.31`（内网），TCP 443 **超时**；
- `github.com` / `gitlab.com` / `codeload.github.com` **可达**；
- `sortie-worker` 内无 `~/.ssh`、无 `git config --global`、无 `GH_TOKEN`/`GITLAB_TOKEN`；`.env` 无任何 git 相关键；
- 线上 5 个任务**均未绑定仓库**，`gitlab_api_token` 未配置 → 本次改造无历史数据迁移压力。

> **重要**：凭据解决「你是谁」，解决不了「连不上」。内网 GitLab 需另行走 VPN/专线或镜像仓（见 §12），本方案只保证「凭据正确时能给出明确的鉴权/网络失败原因」。

---

## 3. 业内做法（调研摘要）

- **单仓最小权限凭据是 CI 拉私有库的主流做法**：deploy key / deploy token 绑定单个仓库、只读、随仓库删除而失效，避免把个人 PAT（覆盖全账号）放进无人值守机器 —— [TechResolve](https://techresolve.blog/2026/03/04/is-anyone-else-considering-alternatives-iv-been)、[Deploy Keys for Unattended Git Pulls](https://www.ytpor.com/2026/07/deploy-keys-for-multiple-repos)
- **凭据不落命令行**：CI 体系普遍用 `GIT_ASKPASS` / credential helper / ssh-agent 注入，而非把 secret 拼进 URL —— [Private repository auth patterns](https://www.deployhq.com/blog/building-your-site-using-dependencies-from-a-private-repository)
- **内网仓库给外部 CI 用**：主流是隧道/VPN 组网或镜像同步，而不是把内网 GitLab 直接暴露公网 —— [内网穿透方案汇总](https://www.sohu.com/a/973602040_122597938)

本方案与主流的差异：把「工作区仓库」做成平台内的一等实体（管理员维护、全员可见可选用），而不是让每个任务各自填 URL；个人凭据只作为自建/私人仓库的补充。

---

## 4. 总体设计

### 4.1 两类仓库来源（来源决定凭据，不存在回退竞争）

| 来源 | 谁能创建 | 谁能看到 | 用谁的凭据 |
|---|---|---|---|
| `workspace` 工作区仓库 | 管理员 | 所有登录用户（只读） | 该条目绑定的**工作区凭据** |
| `custom` 自定义仓库 | 任意登录用户（在任务里填 URL） | 任务可见范围内的人 | 该用户**本人的个人凭据**（按 host 匹配） |

**不做优先级回退**：工作区仓库永远用工作区凭据，个人凭据不能覆盖它（防止绕过管理员策略）；自定义仓库永远用个人凭据，不会借用工作区凭据（防止越权使用服务账号）。

### 4.2 凭据解析规则

```text
repo.source == "workspace" → workspace_repos.credential_id → git_credentials(scope=workspace)
repo.source == "custom"    → 按 (host, scheme) 匹配 git_credentials(scope=user, owner_id=任务创建者)
匹配不到                       → 绑定阶段直接拒绝（400），不进入 coding 阶段
```

### 4.3 可见性矩阵

| 对象 | 管理员 | 本人 | 其他用户 |
|---|---|---|---|
| 工作区仓库条目（URL/分支/启用状态） | 增删改查 | 只读 | 只读 |
| 工作区凭据（明文） | **不可读回**，只能覆盖写入/禁用 | 无 | 无 |
| 个人凭据（明文） | **不可读回**，可**禁用**（安全处置，留审计） | 增删改查 | **404**（不暴露存在性） |

个人凭据的「禁用」能力作为管理员安全兜底（离职、密钥外泄），但不提供读取与修改；是否保留该能力列为决策项 D3。

### 4.4 加密与审计

- 加密算法：Fernet（`cryptography`，本机与云端镜像均已有 `50.0.2`，需在 `pyproject.toml` 显式声明依赖）。
- 主密钥：新增 `TDT_GIT_SECRET_KEY`（base64 32 字节）。**未配置时拒绝保存凭据**，不降级明文。
- 轮换：用 `MultiFernet`，新钥在前、旧钥保留一轮解密。
- 审计事件（写入事件流 + 日志）：`谁 / 何时 / 哪个凭据 id / 对哪个 host 做了 clone|push|preflight / 成功或失败`，不含明文。

### 4.5 凭据注入方式（替代 URL 拼接）

| 协议 | 注入方式 |
|---|---|
| HTTPS | 任务级临时 askpass 脚本（0700，随机名，落在任务沙盒 tmp）+ `GIT_ASKPASS` + `GIT_TERMINAL_PROMPT=0`；`_run_cmd(extra_env=...)` 已支持（`/Library/git-root/sortie-deck-todos/src/sortie_deck/plugins/coding.py:273`） |
| SSH（二期） | 私钥写临时文件（0600）+ `GIT_SSH_COMMAND="ssh -i <file> -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"` |

子进程结束即删临时文件；span 里只记 `repo.host` 与 `credential.id`。

---

## 5. 数据模型

新增单库 `data/git_registry.sqlite`（与 `api_keys.sqlite` 同风格：WAL + `busy_timeout` + 线程锁，见 `/Library/git-root/sortie-deck-todos/src/sortie_deck/api_keys.py:1-13`）。

```sql
CREATE TABLE git_credentials (
  id             TEXT PRIMARY KEY,          -- crd_xxx
  scope          TEXT NOT NULL,             -- workspace | user
  owner_id       TEXT NOT NULL,             -- scope=workspace 固定 'workspace'；scope=user 存 user_id
  label          TEXT NOT NULL,             -- 展示名，如「GitLab 服务账号」
  host           TEXT NOT NULL,             -- gitlab.whsaihui.cn / github.com
  scheme         TEXT NOT NULL,             -- https | ssh
  username       TEXT NOT NULL DEFAULT '',  -- https 用；ssh 可空
  secret_cipher  BLOB NOT NULL,             -- 加密后的 token / 私钥
  fingerprint    TEXT NOT NULL,             -- 展示用，如 sk-…a1b2 / SHA256:xx
  expires_at     REAL,                      -- 空=长期
  disabled_at    REAL,                      -- 空=启用
  created_at     REAL NOT NULL,
  last_used_at   REAL
);
CREATE UNIQUE INDEX ux_cred_host ON git_credentials(scope, owner_id, host, scheme, username);

CREATE TABLE workspace_repos (
  id             TEXT PRIMARY KEY,          -- wrp_xxx
  name           TEXT NOT NULL,             -- checkout 目录名
  url            TEXT NOT NULL,
  default_branch TEXT NOT NULL DEFAULT '',
  role_slots     TEXT NOT NULL DEFAULT '[]',-- 建议适用的槽位，如 ["eng","qa"]，空=不限
  credential_id  TEXT NOT NULL,
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_by     TEXT NOT NULL,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL
);
```

`RepoConfig` 变更（`/Library/git-root/sortie-deck-todos/src/sortie_deck/models.py:170`）：

```python
class RepoConfig(BaseModel):
    url: str
    branch: str | None = None
    name: str | None = None
    source: Literal["workspace", "custom"] = "custom"
    credential_id: str | None = None      # workspace 来源必填；custom 留空，执行期按 host 解析
    token: str | None = None              # @deprecated：只读兼容一个版本，新写入一律置空
```

---

## 6. API 契约

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/git/workspace-repos` | 登录用户 | 列出启用中的工作区仓库（url/name/branch/slots，无密钥） |
| POST | `/api/admin/git/workspace-repos` | admin | 新建仓库条目（含首次写入 `credential`） |
| PATCH | `/api/admin/git/workspace-repos/{id}` | admin | 改 URL/分支/slots/启用状态 |
| PUT | `/api/admin/git/workspace-repos/{id}/credential` | admin | 覆盖写入工作区凭据（只写不读） |
| DELETE | `/api/admin/git/workspace-repos/{id}` | admin | 删除条目（软删或禁用，决策项 D4） |
| GET | `/api/me/git-credentials` | 本人 | 列表，仅返回 id/label/host/scheme/fingerprint/expires_at |
| POST | `/api/me/git-credentials` | 本人 | 新建个人凭据（写入即加密） |
| DELETE | `/api/me/git-credentials/{id}` | 本人 | 删除 |
| POST | `/api/me/git-credentials/{id}/verify` | 本人 | `git ls-remote` 探活，返回 ok/原因 |
| POST | `/api/admin/git/credentials/{id}/disable` | admin | 安全处置：禁用某条个人凭据（决策项 D3） |
| POST | `/api/git/preflight` | 登录用户 | 传 `{url, branch}` → 返回将使用的凭据来源 + `ls-remote` 结果（不返回密钥） |

统一约定：任何响应、错误信息、事件流都**不得**包含密钥明文；错误信息只允许出现 host、username、fingerprint。

---

## 7. 服务层与执行链路改造

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/git_cred.py`（新增） | 凭据 CRUD、Fernet 加解密、host 解析、askpass/ssh env 构造、`ls-remote` 预检 |
| `src/sortie_deck/service.py:82` | `_clean_role_repos()`：校验 source、工作区仓库存在且启用、custom 必须有匹配 host 的个人凭据；不再接受外部传入的 `token` |
| `src/sortie_deck/service.py:494` | `bind_repos()`：绑定前调用 preflight（10s 超时），失败直接 400 并给可读原因 |
| `src/sortie_deck/plugins/coding.py:356` | 删除 `_clone_url()` 的 token 拼接，改为返回原始 URL；clone 时通过 `extra_env` 注入凭据 |
| `src/sortie_deck/plugins/coding.py:363` | `clone_repos()` 增加 `actor_user_id` 参数（取任务 `created_by`），用于解析个人凭据 |
| `src/sortie_deck/git_publish.py:130` | push 同样改为 env 注入 |
| `src/sortie_deck/settings.py:291-304` | 新增 `git_secret_key`、`git_preflight_timeout`、`git_ssh_command` 等配置 |
| `src/sortie_deck/api/app.py`（§6 路由） | 新增 git 仓库与凭据相关路由，沿用 `/api/admin/users` 的依赖注入风格（`app.py:678-718`） |
| `src/sortie_deck/models.py:170` | `RepoConfig` 新增 `source` / `credential_id`，`token` 标 deprecated |
| `pyproject.toml` | 显式声明 `cryptography>=43`（当前是传递依赖，不能依赖它一直存在） |

### 执行期身份（默认值，见决策项 D1）

- 任务创建时快照 `created_by`；worker 执行时按该用户解析个人凭据。
- 凭据在执行前被删除/禁用 → 该阶段**硬失败**，事件流提示「重新绑定仓库或更新凭据」（决策项 D2）。

---

## 8. 前端改造

| 页面 | 改动 |
|---|---|
| 设置页 | 新增「Git 凭据」区块：列表（label/host/指纹/到期）、新建（host + 类型 + 密钥 + 可选用户名）、删除、探活按钮；密钥输入框只写不读 |
| 管理页 | 新增「工作区仓库」区块：仓库列表、增删改、注入/覆盖凭据、启用开关 |
| 任务规划（绑仓库） | `/Library/git-root/sortie-deck-todos/apps/web/src/components/MissionBriefingChat.tsx:203-231` 的 `{url, branch}` 行改为：仓库选择器（工作区仓库下拉）+ 「自定义 URL」入口；每行显示凭据来源徽标（`工作区凭据` / `个人凭据` / `缺少凭据`） |
| 全局 | 预检失败的红色提示复用现有 `error` bar；凭据缺失时禁用「确认」按钮 |

---

## 9. 迁移方案

- 线上 `initiatives.sqlite` 中 5 个任务**均无** `role_repos`（已实测），无数据迁移。
- 代码层保留 `RepoConfig.token` 读取兼容一个版本：读到 token 时记 warn 事件「该任务使用了旧版明文凭据，请重新绑定」，但不参与新逻辑写入。

---

## 10. 测试计划

- 单测 `tests/test_git_credentials.py`（新增）：
  - 加解密往返、密钥缺失时拒绝写入、fingerprint 生成；
  - 唯一约束冲突；禁用后解析失败；
  - 解析规则：workspace 用工作区凭据 / custom 用本人凭据 / 无匹配返回明确错误。
- 单测 `tests/test_workspace_git_repos.py`（新增）：可见性（他人 404）、admin 无明文读取路径。
- 集成：`bind_repos` 预检失败即 400；clone 成功后 `process.command` span 中不含 secret（断言）。
- 端到端（手工）：admin 配工作区仓库 → 普通用户建任务选中 → 克隆成功；普通用户自建个人凭据 → 另一个用户看不到、API 404。

---

## 11. 验收标准

1. 管理员配置工作区仓库 + 凭据后，任何登录用户建任务都能选到并克隆成功。
2. 用户 A 的个人凭据，用户 B 与管理员均**看不到明文**；B 直接访问返回 404。
3. 凭据缺失、鉴权失败、网络不可达三种情况，在**绑定阶段**即给出明确原因，不进入 coding 阶段。
4. 日志、事件流、OTel span 中不出现任何密钥明文（含 `process.command`）。
5. 仓库 URL / 分支的绑定语义与原行为一致，历史任务不受影响。

---

## 12. 不在本方案范围内（另案）

- **内网 GitLab 连通性**：当前云端到 `10.217.0.31:443` 超时。可选 VPN/专线、内网反向代理、push mirror 镜像仓、worker 下沉内网。本方案只保证「凭据正确但网络不通」时给出准确报错。
- **SSH 凭据**：二期支持（HTTPS token 先覆盖主要场景）。
- **仓库级 deploy token 独立成一级**：按用户要求暂不做。

---

## 13. 待确认（PRD/评审需拍板）

| 编号 | 问题 | 建议默认 |
|---|---|---|
| D1 | 共享任务里别人重跑，用谁的凭据？ | 任务创建者快照；允许「以当前操作人凭据重跑」作为高级开关 |
| D2 | 凭据在执行前失效，任务怎么办？ | 硬失败 + 事件流提示重新绑定 |
| D3 | 管理员可否禁用他人的个人凭据（不可读、不可改）？ | 保留禁用能力，记审计 |
| D4 | 删除工作区仓库：软删（保留历史任务引用）还是硬删？ | 软删（`enabled=0`） |
| D5 | 是否允许用户填全权限个人 PAT？ | 允许但保存时 `ls-remote` 探活；提示建议用最小权限 token |

---

## 14. 工作量估算

| 项 | 人日 |
|---|---|
| 后端（表/加密/解析/注入/预检/API） | 1.5 |
| 前端（设置页 / 管理页 / 绑定面板） | 1.0 |
| 测试 + 云端联调 | 0.5 |

---

## 15. 状态

**待确认**。确认后按 §7 文件清单落地，先做 HTTPS token 链路，SSH 二期。
