# 计划：API 密钥独立页 · 有效期 · 多客户端接入配置

- 日期：2026-10-31
- 状态：**已实施**（选定 v2「通行证」；见 §10 实施结果）
- 关联：[`2026-10-30-mcp-apikey-cli.md`](./2026-10-30-mcp-apikey-cli.md)（API Key 与 MCP 服务端的初版实现）

---

## 1. 需求

### 1.1 用户原话拆解

| 诉求 | 落地要点 |
|---|---|
| 现在的密钥页太难看 | 从顶栏弹窗升级为独立页面，与「决策中心 / 管理」同级挂在主导航 |
| 设计三个页面供选择 | `design-demos/apikeys-v{1,2,3}-*.html` 三版可交互高保真，选定后再实现 |
| 点击后直接进行密钥管理 | 导航直达列表页，不再需要先点弹窗按钮 |
| 删除后直接删除数据库 | `DELETE` 改为物理删除（现在是 `revoked_at` 软吊销） |
| 新建存 SQLite、按人隔离、一人多把 | 现有 `user_id` 隔离与一对多已满足，沿用 |
| 可设有效期，默认 1 个月，1/3/6/12 个月 | 新增 `expires_at` 列，签发时选择 1 / 3 / 6 / 12 个月 |
| 创建后显示 MCP 配置，不只 Claude Code | 同一把 Key 生成 Claude Code / Codex / Cursor / OpenCode 四套片段 |
| 也提供 `claude mcp add ...` 命令 | 保留为 Claude Code 页签下的「命令行」视图 |

### 1.2 非目标

- 不做 scope 细分（v1 仍是 `scopes = ["*"]`，列已预留）。
- 不做 Key 轮换向导（双 Key 重叠期）；本期只做「删除即失效」。
- 不做用量配额 / 计费，仅在页面上展示 `last_used_at` 与近 24h 调用次数（数据来自现有 `last_used_at` 粒度，不新增埋点）。

---

## 2. 业内调研

### 2.1 凭证签发页的通用做法

| 结论 | 依据 |
|---|---|
| 明文只展示一次，列表只留前缀；库里存哈希 | [apikeys.guide · For API Providers](https://apikeys.guide/docs/best-practices/for-providers)：CSPRNG 生成、可识别前缀、单独 non-secret 标识便于查找 |
| 新 Key 默认应「窄权限 + 有限寿命」，而不是永久有效 | [Safeguard · Safe defaults for new API keys](https://safeguard.sh/resources/blog/safe-defaults-for-new-api-keys-scope-and-expiry)：默认最窄 scope + 明确过期时间；OWASP 同类建议 |
| 过期与删除是两种语义，列表要能看出「已过期」 | [AverageDevs · Design Customer-Facing API Keys](https://averagedevs.com/blog/design-customer-facing-api-keys-saas)：展示一次、前缀查找、scoped、双 Key 轮换、泄露响应 |
| 远程 MCP 产品统一形态：一个 endpoint + 一把 Key + 按客户端分页签给配置 | [Hyperping Agent setup](https://hyperping.com/docs/agent-setup)、[LiveContext MCP](https://livecontext.ai/docs/mcp-server)、[Flux MCP](https://flux.umin.ai/mcp) 均为此结构 |

**对本项目的直接影响**

1. 有效期必须有「到期日」这个一等公民字段，列表里要能看到剩余天数，而不是只显示创建时间。
2. 配置区从「一段 JSON」升级为「客户端页签 + 目标文件路径 + 复制」，这是同类产品的标准形态。
3. 删除按钮的文案必须写清后果（立即断开、不可恢复），而不是一句「确定吗」。

### 2.2 四个客户端的确切配置格式（已核对官方文档）

| 客户端 | 目标文件 | 远程 HTTP 写法 | 来源 |
|---|---|---|---|
| Claude Code | 项目根 `.mcp.json`（也可 `claude mcp add`） | `{"mcpServers":{"sortie-deck":{"type":"http","url":"...","headers":{"Authorization":"Bearer ..."}}}}`；`type` 必填，缺省会按 stdio 处理 | [Claude Code MCP 文档](https://code.claude.com/docs/en/mcp) |
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.sortie-deck]` + `url` + `http_headers = { Authorization = "Bearer ..." }`（也支持 `bearer_token` / `env_http_headers`） | [Codex mcp_servers 字段参考](https://www.gptmap.org/en/posts/codex-cli-mcp) |
| Cursor | `~/.cursor/mcp.json` 或 `<项目>/.cursor/mcp.json` | `{"mcpServers":{"sortie-deck":{"url":"...","headers":{"Authorization":"Bearer ..."}}}}`（`url` 即可判定远程，`type` 可省） | [Cursor MCP 文档](https://cursor.com/docs/mcp) |
| OpenCode | `opencode.json` | `{"$schema":"https://opencode.ai/config.json","mcp":{"sortie-deck":{"type":"remote","url":"...","enabled":true,"headers":{"Authorization":"Bearer ..."}}}}` | [OpenCode MCP 文档](https://opencode.ai/docs/mcp-servers) |

`claude mcp add` 的参数顺序也已核对：`--transport http <name> <url> --header "Authorization: Bearer ..."`，`--header` 放在 URL 之后是官方示例写法。

> 安全提示：四套片段都把明文 Key 直接写进配置。本期按要求照做，同时在界面上标注「只显示一次」与「不要把配置提交到 Git」。

---

## 3. 现状盘点（代码锚点）

| 主题 | 现状 |
|---|---|
| 数据表 | `api_keys` 只有 `id / user_id / name / prefix / key_hash / scopes / created_at / last_used_at / revoked_at`，无过期字段 |
| 校验 | `ApiKeyStore.verify()` 只判断 `revoked_at`；过期语义不存在 |
| 删除 | `DELETE /api/keys/{id}` 调 `revoke()`，写 `revoked_at`，行仍在库里 |
| 配置片段 | `api/app.py` 的 `_mcp_snippet()` 只返回 Claude Code 的 `json` + `command` |
| 前端 | `apps/web/src/components/ApiKeysPanel.tsx`：顶栏 chip → 浮层弹窗；表单只有名称；表格 5 列 |
| 导航 | `apps/web/src/App.tsx` 主导航已有 `decisions`（决策中心）与 `admin`（管理），密钥入口在 header 的 `op-tools` 里 |
| 测试 | `tests/test_api_keys.py`：哈希存储、`last_used_at` 节流、软吊销后 401、HTTP 往返 |

关键位置：

- `/Library/git-root/sortie-deck-todos/src/sortie_deck/api_keys.py:52`（`ApiKeyStore`）
- `/Library/git-root/sortie-deck-todos/src/sortie_deck/api/app.py:485`（`_mcp_snippet`）
- `/Library/git-root/sortie-deck-todos/apps/web/src/components/ApiKeysPanel.tsx:13`
- `/Library/git-root/sortie-deck-todos/apps/web/src/App.tsx:2740`（决策中心 / 管理两个导航项）

---

## 4. 技术设计

### 4.1 数据模型与迁移

`ApiKey` 增加：

```python
expires_at: float | None = None   # 到期时间戳；None 表示永不过期（历史数据）
```

建表语句补列，并对已有库做幂等迁移：

```sql
ALTER TABLE api_keys ADD COLUMN expires_at REAL;   -- 仅当 PRAGMA table_info 缺列时执行
```

- 历史行 `expires_at` 为 `NULL`：保持「不过期」，避免升级后旧 Key 集体失效。
- 过期判定：`expires_at is not None and now >= expires_at` → `verify()` 返回 `None`（与吊销同路径，HTTP 层统一 401）。
- 列表仍返回过期行（前端标「已过期」），用户可手动删除。

### 4.2 签发与有效期

```python
def create(self, *, user_id: str, name: str, expires_in_months: int = 1) -> tuple[ApiKey, str]
```

- 允许值 `{1, 3, 6, 12}`，非法值在 API 层 400；存储层再兜一次 `ValueError`。
- 月份按**日历月**推进（1 月 31 日 + 1 个月 = 2 月最后一天），用 `calendar.monthrange` 实现，不引入 `dateutil`。
- 默认 1 个月，与用户要求一致。

### 4.3 删除改为物理删除

```python
def delete(self, *, user_id: str, key_id: str) -> bool:
    DELETE FROM api_keys WHERE id=? AND user_id=?
```

- 路由 `DELETE /api/keys/{key_id}` 改调 `delete()`，返回 `{"status": "deleted"}`。
- 移除 `revoke()`（唯一调用点就是该路由；`revoked_at` 列保留，兼容历史数据，不再写入）。
- 跨用户删除仍然 404（`WHERE user_id=?` 保证按人隔离）。

### 4.4 API 契约

`POST /api/keys`

```json
{ "name": "MacBook · Claude Code", "expires_in_months": 1 }
```

响应新增 `key.expires_at` 与多客户端片段：

```json
{
  "key": { "...": "...", "expires_at": 1793491200.0 },
  "plaintext": "sk_sortie_xxx",
  "mcp": {
    "url": "http://127.0.0.1:8787/mcp",
    "clients": [
      { "id": "claude-code", "label": "Claude Code", "target": "项目根目录 .mcp.json",
        "language": "json", "code": "{...}", "command": "claude mcp add ..." },
      { "id": "codex", "label": "Codex CLI", "target": "~/.codex/config.toml", "language": "toml", "code": "..." },
      { "id": "cursor", "label": "Cursor", "target": "~/.cursor/mcp.json", "language": "json", "code": "..." },
      { "id": "opencode", "label": "OpenCode", "target": "opencode.json", "language": "json", "code": "..." }
    ]
  }
}
```

保留旧的 `mcp.json` / `mcp.command` 字段一个版本，避免外部脚本（README 里的 curl 示例）直接失效；前端不再读它们。

### 4.5 前端结构

| 文件 | 改动 |
|---|---|
| `apps/web/src/types.ts` | `View` 增加 `"keys"`；`ApiKeyRecord.expires_at`；`ApiKeyCreated.mcp.clients` |
| `apps/web/src/api.ts` | `createApiKey(token, { name, expires_in_months })`；`deleteApiKey(token, id)` |
| `apps/web/src/components/ApiKeysPage.tsx` | 新增整页组件（列表 / 签发 / 一次性明文 / 客户端页签 / 删除确认） |
| `apps/web/src/App.tsx` | 导航插入「API 密钥」；`view === "keys"` 渲染页面；移除 header 里的 `ApiKeysPanel` |
| `apps/web/src/components/ApiKeysPanel.tsx` | 删除（被整页组件取代） |
| `apps/web/src/i18n.ts` | 新增中英文案 |
| `apps/web/src/styles.css` | 追加 `.akeys-*` 作用域样式（按选定方向定稿） |
| `tests/test_api_keys.py` | 过期校验、物理删除、四客户端片段、HTTP 往返 |
| `docs/{zh,en}/modules/mcp.md` | 密钥有效期与多客户端接入说明 |
| `CHANGELOG.md` | Unreleased 追加条目 |

### 4.6 交互状态机

```
列表 ──点「新建」──► 签发表单（名称 + 有效期，默认 1 个月）
  ▲                        │ 提交
  │                        ▼
  └──点「完成」── 一次性明文 + 客户端页签（Claude Code / Codex / Cursor / OpenCode）
  │
  └──点「删除」──► 行内确认（写明立即失效、不可恢复）──确认──► 从库中移除并刷新
```

页面级要点：

- 明文区块用警示底 + 「仅显示一次」标签，离开时不留痕。
- 客户端页签各自展示目标文件路径；Claude Code 额外提供「配置文件 / 命令行」二级切换。
- 过期行置灰并标注「已过期 N 天」，删除按钮仍可用。
- 空态给出「签发第一把密钥」的直接入口，而不是一句「暂无数据」。

---

## 5. 三个设计方向

三版使用同一份内容和同一套交互，只换设计逻辑，便于横向比较。文件在 `design-demos/`。

| 版本 | 逻辑 | 一句话概念 | 视觉签名 |
|---|---|---|---|
| v1 台账 | 秒数轮盘（18 % 20 + 1 = 19 → 表格主义/瑞士排版） | 密钥是一本**签发台账**：左表右签发栏，表格式节奏 | 衬线标题 + 无卡片的细线表格 + 「有效/已过期」方形戳记 + 寿命细线 |
| v2 通行证 | 现实参照（机场自助值机 / 登机牌签发） | 每把密钥是一张**通行证**：主体 + 虚线撕口 + 右侧票根 | 撕口缺口、票根上的大字到期日、签发台实时预览证件 |
| v3 连接台 | 最佳设计师（Linear / Vercel 工程控制台思维） | 密钥是**连接终端**：左轨凭证列表 + 右侧接入检查器 | 高密度、等宽为主、24 小时调用柱状图、键盘感的分栏 |

三版都保留：纸/墨双主题、键盘焦点可见、`prefers-reduced-motion` 降级、窄屏单列。

---

## 6. 备选方案

| 方案 | 做法 | 结论 |
|---|---|---|
| A. 保留弹窗，只重做视觉 | 改动最小 | 不满足「与决策中心同级页面」的诉求，否决 |
| B. 独立页 + 保留软吊销 | 只加有效期 | 与「删除后直接删数据库」冲突，否决 |
| C. 独立页 + 物理删除 + 过期字段（本方案） | 见 §4 | 采用 |
| D. 引入 Key 轮换与 scope 编辑 | 双 Key 重叠、按工具授权 | 本期不做，列已预留，后续可加 |

---

## 7. 验收

1. 导航出现「API 密钥」，与「决策中心 / 管理」同级；点击直达列表，无弹窗。
2. 新建时可选 1 / 3 / 6 / 12 个月，默认 1 个月；列表显示到期日与剩余天数。
3. 到期后该 Key 调 `/api/*` 或 `/mcp` 均 401，列表仍可见并标「已过期」。
4. 删除后 `SELECT count(*) FROM api_keys WHERE id=?` 为 0，且用该 Key 立即 401。
5. 创建响应含四个客户端片段，逐段粘贴可用（Claude Code 实测一次）。
6. A 用户看不到也删不掉 B 用户的 Key（404）。
7. `ruff check src tests`、`pytest -q`、`npm run typecheck` 全绿。

---

## 8. 风险

| 风险 | 处置 |
|---|---|
| 历史 Key 无 `expires_at`，升级后被误判过期 | 迁移只加列，`NULL` 视为永不过期 |
| 物理删除后无审计痕迹 | 删除前写一条 `logger.info`（只含 id / user / prefix）；如需审计表另开需求 |
| 明文写进客户端配置，易被提交进 Git | 界面固定提示；文档给出 `.gitignore` 建议；后续可加「环境变量占位」开关 |
| 四套片段格式随客户端版本变化 | 片段集中在后端 `_mcp_snippet()` 一处生成，改一处即可；文档标注核对日期 |
| `mcp.json` 旧字段移除会破坏外部脚本 | 保留一个版本，前端不再依赖 |

---

## 9. 状态

- [x] 业内调研（凭证页做法 + 四客户端配置格式）
- [x] 三版设计稿（`design-demos/apikeys-v1-ledger.html` / `-v2-pass.html` / `-v3-console.html`）
- [x] 用户选定设计方向（v2 通行证）
- [x] 后端：`expires_at` + 日历月有效期 + 物理删除 + 四客户端片段
- [x] 前端：独立页 + 导航 + i18n + 样式
- [x] 测试与文档

---

## 10. 实施结果（2026-10-31）

| 项 | 结果 |
|---|---|
| 数据模型 | `ApiKey.expires_at`；建表补列 + `PRAGMA table_info` 幂等迁移；历史行 `NULL` 视为长期有效 |
| 有效期 | `_add_months()` 按 UTC 日历月推进（月末对齐）；`ALLOWED_TTL_MONTHS = (1, 3, 6, 12)`，默认 1 |
| 校验 | `is_active()` 同时判吊销与过期；过期 Key 的 `/api/*` 与 `/mcp` 均为 401，列表仍保留 |
| 删除 | `ApiKeyStore.delete()` 物理删除；`DELETE /api/keys/{id}` 返回 `{"status":"deleted"}` |
| 配置片段 | `_mcp_snippet()` 输出 `clients[]`（claude-code / codex / cursor / opencode），保留旧 `json` / `command` 一个版本 |
| 前端 | `ApiKeysPage.tsx`（通行证版式）+ `view="keys"` + 导航「密钥」；移除顶栏弹窗 `ApiKeysPanel.tsx` |
| 交互 | 签发台实时预览；四客户端页签；Claude Code 配置文件 / 命令行切换；作废二次确认 |
| 验证 | `tests/test_api_keys.py` 13 项通过；`ruff check` 对新增/改动文件无告警；`npm run typecheck` 通过；真实前端 E2E（登录 → 签发 3 个月 → 四页签 → 作废）通过，数据库行数归零，控制台零报错；纸/墨双主题与 390 宽移动端无横向溢出 |
| 样式调整 | 导航项增加到 11 个后 1440 宽会换行，略收紧 `.main-nav` 的内边距与字距 |

设计稿留档：`design-demos/apikeys-index.html`（三版对比）、`apikeys-live-*.png`（实机截图）。
