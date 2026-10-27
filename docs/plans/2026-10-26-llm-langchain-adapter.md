# LLM 接入层迁移到 LangChain + 建队瀑布图化

- 日期：2026-10-26
- 状态：已完成
- 范围：`src/sortie_deck/llm.py`、`llm_chain.py`、`harness/waterfall.py`、`mission_briefing.py`

## 一、需求背景

项目虽已用 LangGraph 跑流水线（`graph.py`），但所有大模型调用仍是手写 httpx 直连
`/chat/completions`，没有走 LangChain 的 ChatModel 抽象。目标是统一到 LangChain 生态，
分两档：

- **A 档**：`LlmClient` 内部换成 LangChain ChatModel，业务调用方零改动。
- **B 档**：自研 `harness` 的工具编排循环改为 LangGraph 图。

**明确排除**：`harness_providers/`（Claude Code / OpenCode / Codex / Cursor）与
`plugins/coding.py` 的 CLI 型编码 Agent **不改**。这类能力依赖本地 CLI 登录态、
订阅额度与真实仓库文件操作，改成 LangChain Agent 必须走 API key，属产品能力降级。

## 二、业内调研

| 结论 | 来源 |
|------|------|
| LangGraph 可独立于 LangChain 使用，两者定位不同（编排引擎 vs 组件抽象层） | [LangGraph FAQ](https://langgraph.com.cn/concepts/faq/index.html) |
| LangChain 1.0 起底层已基于 LangGraph Runtime，标准生产栈为 LangChain（组件）+ LangGraph（调度）+ LangSmith（观测） | [博客园：LangChain / LangGraph / LangSmith 定位区分](https://www.cnblogs.com/cyjcyjcyj/p/22200397) |
| CLI 型 agent 没有官方 LangChain 集成，社区包 `langchain-claude-agent-sdk` 是 Claude Agent SDK 的 chat-model adapter | [PyPI: langchain-claude-agent-sdk](https://pypi.org/project/langchain-claude-agent-sdk/0.2.0) |

归纳：**LLM 调用层适合换 LangChain，CLI agent 层不适合**。本项目与主流做法一致，
只迁移前者。

## 三、技术设计

### A 档：`llm_chain.py` + `llm.py` 路由

```text
18 个调用点 → LlmClient.chat_json / chat_text
              ├─ api_key     → LangChainBackend → ChatOpenAI
              └─ 无 api_key  → claude -p CLI 兜底（原样保留）
```

关键取舍：

1. **不保留 httpx 实现，也不留 `TDT_LLM_IMPL` 开关**。开关会让 `impl` 参数常驻构造
   签名，后续维护者容易误以为仍是可选配置；旧实现失去测试覆盖后就是死代码。
   回滚统一走 `git revert`。
2. **不用 `with_structured_output` 强 schema**。18 个调用点的 JSON key 写在 prompt 里
   （如 `pipeline_llm.py:18-31`），强 schema 要给每个点定义 Pydantic 模型，
   就破坏了「调用方零改动」；且大量 OpenAI 兼容端点不支持 `json_object`。
   改为保留 `chat_json` 现有的围栏剥离逻辑。
3. **`response_format` 不支持时降级重试一次**，不做通用重试——避免重复计费与放大故障。
   其余异常统一包成 `RuntimeError`，上游 `except Exception` 兜底逻辑（如
   `pipeline_llm.py:164-167`）无需改动。
4. **`backend` 仍返回 `http`**。该值被 13 处写进 UI 文案与 meta
   （`pipeline_llm.py:185/189/236/306`、`plugins/deploy.py:78/86`、
   `mission_briefing.py:257/272`、`skill_install.py:145`、`api/app.py:1646`）。
5. **后端实例按连接参数缓存**（`get_backend`），复用底层连接池；key 用 api_key 的
   sha256 前缀，避免明文驻留字典键。业务侧 18 处 `LlmClient()` 每次新建实例，
   只有跨实例缓存才真正省掉 TLS 握手。

### B 档：`harness/waterfall.py`

现状：`Harness`（`harness/__init__.py:95-160`）**不含 LLM 调用**，只是事件 + 工具容器；
真正的五步瀑布硬编码在接口层 `mission_briefing.py:404-423`。

```text
START → search_history → retrieve_knowledge → propose_pipeline
      → propose_loadout → ensure_missing_roles → END
```

关键取舍：

1. **不用 `create_react_agent`**。前端
   `apps/web/src/components/MissionBriefingChat.tsx` 依赖固定 `data.step` 序列渲染
   瀑布动画，`ensure_missing_roles` 还有建角色副作用。让模型自主决定工具顺序与次数
   属行为变更，要连带重写前端与测试，收益为负。
2. **harness 通过 `config["configurable"]` 注入**，不放进图状态，避免每次调用
   重新编译图。
3. **`context` 不放图状态**。节点直接写 `HarnessState.context` 这个 dict 对象，
   规避 LangGraph 状态复制导致 `draft` 读不到工具结果。
4. **不挂 checkpointer**。会话持久化仍由 `BriefingStore` 承担（`mission_briefing.py:64-78`），
   双写会产生两份真相。
5. **不开 LangGraph retry**。`ensure_missing_roles` 有副作用，重试会产生重复草稿。
6. 工具结果映射抽成纯函数 `apply_tool_result(context, name, data)`，便于单测。

## 四、文件清单

| 文件 | 变更 |
|------|------|
| `pyproject.toml` | 主依赖新增 `langchain-openai>=1.0.0`；`httpx` 保留（另有 9 个模块在用） |
| `src/sortie_deck/llm_chain.py` | 新增：`LangChainBackend`、`get_backend` 缓存、`reset_backend_cache` |
| `src/sortie_deck/llm.py` | 新增 `_chat_chain`；删除 httpx 直连 `_chat_http`；`_chat_claude_cli` 保留 |
| `src/sortie_deck/harness/waterfall.py` | 新增：五步 `StateGraph`、`apply_tool_result`、`run_waterfall` |
| `src/sortie_deck/mission_briefing.py` | 删除硬编码瀑布与 `run_tool` 闭包，改调 `run_waterfall` |
| `tests/test_llm_langchain.py` | 新增：7 个用例（路由 / 围栏 / 降级 / 异常归一化 / httpx 已移除） |
| `tests/test_harness_waterfall.py` | 新增：6 个用例（顺序 / 事件 / 参数 / 映射 / 失败容忍） |
| `docs/{zh,en}/modules/harness.md` | 补充瀑布图小节与契约说明 |
| `CHANGELOG.md` | `[Unreleased] / Changed` 记录两条 |

## 五、备选方案（未采用）

| 方案 | 不采用原因 |
|------|-----------|
| `with_structured_output` + 每个调用点定义 Pydantic schema | 破坏「调用方零改动」，工作量从 1 天涨到 3-4 天，且需为 18 个 prompt 维护模型 |
| `create_react_agent` 自由 tool-calling | 改变前端依赖的确定性事件序列，属行为变更 |
| 把 `harness_providers/` 的 CLI agent 换成 LangChain Agent | 丢掉 CLI 登录态、本地仓库操作与订阅额度复用，产品能力降级 |
| 图挂 checkpointer 复用 `BriefingStore` | 双写、两套恢复语义，收益不抵复杂度 |

## 六、验证结果

- `pytest -q`：**512 passed**（含新增 13 个用例：`test_llm_langchain.py` 7 个 +
  `test_harness_waterfall.py` 6 个；既有 `test_mission_briefing.py` /
  `test_pipeline_llm.py` / `test_room_llm.py` / `test_harness.py` 全部通过）
- `ruff check src tests`：24 errors（基线 25，`llm.py` 的 I001 顺带修复；其余均为
  既有债务，本次新增文件零告警）
- `npm run typecheck`：通过（前端未改动，作无回归确认）
- `mkdocs build --strict`：仅剩 3 个既有坏链 warning（`zh/design/*` 指向
  `TODO.md` / `SECURITY.md`），与本次无关

## 七、回滚方式

- A 档：`git revert` 对应提交。httpx 直连已彻底删除，没有运行时开关可回退。
- B 档：`git revert` 对应提交即可，`harness/__init__.py` 的事件与工具契约未变，
  前端与存储格式不受影响。
