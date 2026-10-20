# Claude Code 的 Tool Search 机制详解

> 以 Claude Code 的 Tool Search 机制为主要参照（它是"模式 2"目前最完整的公开实现），把整个流程拆成六个阶段讲清楚。

## 〇、核心架构：三层结构

```text
┌─ 常驻层（每轮都在上下文）────────────────────────────┐
│ ① 各 server/skill 的一句话说明（如 "filesystem: 仓库读写"）│
│ ② 一个特殊工具 schema：tool_search(query) -> 候选列表    │
└──────────────────────────────────────────────────────┘
┌─ 索引层（模型外，服务端/客户端内存）─────────────────────┐
│ ③ 全量工具/skill 的元数据：name + description + tags     │
│    → 建成向量索引或关键词倒排（不占模型上下文）              │
└──────────────────────────────────────────────────────┘
┌─ 实体层（磁盘/远端）─────────────────────────────────┐
│ ④ 完整 schema（inputSchema/参数定义）                    │
│ ⑤ SKILL.md 正文 + scripts/                             │
│ ⑥ MCP server 进程 / CLI 可执行文件                      │
└──────────────────────────────────────────────────────┘
```

常驻成本是 **O(1)**（一个搜索工具 + 几条摘要），实体按需上浮。

## 一、阶段 A：启动装配（Deferred 注册）

1. **收集来源**：扫描三级配置（project `.mcp.json` / user 全局 / local），skill 目录同理。
2. **建立连接但不展开**：MCP server 按 stdio/SSE/HTTP 连接，客户端调 `tools/list` 拿到全量元数据——但不把 schema 放进模型工具表，而是标记 `deferLoading: true`（OpenAI Agents JS 的写法；Claude Code 里对应 Tool Search 开启时工具自动 defer）。
3. **建索引**：所有元数据（工具描述、skill 的 frontmatter description、server 说明）进入检索索引。
4. **注入常驻层**：模型工具表里只有两个东西——各来源的一行摘要 + `tool_search` 本身。个别必须每次可用的 server 设 `alwaysLoad: true`，跳过 defer 直通。

此时模型"知道世界上有这些能力域"，但不知道任何参数细节。

## 二、阶段 B：模型发起搜索（推理循环内）

模型执行任务中判断需要某个外部能力，生成一次工具调用：

```json
{"name": "tool_search", "arguments": {"query": "查询 postgres 慢查询日志", "limit": 5}}
```

**关键点**：query 是模型改写后的，不是任务原文——模型已理解意图，用接近工具描述的措辞提问。这就是比模式 1 准的原因。

## 三、阶段 C：检索执行（模型外）

客户端收到调用后，在索引层做匹配：

- **向量路线**：query embedding ↔ 元数据 embedding 余弦相似度，top-k
- **关键词路线**：分词倒排打分（本项目的 `recall()` 就是这个的缩微版）
- **混合**：向量召回 + 关键词过滤 + enable 状态/权限过滤（禁用项、allowlist 外的永远不出现）

返回的是**候选摘要**（不是 schema）：

```json
[{"id": "mcp__pg__query_slow_log", "description": "按时间窗检索慢查询...", "kind": "mcp"},
 {"id": "skill/log-analyzer", "description": "分析慢日志并给出索引建议", "kind": "skill"}]
```

## 四、阶段 D：实体上浮（schema 展开）

模型从候选中点名某个 id，该条目解冻：

- **普通 tool / MCP 工具**：完整 `inputSchema` 以 `tool_reference`（Claude Code / Agents SDK 的机制）追加进模型工具表，名字如 `mcp__pg__query_slow_log`。此后每轮上下文它都在场（直到会话结束或主动卸载）。
- **Skill**：上浮的不是 schema 而是动作指令——客户端把对应 `SKILL.md` 正文读入对话（或指示模型去读），skill 附带的 scripts 保持"只执行、不读入"。
- **描述截断**：条目 description 过长会被截到约 2KB，防单个条目污染上下文。

模型至此才第一次看到参数定义，进入正常的 function calling 流程。

## 五、阶段 E：实际调用（执行与权限门）

```text
模型 tool_use(mcp__pg__query_slow_log, args)
  → 客户端权限检查：allowedTools 白名单 / read_only 写操作确认门
  → 路由到对应 server（MCP 协议 call / bash 执行脚本）
  → 结果回填 tool_result（输出超约 25k token 会截断告警）
  → 模型继续推理；若结果不匹配预期，回到阶段 B 换词重搜
```

最后一步是模式 2 的精髓：**调用失败/不合适 → 回到搜索 → 换候选，形成闭环**，模式 1 没有这条回路。

## 六、阶段 F：生命周期管理

- **会话内缓存**：解冻过的工具在本次会话持续可用，不重复搜索
- **失效处理**：MCP server 断连时其工具标记不可用，模型再调用得到明确错误而非静默失败
- **预算控制**：deferred 工具总量、单次搜索候选数、输出上限都有阈值，防上下文膨胀

## 七、时序全景

```text
启动    : 扫描配置 → 连接 server → tools/list → 建索引 → 注入「摘要+tool_search」
任务开始: 模型推理 ──缺能力──▶ tool_search(query)
        ◀── 候选摘要（k 条）
        模型点名 ──▶ schema 解冻注入 / SKILL.md 载入
        模型 tool_use ──▶ 权限门 ──▶ server 执行 ──▶ tool_result
        结果不合适? ──是──▶ 回到 tool_search（闭环）
        ──否──▶ 继续，直至任务完成
```

## 八、和另外两类对象的统一性

tool / MCP / skill 在这套机制里只是**实体层形态不同、上浮方式不同**：

| 类型 | 索引层放什么 | 上浮时发生什么 | 执行方式 |
|---|---|---|---|
| tool（function） | 名称+描述 | 注入 inputSchema | 客户端直接执行 |
| MCP 工具 | server 摘要+工具描述 | 注入 inputSchema（`mcp__x__y`） | 路由到 MCP server |
| skill | frontmatter description | 载入 SKILL.md 正文 | 模型按正文操作，scripts 由 bash 执行 |

## 九、本项目的落地路径

本项目若要补齐模式 2，路径很短：

- `ToolkitStore` 的元数据就是现成索引（甚至可以加个 embedding 字段升级向量检索）；
- 再暴露一个 `/api` 之上的 `toolkit_search` 元工具；
- 按上面的 D→E 加 schema 解冻和权限门即可——`mcp_runtime.py` 的 `McpToolInvoker` 已经承担了 E 阶段的执行与确认门。
