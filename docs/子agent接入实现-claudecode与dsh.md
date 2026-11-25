# 子 Agent 接入实现（claude_code / dsh）

- 版本：2026-11-25
- 范围：本项目如何给 harness 供给子代理、如何观测委派、各家的能力边界
- 关联：`docs/plans/2026-11-24-全局子代理池.md`（方案）、`docs/子代理与自审会话隔离-问答与解决方案.md`（决策记录）

## 0. 一句话

编排层只做**供给 + 契约 + 观测 + 护栏**；「何时委派、派给谁、传什么 prompt、怎么用返回值」全部由 harness 自己的父模型决定，我们插不进去。

```
模板/StageContext ──> resolve_subagents() ──> StageSubagentPlan
                                                    │
              ┌─────────────────────────────────────┼──────────────────────┐
        claude_code: SDK agents            codex: .codex/agents/*.toml     dsh: 只能提示词引导
        （native）                          （native）                      （prompt_only）
```

## 1. 分层：供给在编排层，决策在 harness 内

| 层 | 谁负责 | 本项目做什么 | 本项目不做什么 |
|---|---|---|---|
| 供给 | 编排层 | 决定池里有哪些 agent、各带什么工具、多少步上限 | 不决定本次是否委派 |
| 决策 | harness 父模型 | 读 `description` 选 agent、写委派 prompt | 不决定池外还有谁 |
| 执行 | 子代理 | 独立上下文跑完，只回最终报告 | 看不到父对话历史 |
| 观测 | provider + 执行器 | 数委派次数、子会话、终止原因 | 拿不到子代理内部完整链路（各家不同） |

技术约束决定了这个分界：SDK 的 `agents` 是 `query()` 的**启动参数**，`.claude/agents/*.md` 是**启动时扫描**。会话内无法新增 agent 定义，所以「动态」只发生在**选择**这一层，供给必须提前按任务准备好。

## 2. 数据流

```
packages/templates/*.yaml (stage.subagents)
        │  graph.py 透传
        ▼
StageContext.subagents: list[dict]
        │  executor._build_context_pack()
        ▼
pack["subagents"] = resolve_subagents(ctx, declared=...).model_dump()
        │
        ├─ pack["subagents"]["agents"]   → provider 渲染成各家原生定义
        └─ pack["subagents"]["guidance"] → _build_prompt() 追加到最后一段
```

三处入口：

```2171:2177:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/executor.py
        # 子代理池：默认只有只读 scout（业务语义由模板声明补充）。
        # 开关关闭时 agents 为空，prompt 与 provider 两侧都不产生任何变化。
        from sortie_deck.subagents import resolve_subagents

        out["subagents"] = resolve_subagents(
            ctx, declared=list(getattr(ctx, "subagents", None) or [])
        ).model_dump()
        return out
```

```2396:2401:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/executor.py
        # 子代理委派指引：只在池非空时注入（关闭开关 / 无只读能力时完全不出现在 prompt 里）
        subagent_guidance = str((pack.get("subagents") or {}).get("guidance") or "")
        if subagent_guidance.strip():
            sections.append(subagent_guidance)
```

**关键设计：池为空时，prompt 与 provider 两侧都不产生任何字节变化。** 这是开关可随时置 false 整体回滚的原因（开关自 2026-11-25 起默认常开）。

## 3. canonical spec 与派生规则

`subagents.py` 定义与 harness 无关的中间表示：

| 概念 | 字段 | 说明 |
|---|---|---|
| `SubagentSpec` | `id` / `description` / `prompt` / `capabilities` / `mode` / `max_turns` / `model` | 能力用标签表达，不写具体工具名 |
| `StageSubagentPlan` | `agents` / `guidance` / `support` / `notes` / `stage` / `harness` | 一个阶段的完整计划 |

能力标签到工具的映射（`:123`）：

| capability | claude_code | codex | dsh |
|---|---|---|---|
| `read_files` | `Read` | 继承父级 | 继承父级 |
| `search_code` | `Grep` / `Glob` | 继承 | 继承 |
| `web` | `WebSearch` / `WebFetch` | 继承 | 继承 |
| `shell` | `Bash` | 继承 | 继承 |
| `write_files` | `Write` / `Edit` | 继承 | 继承 |

派生规则（`resolve_subagents`，`:213`）：

1. `TDT_SUBAGENT_ENABLED=false` → 空池，`support="disabled"`；
2. harness 在 `SUPPORT_BY_HARNESS` 里为 `unsupported`（cursor / mock）→ 空池，**不注入指引**；
3. 默认池只有 `scout`，能力 = 本阶段可用只读能力 ∩ `{read_files, search_code, web}`；
4. 模板声明的 agent 追加在后，`mode=readonly` 时**强制剔除** `shell` / `write_files`；
5. `mode=write` 且 `TDT_SUBAGENT_ALLOW_WRITE=false` → 降级为只读并记 note；
6. 所有 `description` 合计超 `TDT_SUBAGENT_DESCRIPTION_BUDGET`（默认 12000 字符）→ 按声明序截断；
7. `guidance` 只在池非空时渲染，内容是"何时该委派 / 必须写明目标与验收 / 不要委派给自己能两步做完的事"。

阶段工具策略会自动收缩池：某 stage 禁了 `Read`，scout 就不再声称有 `read_files` —— 不给"一调就被拒"的子代理。

## 4. claude_code：native 注入

### 4.1 渲染

`_agent_definitions()`（`claude_code.py:54`）把 canonical spec 翻成 SDK 的 `AgentDefinition`：

```54:82:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
def _agent_definitions(subagents: dict[str, Any], disallowed: list[str]) -> dict[str, Any]:
    """把编排层的 canonical 子代理计划渲染成 SDK ``AgentDefinition`` 映射。

    输入：``pack["subagents"]``（见 ``sortie_deck.subagents``）+ 本阶段禁用工具。
    输出：``{agent_id: AgentDefinition}``；空 dict 表示本阶段不提供子代理。

    只做两件事：能力标签 → Claude 工具名、剔除被阶段禁用的工具。路由描述与提示词
    原样透传——那是父模型判断"何时委派"的唯一信号，provider 不得改写。
    单条渲染失败只丢该条并告警：子代理是增强项，不能打挂阶段。
    """
```

注入点在 options 组装处，且先判断 SDK 是否有 `agents` 字段（旧 SDK 零回归）：

```313:321:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
        # 子代理池（默认仅只读 scout）：SDK 有 agents 字段才传，旧 SDK 忽略（零回归）
        agent_defs = _agent_definitions(pack.get("subagents") or {}, disallowed)
        if agent_defs and "agents" in getattr(ClaudeAgentOptions, "__dataclass_fields__", {}):
            options_kwargs["agents"] = agent_defs
            logger.info(
                "注入子代理: session=%s agents=%s",
                session.id,
                ",".join(sorted(agent_defs)),
            )
```

`_harness_env()`（`:103`）额外处理内置 agent：`TDT_SUBAGENT_DISABLE_BUILTIN=true` 时设 `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`。默认**保留**内置 `Explore`（代码任务多一个免费选项）。

### 4.2 观测

三个信号，全部落进 `meta["context"]["subagents"]`：

| 字段 | 来源 | 回答什么 |
|---|---|---|
| `task_tool` | `SystemMessage(subtype="init").data["tools"]` 是否含 `Task`/`Agent` | 本次会话到底有没有委派能力位 |
| `calls` / `agents` | `ToolUseBlock.name in {Task, Agent}` + `subagent_type` | 委派几次、派给谁 |
| `steps` | `AssistantMessage.parent_tool_use_id` 非空 | 子代理内部 assistant 消息数 |

```393:399:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
            elif isinstance(message, SystemMessage):
                subtype = str(getattr(message, "subtype", "") or "")
                # init 事件带本次会话的工具清单：据此判断委派能力位是否真的开着，
                # 而不是假设「SDK 装了就有 Task 工具」
                if subtype == "init":
                    data = getattr(message, "data", None) or {}
                    tools = {str(t) for t in (data.get("tools") or []) if isinstance(t, str)}
                    subagent_meta["task_tool"] = bool(tools & {"Task", "Agent"})
```

**实测限制：`steps` 恒为 0。** Agent SDK 只回传子代理最终报告，不把子代理内部的 assistant 回合推进主流，所以 `parent_tool_use_id` 非空的消息根本不存在。这个字段不要当"子代理工作量"用，只能看 `calls` 与子代理侧时长。

### 4.3 降级

- SDK 无 `AgentDefinition` → 打日志跳过注入，阶段照常；
- 单条 spec 渲染失败 → 只丢该条并告警；
- `disallowed` 里的工具从子代理工具集中剔除，避免"注入了但一调就拒"。

## 5. dsh：prompt_only + 事件观测

### 5.1 为什么不能像 claude 那样注入

dsh runtime 的自定义子代理能力是**插件级 Config**，不是模型可选的池：

- 工具入参只有 `description` / `prompt` / `run_in_background`；
- `persona` / `toolFilter` / `maxDepth` / `agentOptions` 属于 `tool-subagent` 的 Config，一次配置对**全部**子代理生效；
- 因此 dsh 上做不到"从多个命名 agent 里按 description 路由"，只能"统一引导它用自己的内置 `subagent`"。

代码里显式承认这一点，不静默假装：

```692:702:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/dsh.py
        subagent_rows = ((context_pack or {}).get("subagents") or {}).get("agents") or []
        if subagent_rows:
            # 不静默：DSH 的自定义子代理定义机制尚未验证（见计划 P4），
            # 当前只能靠提示词引导它用自带的 tool-subagent，无法保证按池选择
            logger.warning(
                "DSH 暂不支持自定义子代理定义，仅提示词引导: initiative=%s stage=%s agents=%s",
                (session.meta or {}).get("initiative_id"),
                (session.meta or {}).get("stage"),
                ",".join(str(r.get("id") or "") for r in subagent_rows if isinstance(r, dict)),
            )
```

### 5.2 能拿到的比 claude 更细

dsh 有 `subagent.started` / `subagent.finished` 生命周期通知。notifier 在 worker 线程累积，回合结束并入 meta：

```967:979:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/dsh.py
def _record_subagent_notification(method: str, payload: Any, acc: dict[str, Any]) -> None:
    """累积 dsh 的子代理生命周期事件（``subagent.started`` / ``subagent.finished``）。

    实测 payload（SDK 0.2.1rc2，2026-11-25）：

    - ``subagent.started``: ``{parentSessionId, childSessionId}``
    - ``subagent.finished``: ``{provider, agentId, parentSessionId, childSessionId,
      status, stopReason, lastAssistantMessage}``
    """
```

输出形状：

```json
{
  "calls": 2,
  "finished": 0,
  "providers": ["spawn"],
  "children": ["6d0a4453-..."],
  "stop_reasons": ["completed"]
}
```

三个实测要点：

1. 子会话 id 必须取 `childSessionId` / `agentId`——`started` 事件里**没有** `provider`，`id` / `session_id` 这两个键也不存在（早期实现按它们取值，`children` 永远为空）；
2. `finished=0` 是正常的：dsh 子代理默认后台运行，主回合结束时可能还没收到完成事件；
3. `failed` 只在 `status != "ok"` 时出现，`stop_reasons` 记录 `completed` / `error` 等。

## 6. 开关与支持矩阵

```302:321:/Library/git-root/sortie-deck-todos/src/sortie_deck/settings.py
    # 开启后默认只供给只读 scout；写型子代理必须由模板声明且放行 subagent_allow_write。
    subagent_enabled: bool = True
    # 是否允许模板声明 mode=write 的子代理（同一 worktree 多写者会互相覆盖，默认禁止）
    subagent_allow_write: bool = False
    # spec 未指定 max_turns 时的兜底上限：防止子代理无限循环放大成本
    subagent_max_turns: int = 12
    # 单阶段允许的委派次数（只在提示词里约束，harness 侧无法硬拦）
    subagent_max_calls_per_stage: int = 6
    # 所有子代理 description 的字符预算：Claude 侧合计超 15k token 会告警，
    # 超出即按声明序截断（宁可少给，不要撞预算）
    subagent_description_budget: int = 12_000
    # 是否允许只读池并行；写型池无论该值如何都不并行
    subagent_parallel: bool = True
    # 是否关闭 harness 内置子代理（claude_code: CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS）。
    # 默认保留内置 Explore——代码任务多一个免费选项；若观测到与 scout 抢路由再打开。
    subagent_disable_builtin: bool = False
```

| harness | support | 供给方式 | 观测能力 | 工具裁剪 |
|---|---|---|---|---|
| claude_code | `native` | SDK `agents` 参数 | 委派次数 / 子会话 / 能力位（`steps` 恒 0） | ✅ 按 agent |
| codex | `native` | `<cwd>/.codex/agents/*.toml` | 仅 `collab_tool_call` 活动次数，**拿不到 agent 名** | ❌ 只继承父级 |
| dsh | `prompt_only` | 只能提示词引导 | 委派次数 / 子会话 / provider / stopReason | ❌ 插件级统一配置 |
| opencode | `native` | 未做适配 | — | — |
| cursor / mock | `unsupported` | 空池，不注入指引 | — | — |

`unsupported` 必须空池：给了指引模型会"假装委派"并编造子代理的返回结果（codex 探针时观察到的真实失败模式）。

## 7. 自审轮与写型约束

| 约束 | 实现 | 原因 |
|---|---|---|
| 自审轮池强制为空 | `review_pack = {..., "subagents": {}}` | 自审已占阶段耗时大头，再嵌套委派会让成本无法归因 |
| 写型默认禁止 | `subagent_allow_write=false` | 并行波次已负责并行，同 worktree 多写者互相覆盖 |
| parallel 只管只读 | `subagent_parallel` 写型池无视 | 写型并行没有安全边界 |

## 8. 端到端实测（2026-11-25，本地）

做法：`TDT_SUBAGENT_ENABLED=1` 启动本地 API（inline 后端），express track 建任务，brief 显式要求"并行派发两个子代理分别评估 add / mul 接口"。

| harness | 任务 | 观测 |
|---|---|---|
| dsh | `ini_33bfee3c5837` | `子代理用量(DSH): calls=2 finished=0 providers=- children=2` |
| claude_code | `ini_353dfacb3eef` | 两条 `子代理委派` 相隔 27ms（12:09:48.854 / .881，purpose 分别是 add / mul），`calls=2 agents=scout` |

结论：两家都能按 brief 并行派发，且都拿到独立子会话。

三个环境坑（与功能无关，但会让人误判"功能坏了"）：

1. `~/.claude/settings.json` 的 `env` 优先级高于进程环境变量：本地该文件里 `ANTHROPIC_BASE_URL=http://127.0.0.1:15721` 指向未启动的代理，导致 CLI `ECONNREFUSED`。验证时用 `TDT_HARNESS_SETTING_SOURCES=none` 隔离 user settings。
2. pydantic-settings 读 `.env` **不会 export 到进程环境**：dsh 走参数传递没事，claude CLI 是子进程只认真实 env，启动服务需 `set -a; source .env; set +a`。
3. `source .env` 后跑测试会带入真实 key，`tests/test_llm_offline.py::test_cli_fallback_switch_is_respected` 会失败——用 `env -u TDT_LLM_API_KEY` 跑。

## 9. 已知限制

| # | 限制 | 影响 | 后续 |
|---|---|---|---|
| 1 | dsh 无法按池路由 | 只能统一引导，多个命名 agent 不可用 | 需 patch `tool-subagent` 的统一 persona/toolFilter，属另一个产品决策 |
| 2 | codex 拿不到被委派 agent 名 | 归因只能到"次数" | 等 codex 把 `spawn_agent` 暴露进 `--json` 流 |
| 3 | claude `steps` 恒 0 | 无法量化子代理工作量 | 依赖 `calls` + 子代理侧时长 |
| 4 | `max_calls_per_stage` 只是提示词约束 | harness 侧无法硬拦 | 靠 `max_turns` 兜底 |
| 5 | 写型子代理默认关 | 并行改代码不可用 | 需 worktree 级隔离才安全 |

## 10. 代码索引

| 位置 | 职责 |
|---|---|
| `src/sortie_deck/subagents.py:33` | `SUPPORT_BY_HARNESS` 能力矩阵 |
| `src/sortie_deck/subagents.py:123` | capability → 各 harness 工具名 |
| `src/sortie_deck/subagents.py:205` | 委派指引渲染 |
| `src/sortie_deck/subagents.py:213` | `resolve_subagents()` 池派生 |
| `src/sortie_deck/harness_providers/executor.py:2172` | 注入 `pack["subagents"]` |
| `src/sortie_deck/harness_providers/executor.py:2397` | prompt 注入委派指引 |
| `src/sortie_deck/harness_providers/executor.py:1850` | 自审轮空池 |
| `src/sortie_deck/harness_providers/claude_code.py:54` | `_agent_definitions()` 渲染 |
| `src/sortie_deck/harness_providers/claude_code.py:314` | 注入 SDK `agents` |
| `src/sortie_deck/harness_providers/claude_code.py:334` | `subagent_meta` 初始化 |
| `src/sortie_deck/harness_providers/claude_code.py:396` | `system:init` 读工具清单 |
| `src/sortie_deck/harness_providers/dsh.py:691` | prompt_only 显式告警 |
| `src/sortie_deck/harness_providers/dsh.py:967` | `_record_subagent_notification()` |
| `src/sortie_deck/harness_providers/dsh.py:917` | 子代理用量日志 |
| `src/sortie_deck/harness_providers/codex.py:61` | `_codex_agent_name()` 命名收敛 |
| `src/sortie_deck/harness_providers/codex.py:97` | `ensure_subagent_files()` 落盘 |
| `src/sortie_deck/harness_providers/codex.py:325` | `_collab_stats()` 观测 |
| `src/sortie_deck/settings.py:304` | 7 个开关 |
| `packages/templates/research_pipeline.yaml:31` | `web-researcher` 声明样例 |
| `packages/templates/office_pipeline.yaml:32` | `doc-reader` 声明样例 |
