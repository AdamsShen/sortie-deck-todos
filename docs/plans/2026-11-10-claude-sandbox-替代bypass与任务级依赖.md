# 用 Claude Code 沙箱替代全局 bypass，并把依赖装进任务工作区

- 日期：2026-11-10
- 状态：**待确认，未改代码**
- 前置：`docs/plans/2026-11-09-权限阻断与环境阻塞熔断.md`（P0 已落地：权限透传 + 环境阻塞归因 + 自动暂停）
- 触发问题：用户提出「环境问题 Claude Code SDK 能否自己修？没装 node 能否在沙箱里自行安装？整个任务是不是在沙箱里跑？」

---

## 1. 需求与事实纠正

### 1.1 两个需要先纠正的前提

1. **本次故障不是「没装 node」**：云端实测 `node v22.23.4` 位于 `/usr/local/bin/node`，npm 10.9.9；失败的是 `node --test` / `npm` / `curl` 被命令审批拒绝，`node --version` 属于只读命令集才放行。
2. **SDK 不会自己修环境**：`claude-agent-sdk` 只驱动 agent loop（工具 / 权限 / 会话），没有「检测缺依赖 → 自动安装」的能力。所谓自修只能靠 agent 调 Bash；而本次 Bash 恰好被挡，形成自举悖论。

### 1.2 用户真正想要的效果

- 环境问题不要让任务空转，最好能自愈；
- 每个任务像 Python venv 一样有独立依赖环境；
- 任务内的安装/改动**不影响其他人的开发环境**。

### 1.3 现状核查（2026-11-10 实测）

| 维度 | 实测结果 | 结论 |
|---|---|---|
| 隔离单元 | `data/missions/<id>/workspace` 普通目录，workspace 内无 `node_modules` / `.venv` | 目录级，不是任务级沙箱 |
| 运行时 | node / claude / codex 全部镜像构建期全局预置 | 任务间共享 |
| 容器 | api 与 worker 同镜像、共享 `/data` 卷；同一 worker 进程内多任务并发 | 无 per-task 容器 |
| 全局可写 | `/usr/local/bin` 对 `sortie`(uid 10001) **READONLY** | 全局安装被 OS 拒绝（防污染） |
| workspace 可写 | `test -w` 通过 | 可以在任务内装依赖 |
| 沙箱依赖 | 容器内 **无 `bwrap`、无 `socat`**，只有 `unshare` | 官方 Linux 沙箱当前不可直接用 |
| SDK 能力 | `ClaudeAgentOptions.settings: str \| None`；`permission_mode` / `setting_sources` / `can_use_tool` 均可用 | 可通过 settings 注入沙箱配置 |
| 现有权限 | 云端 `.env` 已配 `bypassPermissions`（P0 前的止血） | 权限面最大，需收窄 |
| 文档自评 | 「还不是完整安全边界…网络和文件系统没有限制…需补容器级隔离」`docs/工具箱-tool-skill-mcp-执行流程调研.md:156` | 强隔离未实现（P2） |

---

## 2. 业内调研（真实检索 + 来源）

### 2.1 Claude Code 自带 Bash 沙箱（最贴合本问题）

- 沙箱由**操作系统**执行边界，覆盖 Bash / PowerShell / Monitor 及其子进程；**只覆盖 shell 命令**，文件工具（Read/Write/Edit）、MCP、hooks 不在沙箱内。
- 默认**关闭**，需 `sandbox.enabled=true`。
- 默认边界：写只允许「cwd + 用户临时目录 + `--add-dir` 目录」；读默认放行大多数路径（含 `~/.ssh`、`~/.aws/credentials`）；网络**默认无出站**，需 `sandbox.network.allowedDomains`。
- **`sandbox.autoAllowBashIfSandboxed=true`：沙箱内命令免审批自动执行**，且独立于 `permission_mode`（官方原文：even in Manual mode，sandboxed Bash runs without prompting）。这正是「既解决审批阻断、又不全局放权」的开关。
- 容器场景有两个关键项：`sandbox.enableWeakerNestedSandbox`（「在无特权容器内运行 Linux 沙箱」）与 `sandbox.failIfUnavailable`（沙箱起不来时拒绝启动，而不是静默全放行）。
- 凭据防护：`sandbox.credentials.envVars/files` 可遮蔽环境变量与凭据文件；`sandbox.filesystem.denyRead/denyWrite` 收窄读写。

来源：
- <https://code.claude.com/docs/en/sandboxing>
- <https://code.claude.com/docs/en/settings-reference>（`sandbox.*` 字段清单）
- <https://code.claude.com/docs/en/permission-modes>
- <https://code.claude.com/docs/en/agent-sdk/permissions>

### 2.2 每任务隔离环境（用户设想的「venv」）

| 形态 | 代表 | 隔离级别 | 能否任务内装依赖 |
|---|---|---|---|
| 每任务容器 | container-use、Docker Sandboxes (`sbx`) | 容器 / microVM | 能 |
| 托管沙箱 SDK | E2B（Firecracker microVM）、Modal、Daytona | microVM / 托管容器 | 能，且有 egress 白名单与生命周期 |
| Dev Container / devbox / Nix | VS Code Dev Containers、devbox、Nix | 进程/文件系统 | 能，声明式、可复现 |

来源：
- <https://www.daytona.io/docs/en/linux-sandboxes/>
- <https://developertoolkit.ai/en/ecosystem/agent-tools/sandboxes/>

### 2.3 评测/编码平台的主流取舍：构建期预置，不是运行期自装

- SWE-bench：三层镜像（base / env / instance），依赖在**构建期**装好，运行时只应用补丁并跑测试。
- OpenHands：每个实例预构建 runtime image（含 runtime client），容器内提供 bash 与插件。
- Daytona/E2B：从 snapshot 启动，模板构建期装依赖。

来源：
- <https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/harness.md>
- <https://github.com/OpenHands/benchmarks/blob/main/benchmarks/swebench/README.md>
- <https://docs.openhands.dev/openhands/usage/architecture/runtime>

### 2.4 共性归纳

| 共性 | 本项目现状 | 差距 |
|---|---|---|
| 权限边界用 OS 强制，而非「命令白名单穷举」 | `permission_mode` + `disallowed_tools` 是工具层约定 | 缺 OS 级边界 |
| 沙箱内命令免审批，沙箱外仍需审批 | 要么全放行（bypass），要么全被拒（acceptEdits） | 缺中间态 |
| 依赖装在任务内（cwd），工具链预置在镜像 | 无任务级依赖目录 | 缺 workspace 内依赖约定 |
| 构建期预置优先，运行期安装受控 | 无安装策略 | 需要明确「能装什么、装到哪」 |
| 不可恢复失败要能识别并停下 | P0 已实现 | ✅ |

---

## 3. 拟采用设计

核心原则：**工具链预置在镜像，项目依赖装进 workspace；Bash 用 OS 沙箱约束并免审批；不再使用全局 `bypassPermissions`。**

### 3.1 分层设计

```
镜像层（构建期）
  ├─ node / npm / claude / codex / python（已有，保持全局）
  └─ 新增 bubblewrap + socat（Linux 沙箱依赖）
        │
        ▼
沙箱配置层（user-level settings，容器内共享）
  ├─ sandbox.enabled = true
  ├─ sandbox.autoAllowBashIfSandboxed = true     ← 沙箱内命令免审批
  ├─ sandbox.enableWeakerNestedSandbox = true    ← 无特权容器内可用
  ├─ sandbox.allowUnsandboxedCommands = false    ← 禁止跳出沙箱重试
  ├─ sandbox.network.allowedDomains = [npm/pypi/github...]
  └─ sandbox.credentials.envVars = [TDT_*, ANTHROPIC_*, ...]  ← 遮蔽密钥
        │
        ▼
任务层（workspace，per-mission）
  ├─ node_modules/、.venv/ 可写（cwd 默认允许）
  ├─ 全局 /usr/local 写入被 OS 拒绝（不影响他人）
  └─ 任务结束随 workspace 清理（沿用现有归档排除规则）
        │
        ▼
编排层（P0 已有）
  └─ environment_blocked 归因 + 第 1 轮自动暂停（沙箱起不来/网络断仍会被识别）
```

### 3.2 沙箱配置样例

```json
{
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": true,
    "enableWeakerNestedSandbox": true,
    "allowUnsandboxedCommands": false,
    "failIfUnavailable": false,
    "network": {
      "allowedDomains": [
        "registry.npmjs.org",
        "*.npmjs.org",
        "github.com",
        "codeload.github.com",
        "pypi.org",
        "files.pythonhosted.org"
      ]
    },
    "credentials": {
      "envVars": [
        "ANTHROPIC_API_KEY",
        "TDT_LLM_API_KEY",
        "TDT_DSH_API_KEY",
        "TDT_GIT_SECRET_KEY"
      ]
    },
    "filesystem": {
      "denyWrite": ["/usr/local", "/etc", "/data/*.sqlite"]
    }
  }
}
```

> 说明：`failIfUnavailable` 先设 `false` 并接入告警，验证稳定后再改 `true`；`enableWeakerNestedSandbox` 只在容器内 unprivileged 场景需要，实施时按实测决定。

### 3.3 注入方式

SDK 的 `settings` 参数是 `str | None`（设置文件路径），因此：

1. 配置项 `TDT_HARNESS_SETTINGS_FILE`（默认 `/etc/sortie/claude-sandbox.json`）；
2. 镜像构建时把 `docker/claude-sandbox.json` 复制到该路径；
3. `claude_code.py` 在 `ClaudeAgentOptions(...)` 中传 `settings=<path>`（为空则不传，保持现状）；
4. CLI 执行器路径同步传 `--settings <path>`；
5. 不再依赖 `~/.claude/settings.json` 的隐式加载（`harness_setting_sources` 保持现状）。

### 3.4 依赖安装策略（明确「能装什么、装到哪」）

| 允许 | 禁止 | 原因 |
|---|---|---|
| workspace 内 `npm install` / `pip install --target .venv` | `npm i -g`、`apt-get install`、写 `/usr/local` | 全局安装影响同容器所有任务 |
| 访问白名单域名（npm/pypi/github） | 任意域名出站 | 供应链与数据外泄 |
| 依赖落在 `workspace/node_modules` / `.venv` | 写入 `/data` 根、其他 mission 目录 | 跨任务污染 |

### 3.5 可观测与兜底

- `/api/health` 增加 `sandbox` 段（`enabled` / `bwrap_available` / `mode`）；
- worker 首个阶段 preflight：检查 `bwrap` / `socat`，缺失时 warning；`failIfUnavailable=true` 时直接失败而不是静默降级；
- P0 的 `environment_blocked` 分类与自动暂停保留：沙箱不可用、网络不通、磁盘满仍会被第 1 轮识别；
- 事件与 `stage_metrics.meta` 记录 `sandbox_mode`，便于复盘。

### 3.6 多 harness 兼容（claude_code / dsh / codex）

三家的「沙箱」不是同一层能力，不能照搬同一份配置，必须拆成 **公共策略意图 + provider 值域映射 + 容器兜底**。

#### 3.6.1 能力对照（2026-11-10 核对）

| 能力 | claude_code | dsh | codex |
|---|---|---|---|
| 档位值域 | `acceptEdits / auto / bypassPermissions / default / dontAsk / plan`（`permissions.py:19-21`） | `read-only / workspace-write / danger-full-access`（`dsh.py:89-97`） | 同 dsh 三值（`settings.py:106-108`） |
| 任务级 override | ✅ `pack["permission_mode"]`（`claude_code.py:228`） | ❌ 只读 settings（`dsh.py:89-97`） | ❌ 只读 settings（`codex.py:114`） |
| 沙箱内命令免审批 | ✅ `sandbox.autoAllowBashIfSandboxed` | 取决于 profile 的 ask 组合 | ✅ `--ask-for-approval never` / `-a never` |
| 可写根 | ✅ `add_dirs`（`claude_code.py:235-238`） | ❌ 单根（`dsh.py:365-366` `supports_extra_write_dirs=False`） | ⚠️ 仅首轮 `--add-dir`，resume 不可改（`codex.py:112-121`） |
| 网络出站控制 | ✅ `sandbox.network.allowedDomains` | ❌ **完全没有**（官方明确网络与读不在沙箱词汇内） | ✅ `sandbox_workspace_write.network_access` + `features.network_proxy.domains` |
| 读边界 / 凭据遮蔽 | ✅ `sandbox.filesystem.denyRead` + `sandbox.credentials.*` | ❌ 读不受限 | ❌ 沙箱只管写 |
| 工具禁用 | ✅ `disallowed_tools` | ⚠️ 整行禁用，`Read` 不可禁 | ❌ 完全不看 `context_pack`（`codex.py:94` 形参闲置） |
| 环境隔离 | ⚠️ `setting_sources` | ✅ 任务级 `DSH_HOME` | ❌ 整份 `os.environ` + 宿主 `~/.codex`（`codex.py:130`） |

来源：
- DSH sandbox 子系统（三档只治文件写，网络与进程可见性不在词汇内）：<https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.md>
- DSH 官方 issue 明确 `read-only` / `workspace-write` **不限制读与网络**：<https://github.com/deepseek-ai/deepseek-harness/discussions/8630>
- Codex 审批与沙箱（`--ask-for-approval never` 适用于所有 sandbox 模式；`workspace-write` 默认断网，需 `network_access=true`）：<https://learn.chatgpt.com/docs/agent-approvals-security>

#### 3.6.2 公共策略模型

在 `context_pack` 增加 `sandbox` 子字典（旧 provider 忽略未知键，向后兼容）：

```json
{
  "sandbox": {
    "mode": "workspace_write",
    "writable_roots": ["<workspace>"],
    "network": {"policy": "allowlist", "domains": ["registry.npmjs.org", "pypi.org"]},
    "deny_tools": ["Bash(rm -rf /*)"],
    "approval": "never"
  }
}
```

- `mode` 只用公共三档 `read_only | workspace_write | full_access`，由 `permissions.py` 从任务级 override / 全局配置解析后输出；
- 各 provider 负责把公共档位映射到自己的值域：
  - `claude_code`：`workspace_write` → `acceptEdits` + `sandbox.enabled`；`full_access` → `bypassPermissions`；
  - `dsh`：`workspace_write` → `DSH_PERMISSION_MODE=workspace-write`（已默认）；
  - `codex`：`workspace_write` → `--sandbox workspace-write`（已默认）。
- `network` / 读边界只是**意图声明**：只有 Claude 能真正兑现；Codex 能兑现到域名级；DSH 只能靠容器。

#### 3.6.3 各 provider 的具体做法

**claude_code（主实现，同上文 3.2）**

- 继续走 `settings` 文件注入：`sandbox.enabled` + `autoAllowBashIfSandboxed` + `network.allowedDomains` + `credentials.envVars`；
- `permission_mode` 回落 `acceptEdits`，不再依赖 `bypassPermissions`；
- preflight 检查 `bwrap` / `socat`。

**dsh（能力最弱，靠容器兜底）**

1. `dsh.py:89-97` 改为先读 `context_pack["sandbox"]["mode"]`，映射到三档；无 pack 时回落 `settings.dsh_permission_mode`。这补齐「任务级 override 对 dsh 无效」的缺口。
2. 保持 `DSH_HOME` 任务级隔离（`dsh.py:100-108`、`:480-484`）——这是 dsh 相对 codex 的优势。
3. **网络与读边界不要在 provider 层做**：官方明确三档只管写。唯一可靠边界是容器：
   - 容器级 egress 白名单（Docker network / iptables / 出站代理），放行 npm/pypi/github；
   - 凭据不落在 agent 可读路径（现在 `.env` 在宿主 `/data/sortie-deploy`，容器内不可见；进程环境变量仍需 `DSH_*` 只注入必要项）。
4. 依赖安装：`workspace-write` 下 cwd 可写，`npm install` 落在 workspace，天然满足「每任务依赖」；但**不能装全局**，且 `extra_dirs` 拿不到写权限（`dsh.py:365-366`），所以产物必须留在 workspace 或走现有归档兜底。

**codex（缺透传，需补齐参数）**

1. `codex.py:109-122` 补审批与网络参数（实施时以 `codex exec --help` 实测为准）：
   - `--ask-for-approval never`（或 `-c approval_policy="never"`）——非交互下不再卡审批；
   - `-c 'sandbox_workspace_write.network_access=true'` —— 允许装依赖时联网；
   - 进一步用 `-c 'features.network_proxy.domains={...}'` 做域名级 allow/deny（可选）。
2. `codex.py:94` 目前完全忽略 `context_pack`：补读 `sandbox.deny_tools`（映射到 `--config` 的 deny 规则或 prompt 约束）与 `permission_mode`（映射到三档）。
3. `codex.py:130` 整份 `os.environ` 透传 + 宿主 `~/.codex`：建议改为任务级 `CODEX_HOME`（复用 `DSH_HOME` 的做法），否则凭据与配置跨任务共享。
4. resume 限制：`codex exec resume` 不接受改沙箱档位（`codex.py:112`），所以**首次进入阶段时就按策略定档**，返工轮沿用。

**统一 preflight**

| harness | 自检命令 | 失败语义 |
|---|---|---|
| claude_code | `bwrap --version` + `socat -V` + `bwrap --unshare-user --dev-bind / / true` | 沙箱不可用 → `environment_blocked` |
| dsh | 跑一条 `workspace-write` 内写探针（写 workspace 临时文件再删） | 写被拒 → `environment_blocked` |
| codex | `codex exec --sandbox read-only` 跑 `true`（或 `--help` 校验参数存在） | 参数不支持 → 记 warning 并回落 |

#### 3.6.4 兼容结论

- **「依赖装进 workspace」三家都能统一**：三家沙箱都默认允许写 cwd，这是公共分母。
- **「沙箱内免审批」Claude 与 Codex 有原生开关，DSH 依赖 profile；但 DSH 的 `workspace-write` 已经比 `acceptEdits` 宽松**（写 workspace 不需要逐条审批），实际已接近目标。
- **网络与读边界只有 Claude 能原生兑现**，Codex 到域名级，DSH 完全没有 → 三层兜底顺序：provider 配置 → 容器 egress → `permissions` deny 规则。
- **不要试图用一个 `permission_mode` 字符串喂三家**：值域不同，必须先归一化成公共三档再映射。

---

## 4. 文件清单

| 绝对路径 | 改动 |
|---|---|
| `Dockerfile` | runtime-harness 阶段 `apt-get install -y bubblewrap socat`；复制 `docker/claude-sandbox.json` 到 `/etc/sortie/` |
| `docker/claude-sandbox.json` | **新增**：3.2 的沙箱配置 |
| `src/sortie_deck/settings.py` | 新增 `harness_settings_file`、`sandbox_enabled`、`sandbox_fail_if_unavailable` |
| `src/sortie_deck/harness_providers/claude_code.py` | `ClaudeAgentOptions(settings=...)`；`probe()` 增加 bwrap 探测 |
| `src/sortie_deck/plugins/coding.py` | CLI 路径加 `--settings`；prompt 声明依赖只能装进 workspace |
| `src/sortie_deck/harness_providers/executor.py` | `context_pack` 增加 `sandbox` 子字典；prompt 增加依赖安装边界；`meta` 记录 `sandbox_mode` |
| `src/sortie_deck/harness_providers/dsh.py` | 读 `pack["sandbox"]["mode"]` 覆盖 `DSH_PERMISSION_MODE`；网络/读边界留给容器 |
| `src/sortie_deck/harness_providers/codex.py` | 补 `--ask-for-approval never`、`sandbox_workspace_write.network_access`、可选 `features.network_proxy.domains`；读 `deny_tools`；任务级 `CODEX_HOME` |
| `src/sortie_deck/permissions.py` | 拆「公共三档」与 provider 值域映射；preflight 增加 `bwrap` / `socat` 与 dsh/codex 探针 |
| `src/sortie_deck/api/app.py` | `/api/health` 增加 sandbox 状态 |
| `.env.example` / `.env.docker.example` | 新增三项配置说明 |
| `tests/test_sandbox_settings.py` | **新增**：settings 透传、preflight、依赖边界 prompt |
| `tests/test_env_block_pause.py` | 补「沙箱不可用 → environment_blocked → 第 1 轮暂停」 |
| `CHANGELOG.md`、`docs/zh|en/modules/harness.md` | 行为与配置说明 |

---

## 5. 验证与验收

### 5.1 P0 前置验证结果（2026-11-10 实测，已完成）

宿主：Ubuntu 24.04.4 LTS，Docker 29.1.3，`kernel.unprivileged_userns_clone=1`，`user.max_user_namespaces=122238`，`kernel.apparmor_restrict_unprivileged_userns=1`。

| 容器参数组合（临时容器内装 bwrap 0.8.0 + socat） | 结果 |
|---|---|
| 默认参数 | `bwrap: No permissions to create new namespace` |
| 仅 `seccomp=unconfined` | `bwrap: Failed to make / slave: Permission denied` |
| `seccomp=unconfined` + `apparmor=unconfined`（无 cap） | `bwrap: setting up uid map: Permission denied` |
| 仅 `cap_add: SYS_ADMIN`（默认 seccomp/apparmor） | `Failed to make / slave: Permission denied` |
| `cap_add: SYS_ADMIN` + `apparmor=unconfined` | `CAP_AA_FAIL` |
| **`seccomp=unconfined` + `apparmor=unconfined` + `cap_add: SYS_ADMIN`** | **`--unshare-user` / 不 unshare / `--unshare-pid` 三组全部 OK** |

补充实测（非 root 进程，worker 以 uid 10001 运行）：

| 组合（宿主 sysctl=0） | 结果 |
|---|---|
| 仅 `seccomp=unconfined` | `Failed to make / slave` |
| 仅 `apparmor=unconfined` | `No permissions to create new namespace` |
| 仅 `cap_add: SYS_ADMIN` | `Failed to make / slave` |
| `seccomp=unconfined` + `apparmor=unconfined` | **成功，沙箱内仍为 uid 10001** |
| 给 `bwrap` 加 setuid | `capset failed`，不可用 |

已落地：
- `Dockerfile` 的 runtime-harness 阶段 `apt-get install bubblewrap socat`；
- `docker-compose.yml` 仅 `worker` 增加 `security_opt: [seccomp=unconfined, apparmor=unconfined]`，`api` 保持不变；
- 宿主持久化 `kernel.apparmor_restrict_unprivileged_userns=0`（`/etc/sysctl.d/99-sortie-userns.conf`）。

结论：`SYS_ADMIN` 不必要；`enableWeakerNestedSandbox` 底层仍是 bwrap，不解决 AppArmor 限制。

### 5.2 单测

- `ClaudeAgentOptions.settings` 收到配置路径；为空时不传；
- `permission_mode` 在沙箱模式下为 `acceptEdits`（不再 `bypassPermissions`）；
- prompt 含「依赖只能装进 workspace，禁止全局安装」；
- preflight 缺 `bwrap` 时返回 `False` 并只告警。

### 5.3 云端端到端回归

1. 沙箱内 `node --test repos/test/` **免审批**跑通；
2. `npm install` 产物出现在 `workspace/node_modules`；
3. 沙箱内 `touch /usr/local/x` 被拒；
4. 未在白名单的域名连接失败，白名单域名成功；
5. `env | grep -i api_key` 看不到 `ANTHROPIC_API_KEY`；
6. 临时把 `bwrap` 改名模拟缺失 → 任务**第 1 轮**暂停（验证 P0 与沙箱联动）；
7. 分别切 `dsh` / `codex` 跑一次编码阶段：依赖装进 workspace、全局写被拒、任务级 override 生效、resume 后档位不被改；
8. 全量 `pytest -q`、`ruff check`、`npm run typecheck`、`mkdocs build --strict`。

---

## 6. 备选方案

| 方案 | 优点 | 缺点 / 适用 |
|---|---|---|
| **A. 保持 `bypassPermissions`**（现状） | 零改动、本次已止血 | 权限面最大，agent 可写容器任意位置；不适合公网多用户 |
| **B. `acceptEdits` + `allowed_tools` 白名单** | 不引入 OS 依赖 | 命令形态多（`node --test`、`npm run`、`FOO=bar npm test`），穷举不完；本次已证明会漏 |
| **C. `acceptEdits` + `can_use_tool` 回调** | 精细、可编程 | 要维护命令规则与异步回调；沙箱起不来时是唯一可行退路 |
| **D. 任务级容器 / microVM（E2B、Daytona、Docker Sandboxes）** | 最彻底，真正的「每任务 venv」 | 工程量最大；需引入外部 SDK/编排；适合 P3 |
| **E. Dev Container / devbox / Nix** | 声明式、可复现 | 需要为每个技术栈维护环境定义，启动慢 |
| **F. 本方案**（沙箱 + 依赖进 workspace） | 改动中等、不引外部依赖、权限面显著收窄 | 依赖容器内 bubblewrap 可用性；只覆盖 Bash |

---

## 7. 风险与回滚

| 风险 | 处置 |
|---|---|
| 容器内 `bwrap` 因 seccomp/apparmor 起不来 | 已实测：宿主持久化 `kernel.apparmor_restrict_unprivileged_userns=0` + worker `security_opt: [seccomp=unconfined, apparmor=unconfined]` 即可，无需 `SYS_ADMIN`；`enableWeakerNestedSandbox` 不足以绕过 |
| 网络白名单漏配导致装包失败 | 白名单集中配置 + preflight 探测 registry；失败归为 `environment_blocked` 第 1 轮暂停 |
| 沙箱只覆盖 Bash，Read 仍能读文件 | 配合 `permissions.deny` 的 `Read` 规则遮蔽 `.env`；长期靠任务级容器 |
| 依赖装在 workspace 导致归档变大 | 沿用现有归档排除（`node_modules` 已排除），`.venv` 加入排除 |
| 切换沙箱后行为变化影响存量任务 | 灰度：先单任务验证，再全量；`TDT_HARNESS_SETTINGS_FILE` 置空即回滚到现状 |
| 沙箱降级静默全放行 | `failIfUnavailable` 在验证稳定后改 `true`；此前用 health + 事件告警 |

回滚：清空 `TDT_HARNESS_SETTINGS_FILE` 并重建容器即可恢复当前行为（仍可用 `bypassPermissions` 或方案 C）。

---

## 8. 实施顺序

1. **P0（前置验证，0.5-1 天）**：镜像装 `bubblewrap` + `socat`，容器内跑 5.1 全部检查，确认命名空间可用。
2. **P1（1-2 天）**：settings 注入 + 开沙箱 + 关掉 bypass + health/preflight + 单测；同时补 dsh / codex 的公共档位映射（`pack["sandbox"]["mode"]` 优先）。
3. **P2（1-2 天）**：依赖边界 prompt、workspace 依赖排除、云端端到端回归。
4. **P3（后续）**：任务级容器/microVM；`can_use_tool` 作为沙箱不可用时的退路。

---

## 9. 状态

**P0 与 P1 代码已完成（2026-11-10），待上线**。已完成：`Dockerfile` 安装 `bubblewrap` + `socat` 并分发 `docker/claude-sandbox.json`；`docker-compose.yml` 给 worker 加三件套权限；`settings.harness_settings_file` + SDK `settings` 透传；`sandbox_preflight()` + `/api/health` 沙箱状态；`context_pack["sandbox"]["mode"]` 公共档位与 dsh/codex 映射；codex 补 `--ask-for-approval` 与网络开关；依赖边界写入 prompt、归档排除 `.venv`/`dist`。测试：`test_sandbox_settings.py` 等新增 13 条，全量 `pytest` 1185 passed。未做：重建线上镜像并关闭全局 `bypassPermissions`、云端端到端回归、dsh 网络边界（只能靠容器 egress）。

本方案只做三件事：把 Bash 从「全放行/全拒绝」变成「沙箱内免审批」、把依赖安装收进 workspace、保留 P0 的环境阻塞止血。不承诺「SDK 自动修复环境」——工具链仍预置在镜像，运行时只允许装项目依赖。
