# 任务流程接入 IM（微信 / 钉钉 / 飞书）技术方案

- 状态：**P0 / P0.5 / P1 / P1.5 已实现并通过测试**；P2（身份绑定、群镜像）待做
- 日期：2026-11-09
- 范围：
  1. 把 mission 生命周期（启动、HITL 门禁、决策答复、完成/失败）接入企业 IM，并支持在 IM 内完成审批；
  2. 在工作台设置中心提供**产品化的 IM 集成管理页**，参照 CodeBuddy「Claw 设置 → 集成」形态：渠道卡片 + 微信扫码（带过期倒计时）+ 飞书/钉钉/企微凭据注册（长连接 / URL 回调二选一）+ 连接状态与探活。
- 关联 backlog：`TODO.md` §1「真人频道与角色绑定」

---

## 1. 需求背景

当前 Sortie Deck 的人机协同只在 Web 工作台内闭环：门禁（HITL）触发后写房间消息 + SSE，人在不看页面时任务会一直挂在 `waiting_hitl`。

已有三块基础，但都不完整：

| 模块 | 已有能力 | 位置 |
|---|---|---|
| 出站通知 | webhook / 飞书 / 钉钉**纯文本**、脱敏、失败降级 | `src/sortie_deck/notify.py` |
| 入站桥接 | slack / feishu / dingtalk 三种 payload 解析 + `BridgeStore` 映射 | `src/sortie_deck/bridges.py` |
| 决策中心 | `decision_id` 幂等、assignee 鉴权、SLA 超时 fallback | `src/sortie_deck/decisions.py` |
| 门禁触达 | HITL 时 fan-out 到 IM（仅文本） | `src/sortie_deck/orchestrator.py:2147` |
| 答复回灌 | 决策答复 → `submit_hitl(EDIT_INSTRUCTION)` → 图 resume | `src/sortie_deck/orchestrator.py:2525` |

**核心缺口**：IM 侧只能看文字、靠手打 `/approve`；没有可点的审批卡片，没有身份映射，钉钉入站甚至没验签。

---

## 2. 业界调研

调研工具：agent-reach（Exa 搜索 + GitHub API 后端）。以下结论均来自实际检索结果。

### 2.1 共性架构：四层适配

对比 LangBot、CubePlex、Dify 集成实践、CopilotKit 与 Slack 官方文档，主流做法高度一致：

```text
平台事件 ──► ① Adapter（验签/解密/归一化）
          ──► ② Identity（IM 用户 → 内部 user_id）
          ──► ③ Router（channel → 业务对象，去重、ACK）
          ──► ④ Interaction（卡片动作 → 恢复被中断的 agent run）
```

- **LangBot**（开源，12+ 平台）：`pkg/platform/sources/` 下每平台一个 adapter，统一转成 SDK message entity，再进 `RuntimeBot → MessageAggregator → Controller → Pipeline`。新增平台 = 新增 adapter，不动业务层。
  来源：<https://github.com/langbot-app/LangBot/blob/master/ARCHITECTURE.md>
- **CubePlex IM Connectors**：明确分成「绑定 bot → 入站消息 → identity gate 校验成员 → 跑 agent → 回帖」，并把投递方式抽象为 long-connection / webhook / gateway 三种。飞书标注 "most developed"，钉钉标 Stream，企微标 Stream(WebSocket)。
  来源：<https://cubeplex.ai/docs/guides/im/overview>
- **CopilotKit Slack Channels**：用一套 JSX 组件渲染成平台原生 UI（Slack 渲染为 Block Kit），按钮点击以 `block_actions` 回到服务端，再 `thread.resume(value)` 恢复被中断的 agent run。这是「一套 HITL 卡片、多平台渲染」的范式。
  来源：<https://docs.copilotkit.ai/slack/langgraph-python/interactive>
- **Dify 集成实践**：强调三件事——统一消息路由层、跨平台用户身份映射、各平台签名验证与消息格式转换，避免为每个平台重写业务逻辑。
  来源：<https://tools.yiteai.com/books/dify/ch18>

### 2.2 免公网接入（关键，决定能否内网部署）

| 平台 | 机制 | 硬约束 |
|---|---|---|
| 飞书 | 长连接 WebSocket（SDK 内置加密鉴权） | 仅企业自建应用；每应用 ≤50 连接；**3 秒内处理完**；推送是集群模式**不广播**（多 client 只有一个收到） |
| 钉钉 | Stream 模式（官方称"五零"：零加解密 / 验签 / 证书 / 白名单 / 内网穿透） | 每应用默认 ≤50 连接；事件可能重推，需按 `eventId` 幂等；HTTP 模式则要 1500ms 内返回加密 `success` |
| 企业微信 | 智能机器人 WebSocket（`wss://openws.work.weixin.qq.com`） | 模板卡片 `template_card_event` 需 **5 秒内** 调 `updateTemplateCard`；传统应用消息回调仍是公网 URL + AES 加解密 |
| Slack | Socket Mode | 官方明确建议**生产用 HTTP**，Socket Mode 适合本地开发/无公网场景 |

来源：
- <https://open.feishu.cn/document/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/configure-callback-request-address?lang=zh-CN>
- <https://github.com/open-dingtalk/dingtalk-stream-sdk-python>
- <https://help.dingtalk.io/zh/open/development/callback-overview>
- <https://github.com/WecomTeam/aibot-node-sdk>
- <https://docs.slack.dev/apis/events-api/comparing-http-socket-mode/>

### 2.3 卡片交互（HITL 的真正载体）

| 平台 | 回调事件 | 响应窗口 | 延时更新 |
|---|---|---|---|
| 飞书 | `card.action.trigger`（新版，旧版 `trigger_v1` 不支持长连接） | 3 秒 HTTP 200 | `interactive/v1/card/update`；token 30 分钟有效、**同 token 仅 2 次**；卡片 30 天内可交互、**14 天内可更新**；禁止 3xx 重定向 |
| 钉钉 | 卡片回调 topic `/v1.0/card/instances/callback`（Stream） | Stream 内 ack | 支持 update card |
| 企微 | `event.template_card_event` | 5 秒 | `updateTemplateCard`（task_id 需与回调一致） |

来源：
- <https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks?lang=zh-CN>
- <https://help.dingtalk.io/open/development/event-subscription-overview>
- <https://github.com/WecomTeam/aibot-node-sdk>

**结论**：卡片按钮的 `value` 里必须自带业务幂等键（本项目就是 `decision_id`），因为平台不保证回调只来一次，且卡片有有效期，过期后必须能降级到"去工作台处理"的链接。

### 2.4 微信的三条路径（必须选一条）

| 路径 | 官方能力 | 主动下发限制 | 结论 |
|---|---|---|---|
| **企业微信** | 自建应用消息（内部员工）；微信客服（触达微信外部客户）；智能机器人 WebSocket | 应用消息 ≤账号上限×200 人次/天，同成员 ≤30 次/分；微信客服 **用户发消息后 48 小时内 5 条** | **唯一合规的"触达微信里的人"通道**，适合内部审批 |
| 公众号 / 小程序 | 客服消息、订阅消息 | 客服消息 48h/5 条；订阅消息一次性授权、7 天内下发 | 只适合客服与通知，撑不起多轮任务流 |
| 个人微信 | 2026-03 起官方 iLink Bot / ClawBot，腾讯开源 `Tencent/openclaw-weixin`（MIT，1015 stars），扫码登录、长轮询收消息 | 仅**单聊**、不支持群；灰度开放；无卡片；配额与频控未公开 | 可作为"个人提醒"补充，但**不能承载审批**（无卡片、仅 DM、规则未稳） |
| 个人微信（非官方） | 注入 / 协议模拟 | — | **红线**：违反服务条款，官方处罚为永久封禁成员账号，情节严重连带封禁企业账号。不做 |

来源：
- <https://github.com/Tencent/openclaw-weixin>
- <https://onethu.github.io/docs/im-cloud/01-wechat/>
- <https://cloud.tencent.com/developer/article/2776537>（企业微信官方通道对比与 48h/5 条实测坑）
- <https://developer.work.weixin.qq.com/document/path/101033>（回调 AES 加解密与 echostr 验证）

### 2.5 选型结论

**飞书 + 钉钉做审批主通道（P0/P1），企业微信进管理页（P1.5），微信个人号做 BETA 扫码（仅通知，不承载审批）。**

理由：

1. 飞书卡片能力最完整、文档最好，长连接零公网，适合做主通道；
2. 钉钉 Stream 接入成本最低（无需验签解密），适合快速验证；
3. 企业微信是唯一能触达微信外部联系人的合规通道，凭 BotID + Secret 十分钟可接完，但模板卡片字段受限，且长连接/回调互斥；
4. 微信个人号：官方 iLink Bot 可做出与 CodeBuddy Claw 一致的扫码体验，但**仅单聊、无卡片、协议非稳定契约**——适合"任务完成/失败提醒"，不能承载审批按钮；非官方 hook/协议模拟仍是红线，不做。

---

## 3. 技术设计

### 3.1 目标架构

```text
飞书长连接 / 钉钉 Stream / 企微 WS / webhook
        │
        ▼
src/sortie_deck/im/<provider>.py    ① Adapter：验签、解密、归一化
        │
        ▼
src/sortie_deck/im/router.py        ③ Router：channel→mission、幂等、先 ACK 后异步
        │                    \
        │                     ▼
        │            src/sortie_deck/im/identity.py  ② IM user ↔ Sortie user
        ▼
Orchestrator（复用现有能力）
  ├─ post_human_message()      → 房间时间线 + 斜杠命令
  ├─ resolve_decision()        → 决策答复 → submit_hitl → 图 resume
  └─ _hitl_notify()（:2147）   → 改为发卡片
        │
        ▼
src/sortie_deck/im/cards.py   ④ 统一卡片模型 → 各平台渲染
        │
        ▼
src/sortie_deck/notify.py     出站 fan-out（扩为 provider 适配）
```

### 3.2 统一内部模型

```python
# im/base.py
class InboundEvent(BaseModel):
    provider: str            # feishu | dingtalk | wecom | slack
    channel_id: str          # chat_id / conversationId
    sender_id: str           # 平台原始 id（open_id / userId / userid）
    sender_name: str
    text: str = ""
    action: CardAction | None = None   # 卡片按钮点击时有值

class CardAction(BaseModel):
    decision_id: str
    action: str              # approve | reject | rewrite | stop
    instruction: str = ""
    operator_id: str = ""

class CardButton(BaseModel):
    key: str                 # 与 action 同名
    text: str
    style: str = "default"   # default | primary | danger

class ApprovalCard(BaseModel):
    title: str
    stage: str
    mission_id: str
    decision_id: str
    summary: str             # 已过 notify.redact
    buttons: list[CardButton]
    fallback_url: str = ""   # 卡片过期后的工作台深链
```

`ApprovalCard` 是三平台渲染的唯一输入：飞书 → `interactive` 卡片 JSON；钉钉 → ActionCard；企微 → `template_card`（`card_type=button_interaction`）。

### 3.3 关键工程约束（缺一不可）

| 约束 | 做法 |
|---|---|
| **3 秒 ACK** | 回调 handler 只做「校验 + 落队列 + 立即返回」，业务处理 `asyncio.create_task` 到后台。绝不能像现在这样同步 `await post_human_message()`（会跑整条 pipeline 恢复） |
| **幂等** | 卡片动作以 `decision_id + action` 为键，复用 `DecisionService.answer` 的 `answered/duplicate` 语义（已实现） |
| **重推** | 钉钉按 `eventId`、企微按 `msgid` 去重，落 `im_dedupe` 表（同库 sqlite，带过期清理） |
| **多副本** | 长连接推送不广播 → 只允许一个进程建连。新增 `TDT_IM_CONSUMER`（默认 true），API 多副本时只给一个实例开启；回调内不依赖进程内状态，统一走 orchestrator 单例 + sqlite |
| **卡片过期** | 飞书 14 天后不可更新 → 卡片 footer 常驻 `fallback_url`（`TDT_PUBLIC_BASE_URL/missions/<id>`），按钮文案带"或到工作台处理" |
| **未映射频道** | 现在直接返回 `channel not mapped`。改为：未映射时回一条带绑定链接的提示，避免消息静默丢失 |
| **敏感信息** | 出站前统一走 `notify.redact`（已实现），卡片 summary 复用同一入口 |

### 3.4 集成管理页（产品化配置，对齐 CodeBuddy Claw 设置）

目标形态：用户在**工作台内**完成渠道接入，不碰 `.env`、不改 docker-compose。参照 CodeBuddy 的「Claw 设置 → 集成」，每个渠道一张卡片，点「配置」出二维码或凭据表单，接入后卡片显示连接状态。

#### 3.4.1 信息架构

现有 `SettingsCenter.tsx` 的左轨分三组：`个人 / 接入 / 数据`，`接入` 下已有「Git 凭据」「API 密钥」（`apps/web/src/components/SettingsCenter.tsx:249`）。新增一个 **「IM 集成」** tab 放在同一组，与凭据管理语义一致（都是"接入外部系统"）。

页面结构：

```text
设置 / IM 集成
├─ 渠道卡片列表
│   ├─ 微信 ClawBot   [推荐] [BETA]      状态徽标   [配置]
│   ├─ 企业微信 AIBot                    状态徽标   [配置]
│   ├─ 飞书                              状态徽标   [配置]
│   ├─ 钉钉                              状态徽标   [配置]
│   └─ Slack                             状态徽标   [配置]
└─ 会话绑定
    └─ 已接入的会话 → 绑定到某个 mission（写入现有 BridgeStore）
```

每张卡片：图标 + 渠道名 + 状态徽标 + 一行说明 + 「配置」按钮 + 「配置指南」链接。徽标取值：`未配置 / 连接中 / 已连接 / 失败 / 已过期 / 已禁用`。

#### 3.4.2 两种配置表单

**A. 扫码类（微信 ClawBot）**

```text
┌ 微信 ClawBot 集成 ──────────────┐
│ 打开微信扫描此二维码             │
│                                │
│         [ QR CODE ]            │
│                                │
│      114 秒后过期               │
└────────────────────────────────┘
```

交互链路：

1. 点「配置」→ `POST /api/im/connections/wechat/qr`
2. 后端调 iLink Bot `POST /ilink/bot/get_bot_qrcode?bot_type=3`，返回 `{ticket, qr_content, expires_in}`
3. 前端本地渲染二维码（不落盘、不打外链），按 `expires_in` 起倒计时
4. 前端每 2 秒 `GET /api/im/connections/wechat/qr/{ticket}`，状态 `pending → scanning → success | expired`
5. `success` 时后端已拿到 `bot_token`，加密落库并自动建连，返回连接记录；卡片翻成「已连接」
6. `expired` 时按钮变「重新获取」，倒计时归零

协议依据：`docs/protocol.md` 中 QR 流程为 `POST get_bot_qrcode` 取码、`GET` 轮询状态；认证头 `AuthorizationType: ilink_bot_token` + `Authorization: Bearer <bot_token>`；收消息为长轮询；发消息 `POST /ilink/bot/sendmessage`，回复必须回传入站消息的 `context_token`。

**B. 凭据类（飞书 / 钉钉 / 企微）**

```text
┌ 注册飞书通道 ────────────────────┐
│ 输入飞书应用凭据以将此工作区绑定   │
│ 到飞书机器人。App ID 和 App Secret │
│ 为必填项。                        │
│                                 │
│ ⦿ WebSocket 长连接  ○ 使用 URL 回调 │
│                                 │
│ [ 飞书 App ID (cli_xxxx)      ]  │
│ [ 飞书 App Secret             ]  │
│                                 │
│              [取消]  [注册]      │
└─────────────────────────────────┘
```

字段：

| 渠道 | 凭据 | 接入方式选项 |
|---|---|---|
| 飞书 | App ID（`cli_` 开头）、App Secret | WebSocket 长连接（默认）/ 使用 URL 回调 |
| 钉钉 | App Key、App Secret | WebSocket 长连接（默认）/ 使用 URL 回调 |
| 企业微信 | Bot ID、Secret | WebSocket 长连接（默认）/ 使用 URL 回调 |
| Slack | Bot Token、Signing Secret | Socket Mode / Events API URL |

提交后后端**同步建连 + 探活**（获取 token / 建立 subscribe），成功才置 `connected`，失败把平台原始错误写进 `last_error` 显示在卡片上——不能出现"填完显示成功但实际连不上"。

#### 3.4.3 二选一必须先说清的坑

三个平台的「长连接 / 回调」是**平台侧互斥配置**，不是本地开关：

| 平台 | 约束 | 后果 |
|---|---|---|
| 飞书 | 开发者后台「事件与回调 → 回调配置 → 订阅方式」需选「使用长连接」 | 本地选 stream 但后台选 URL 回调 → 收不到任何事件 |
| 钉钉 | 开发管理 → 推送方式选 Stream | 反之同理 |
| 企微 | 「API 模式」只能选一种；**切换到回调会让现有长连接失效**，反之亦然 | 切错方向会让线上机器人直接哑掉 |
| 企微（连接数） | 同一机器人同时只允许**一条**有效长连接，新连接会踢掉旧连接 | 多副本必须主备，不能多活 |

因此表单里选 mode 之后，要展示一段就地提示：

> 请到飞书开发者后台「事件与回调 → 回调配置」把订阅方式同步改为「使用长连接」，本地选择才会生效。

并在 `probe` 结果里区分「凭据错误」与「订阅方式不匹配」（后者表现为连上但收不到事件，需 `GET /api/im/connections/{id}/health` 返回 `events_seen_recently=false` 作为提示）。

#### 3.4.4 数据模型

```python
class ImConnection(BaseModel):
    id: str                    # imc_xxx
    provider: str              # wechat | wecom | feishu | dingtalk | slack
    mode: str                  # qr | stream | webhook
    label: str = ""
    status: str                # unconfigured|connecting|connected|error|expired|disabled
    app_id: str = ""           # 飞书 App ID / 钉钉 AppKey / 企微 BotID（非敏感，明文）
    has_secret: bool = False   # 只写不读：接口永远不回显明文
    secret_fingerprint: str = ""   # 用于人工核对"换没换"
    qr_expires_at: float | None = None
    last_error: str = ""
    last_connected_at: float | None = None
    events_seen_at: float | None = None   # 最近一次收到事件时间（诊断订阅是否生效）
    enabled: bool = True
    created_by: str = ""
    created_at: float
```

凭据加密直接复用 `git_cred.py` 的 Fernet 方案（`src/sortie_deck/git_cred.py:178`）。为避免已部署环境因新增变量而无法解密旧凭据，主密钥读取顺序为 `TDT_SECRET_KEY` → 回落 `TDT_GIT_SECRET_KEY`；未配置时**拒绝写入**（沿用现有"绝不降级明文"的约定）。

存储：与 `git_credentials` 同库不同表（`im_connections`），`secret_cipher BLOB`。

#### 3.4.5 运行时：连接管理器

```text
ImConnectionManager
├─ start()            读 enabled 连接 → 按 (provider, mode) 起 adapter
├─ connect(conn)      stream: 建 WebSocket + subscribe；webhook: 只登记路由
├─ disconnect(id)     停 adapter，状态置 disabled
├─ probe(id)          用凭据换 token / 建连一次 → connected | error(last_error)
├─ health(id)         最近事件时间、心跳、重连次数
└─ on_status_change   → 写库 → SSE 广播 {type:"im.status"}
```

- **单消费者**：`TDT_IM_CONSUMER`（默认 true）。多副本时只给一个实例开，其余只读状态。企微因"新连踢旧"必须主备，不能靠 leader 选举凑合。
- **重连**：指数退避 1s → 2s → 4s → … → 30s 上限（对齐 `WecomTeam/aibot-node-sdk` 的做法），连续失败 5 次置 `error`。
- **心跳**：企微建议 30s ping；飞书/钉钉由 SDK 维系。
- **状态可变性**：配置变更（换 mode、换 secret）必须先 `disconnect` 再 `connect`，避免半死连接。

#### 3.4.6 API 清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/im/providers` | 渠道能力表：支持的 mode、是否扫码、指南链接、必填字段 |
| GET | `/api/im/connections` | 列表（脱敏，含 status / last_error） |
| POST | `/api/im/connections` | 创建凭据类连接（body: provider/mode/app_id/secret） |
| PATCH | `/api/im/connections/{id}` | 改 label、启停、换 mode（触发重连） |
| DELETE | `/api/im/connections/{id}` | 删除并断开 |
| POST | `/api/im/connections/{id}/probe` | 探活 |
| GET | `/api/im/connections/{id}/health` | 心跳、最近事件时间、重连次数 |
| POST | `/api/im/connections/wechat/qr` | 申请扫码二维码 |
| GET | `/api/im/connections/wechat/qr/{ticket}` | 轮询扫码状态 |
| GET | `/api/im/connections/{id}/chats` | 该连接可见会话（供绑定 mission） |
| POST | `/api/bridges/map` | 已有，把 chat 绑到 mission（需补 dingtalk 白名单） |

权限：全部 `require_admin`（凭据类操作），`GET` 列表允许 member 只读。

#### 3.4.7 前端组件

| 文件 | 职责 |
|---|---|
| `apps/web/src/components/ImIntegrationsPanel.tsx` | 渠道卡片列表 + 配置弹窗 + 状态轮询 |
| `apps/web/src/components/QrLogin.tsx` | 二维码渲染、倒计时、2s 轮询、过期重取 |
| `apps/web/src/components/ImChatBinding.tsx` | 会话 → mission 绑定（复用 `/api/bridges`） |
| `apps/web/src/components/SettingsCenter.tsx:249` | `接入` 组新增「IM 集成」按钮 + tab 分支 |
| `apps/web/src/api.ts` | 新增 `im*` 客户端方法 |
| `apps/web/src/types.ts` | `ImConnection` / `ImProvider` 类型 |
| `apps/web/src/styles` | `.im-card` / `.im-badge` / `.im-qr` / `.im-form`，沿用 `.stg-*` 设计语言 |

#### 3.4.8 微信扫码的现实取舍（必须让用户知道）

CodeBuddy 能把二维码画在自己的设置页里，是因为它内置了 iLink Bot 协议实现。`docs/protocol.md` 明确声明：**"Client types and behavior do not establish the complete server contract"**——即这是 OpenClaw 插件的客户端实现，不是腾讯公开承诺的稳定契约，随时可能变。

两条可选路径：

| 路径 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 自实现协议（推荐，与 CodeBuddy 一致）** | 复刻 `login-qr` + `monitor` 长轮询 + `sendmessage` | 扫码体验完整、UI 在自家页面、无额外进程 | 协议非稳定契约；仅单聊不支持群；灰度中；需自己处理 `ret=-14` 会话暂停 |
| B. 依赖 OpenClaw Gateway | 部署 openclaw + `@tencent-weixin/openclaw-weixin`，Sortie 走 webhook | 协议变更由上游维护 | 多一个 Node 进程（≥22.13）；扫码 UI 在 OpenClaw 侧，Sortie 只能做二次跳转 |

建议：**A 用于产品体验，但在渠道卡片上明确标 `BETA`**，并在 `docs` 里写明"仅单聊、协议非稳定契约、失效时降级为出站通知"。B 作为运维兜底方案写进文档。

#### 3.4.9 本轮新增的阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0.5** | 集成管理页骨架：`ImConnection` 存储 + 列表/创建/删除/probe API + 飞书 & 钉钉凭据表单（含 stream/webhook 二选一）+ 状态徽标 | 后台填 App ID/Secret 能建连成功，卡片显示「已连接」；填错显示平台原始错误 |
| **P1.5** | 微信 ClawBot 扫码卡片（二维码 + 倒计时 + 轮询）+ 企微 AIBot 表单 + 会话绑定 | 微信扫码后 3 秒内卡片翻为已连接；企微可在群里 @机器人推进任务 |

这两阶段插在原 P0/P1 之后、P2 之前，因为管理页是"能不能用"的前置。

### 3.5 文件清单

**新增**

| 文件 | 内容 |
|---|---|
| `src/sortie_deck/im/__init__.py` | 导出 `InboundEvent` / `ApprovalCard` / `get_im_service` |
| `src/sortie_deck/im/base.py` | `ImProvider` Protocol：`verify()` / `parse_inbound()` / `send_card()` / `update_card()` / `start()` / `close()` |
| `src/sortie_deck/im/feishu.py` | 长连接（`lark-oapi` ws）+ `card.action.trigger`；webhook 模式保留作降级 |
| `src/sortie_deck/im/dingtalk.py` | `dingtalk-stream` SDK；机器人回调 + 卡片回调 topic |
| `src/sortie_deck/im/wecom.py` | 企微智能机器人 WebSocket（P2） |
| `src/sortie_deck/im/cards.py` | `ApprovalCard` → 三平台 payload 渲染 + 已处理态卡片 |
| `src/sortie_deck/im/identity.py` | `ImIdentityStore`：`(provider, sender_id) → usr_xxx`，绑定码（6 位短码，5 分钟有效） |
| `src/sortie_deck/im/router.py` | 入站路由、去重、ACK/后台执行、未映射引导 |
| `src/sortie_deck/im/connection.py` | `ImConnection` 模型 + `ImConnectionStore`（Fernet 加密、只写不读）+ `ImConnectionManager`（建连/断开/探活/状态广播） |
| `src/sortie_deck/im/qr_login.py` | 扫码登录会话：ticket → 二维码内容 → 状态轮询 → 落 token（含过期清理） |
| `src/sortie_deck/im/wechat.py` | iLink Bot 协议：`get_bot_qrcode` / QR 轮询 / 长轮询收消息 / `sendmessage`（回传 `context_token`） |
| `apps/web/src/components/ImIntegrationsPanel.tsx` | 渠道卡片列表 + 配置弹窗 + 状态刷新 |
| `apps/web/src/components/QrLogin.tsx` | 二维码渲染、倒计时、2s 轮询、过期重取 |
| `apps/web/src/components/ImChatBinding.tsx` | 会话 → mission 绑定（复用 `/api/bridges`） |
| `tests/test_im_cards.py` | 卡片渲染快照（三平台） |
| `tests/test_im_router.py` | 幂等、ACK 不阻塞、未映射引导 |
| `tests/test_im_identity.py` | 绑定码过期、一人多端、排他 |
| `tests/test_im_connection.py` | 凭据加密/脱敏、状态机、重连退避、扫码过期 |
| `docs/zh/modules/im.md` + `docs/en/modules/im.md` | 模块规格与平台配置步骤 |

**修改**

| 位置 | 改动 |
|---|---|
| `src/sortie_deck/settings.py:300` | 新增 `feishu_app_id` / `feishu_app_secret` / `dingtalk_client_id` / `dingtalk_client_secret` / `wecom_bot_id` / `wecom_secret` / `im_mode`(stream\|webhook) |
| `src/sortie_deck/settings.py:391` | 新增 `secret_key`（读 `TDT_SECRET_KEY`，回落 `TDT_GIT_SECRET_KEY`）、`im_consumer` |
| `src/sortie_deck/notify.py:84` | `build_payload` 支持 `card` 分支；新增 `send_card` |
| `src/sortie_deck/bridges.py:13` | `PROVIDERS` 加 `wecom`；`BridgeMapping` 增加 `chat_type`、`created_by` |
| `src/sortie_deck/api/app.py:3555` | **修 bug**：`/api/bridges/map` 白名单仅 `{slack, feishu}`，漏 `dingtalk`，导致钉钉映射配不上 |
| `src/sortie_deck/api/app.py:3632` | **修安全问题**：`dingtalk_bridge` 无验签无解密，公网暴露可被伪造 → 迁到 `im/router.py` 统一处理 |
| `src/sortie_deck/api/app.py:3532` | `_bridges()` 改为从 `im` 服务取，路由改为委托 |
| `src/sortie_deck/orchestrator.py:2147` | 门禁通知改为「文本 + 审批卡片」，卡片带 `decision_id` 与三按钮 |
| `src/sortie_deck/orchestrator.py:2525` | `resolve_decision` 增加 `channel="im"` 时回写卡片为已处理态 |
| `apps/web/src/components/SettingsCenter.tsx:249` | `接入` 组新增「IM 集成」入口 + tab 分支 |
| `apps/web/src/api.ts` / `types.ts` | `im*` 客户端方法、`ImConnection` / `ImProvider` 类型 |

### 3.6 分阶段与验收

| 阶段 | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| **P0** | 钉钉 Stream + 飞书长连接：入站文本 + 出站文本；斜杠命令可用；补验签/去重/3 秒 ACK | 钉钉群发 `/approve` 能把 `waiting_hitl` 任务推进到下一阶段；伪造请求被拒 | 1–2 天 |
| **P0.5** | 集成管理页骨架：`ImConnection` 存储 + 列表/创建/删除/probe + 飞书 & 钉钉凭据表单（stream/webhook 二选一）+ 状态徽标 | 后台填 App ID/Secret 能建连；填错显示平台原始错误；重启后连接自动恢复 | 2 天 |
| **P1** | HITL 审批卡片（approve / reject / rewrite / stop），点击 → `resolve_decision` → 图 resume，卡片更新为"已处理" | 卡片按钮可推进任务；重复点击不重复 resume；14 天过期后按钮提示跳工作台 | 2–3 天 |
| **P1.5** | 微信 ClawBot 扫码卡片（二维码 + 114s 倒计时 + 轮询）+ 企微 AIBot 表单 + 会话绑定 | 微信扫码后卡片翻「已连接」；企微群里 @机器人能推进任务 | 2–3 天 |
| **P2** | 身份绑定（IM user ↔ Sortie user，6 位绑定码）+ assignee 鉴权生效 + 群镜像（房间消息 → IM 群，只读/受控写可配） | 非绑定人点卡片被拒并提示；群镜像可开关 | 2 天 |
| **P3** | 微信客服（仅当需要触达微信外部客户）；公众号订阅消息 | 外部客户可收到通知 | 2 天 |

### 3.7 备选方案与取舍

| 方案 | 说明 | 取舍 |
|---|---|---|
| **A. 自建 `im/` 适配层（推荐）** | 复用现有 `decisions` + `bridges` + `notify`，只补 adapter 与卡片 | 增量最小；卡片天然绑定 `decision_id`，幂等可控；缺点是三平台各写一次渲染 |
| B. 用 LangBot 做 IM 网关，Sortie 只暴露 HTTP/MCP | 上线最快，支持 12+ 平台 | 引入外部进程与两套会话状态；卡片只能走通用消息，审批语义要重新映射；多副本运维成本反升 |
| C. CopilotKit Channels（portable UI） | 一套 JSX 渲染 Block Kit / Adaptive Card | 只覆盖 Slack / Teams，**不覆盖国内三平台**，不可用 |
| D. n8n / Dify 作中间编排 | 可视化连 IM 节点 | 多一跳、状态割裂、HITL 语义无法精确表达，排除 |
| E. 只做出站通知 + 链接跳工作台 | 最小成本 | 无法在 IM 内闭环审批，长期仍要回工作台；建议作为 P0 的兜底形态而非终态 |

### 3.8 风险与对策

| 风险 | 对策 |
|---|---|
| 飞书长连接集群模式不广播，多副本丢消息 | 只允许单实例建连（`TDT_IM_CONSUMER`），并把连接声明写进 `sortie doctor` |
| 3 秒 ACK 内跑 pipeline 恢复会超时 | 强制 handler 只做校验+入队，业务在后台任务；加 metrics 记录处理耗时 |
| 钉钉入站现无验签 | P0 必修；未配置 `client_secret` 时拒绝处理而非放行 |
| 卡片有效期（飞书 14/30 天） | footer 常驻工作台深链；过期卡片点击给出 toast 指引 |
| 微信客服 48h/5 条配额 | 发送前本地预算拦截（`{window_start, count}`），超限转人工，不等接口报 95001 |
| 个人微信非官方方案 | 明确不做；文档中写明处罚条款，避免后续误用 |
| **微信 iLink 协议非稳定契约** | 渠道卡片标 `BETA`；adapter 与业务解耦，协议变更只改 `im/wechat.py`；失效时自动降级为出站通知并把 `last_error` 显示在管理页 |
| **企微长连接/回调互斥切换** | 表单内就地提示"切换会使另一种模式失效"；`PATCH` 换 mode 前强制 `disconnect`，并在响应里返回影响说明 |
| **企微同一机器人仅允许一条连接** | 多副本必须主备：`TDT_IM_CONSUMER` 只在一台为 true，另一台禁建连；健康检查发现"被踢"时进 `error` 而非无限重连打转 |
| **凭据主密钥丢失** | 沿用 `git_cred.py` 的"未配置即拒写"；`TDT_SECRET_KEY` 回落 `TDT_GIT_SECRET_KEY`，避免新增变量导致既有部署无法解密；`sortie doctor` 增加主密钥检查项 |
| 管理页配了但平台后台没同步 | 表单内提供「配置指南」深链 + `health` 返回 `events_seen_at`，长时间未收到事件时卡片提示"订阅方式可能不匹配" |

---

## 4. 参考来源汇总

- LangBot 架构：<https://github.com/langbot-app/LangBot/blob/master/ARCHITECTURE.md>
- CubePlex IM Connectors：<https://cubeplex.ai/docs/guides/im/overview>
- CopilotKit Slack + LangGraph 审批：<https://docs.copilotkit.ai/slack/langgraph-python/interactive>
- 飞书长连接：<https://open.feishu.cn/document/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/configure-callback-request-address?lang=zh-CN>
- 飞书卡片回调（3 秒 / 延时更新 / 有效期）：<https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks?lang=zh-CN>
- 钉钉 Stream SDK：<https://github.com/open-dingtalk/dingtalk-stream-sdk-python>
- 钉钉事件订阅 Stream vs HTTP：<https://help.dingtalk.io/open/development/event-subscription-overview>
- 企微智能机器人 SDK：<https://github.com/WecomTeam/aibot-node-sdk>
- 企微智能机器人长连接（BotID/Secret、单连接限制、模式互斥）：<https://developer.work.weixin.qq.com/document/path/101463>
- 企微回调解密：<https://developer.work.weixin.qq.com/document/path/101033>
- 微信个人号官方通道 Tencent/openclaw-weixin：<https://github.com/Tencent/openclaw-weixin>
- 微信 iLink Bot 协议（QR 登录 / 长轮询 / sendMessage）：<https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md>
- 微信个人号通道调研：<https://onethu.github.io/docs/im-cloud/01-wechat/>
- 微信 AI 客服合规链路（48h/5 条实测）：<https://cloud.tencent.com/developer/article/2776537>
- Slack HTTP vs Socket Mode：<https://docs.slack.dev/apis/events-api/comparing-http-socket-mode/>
- Dify 三平台集成实践：<https://tools.yiteai.com/books/dify/ch18>

---

## 5. 实现记录（2026-11-09）

### 已完成

| 阶段 | 交付 | 测试 |
|---|---|---|
| P0 | 统一入站路由（验签/解密、按 `(provider, raw_id)` 去重、先 ACK 后异步）、修复 `bridges/map` 白名单漏 dingtalk、修复 dingtalk 入站无验签 | `test_im_router.py` 16 例 |
| P0.5 | `ImConnection` 加密存储 + 状态机 + 渠道能力表 + 管理页 CRUD/probe/health API | `test_im_connection.py` 13 例、`test_im_api.py` 14 例 |
| P1 | 审批卡片四平台渲染、门禁改发卡片（失败降级文本）、`approve` 映射为真正的图动作 | `test_im_cards.py` 12 例、`test_im_gate.py` 6 例 |
| P1.5 | 微信 iLink 扫码登录、企微 WebSocket、后台运行器（退避重连）、前端管理页 | `test_im_wechat.py` 14 例、`test_im_adapters.py` 15 例 |
| P2 | IM 身份绑定（映射表 + 一次性短码 + `/bind`）、`by` 取值改为绑定后的用户名 | `test_im_identity.py` 17 例 |

### 与方案的偏差（及原因）

1. **钉钉用「签名链接」而非互动卡片**：群机器人 ActionCard 的按钮只能 `actionURL`，
   要真回调必须走企业应用互动卡片（需卡片平台建模板 + `outTrackId`）。签名链接效果
   等价、少一层模板依赖，且能验签 + 限时（默认 24h）。
2. **微信二维码用图片 URL 直出**：前端没有二维码库，后端返回的 `qrcode_img_content`
   直接作为 `<img src>`，打不开时给「复制链接」兜底，避免为一个渠道引入前端依赖。
3. **飞书/钉钉的长连接未内置**：需要官方 SDK（`lark-oapi` / `dingtalk-stream`），已放进
   `[im]` extra。未安装时 `probe` 明确提示改用 URL 回调，不假装可用。
4. **企微/微信长连接自实现**：`websockets` 随 `uvicorn[standard]` 已装，企微 JSON 帧协议
   直接实现，不额外引 SDK。
5. **群镜像未做**：门禁卡片 + 决策通知已覆盖「需要人介入」；全量镜像会刷屏，且
   「谁能往任务里写」是 TODO.md 里的开放问题，需要单独的开关与过滤策略。

### 验证结果

| 检查 | 结果 |
|---|---|
| `pytest -q` | 1050 passed（`test_stage_artifact_salvage.py::test_stage_timeout_recovers_artifacts_from_workspace` 在改动前基线即失败，属既有 flaky：0.3s 超时对 intake 冷启动过紧） |
| `ruff check` | 新增/修改文件全部通过（仓库其余 23 条既有告警未动） |
| `tsc -b` | 通过 |
| `vite build` | 通过 |
| `mkdocs build --strict` | 仅 3 条既有链接告警（`zh/design/*`），与本次改动无关 |
