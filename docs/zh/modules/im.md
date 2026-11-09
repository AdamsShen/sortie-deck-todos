# IM 集成

## 1. 概述

把任务门禁与结果通知接到企业 IM，并支持在 IM 内完成审批。

| 项 | 值 |
|----|-----|
| 代码 | `src/sortie_deck/im/` |
| 存储 | `data/im.sqlite`（接入记录）、`data/im_events.sqlite`（入站去重） |
| API | `/api/im/*`，入站 `/api/im/webhook/{provider}` |
| 前端 | 设置 → 接入 → IM 集成（`ImIntegrationsPanel.tsx`） |

绑定粒度是**工作区级**：每个渠道一条连接，一个 IM 会话绑定一个任务。

## 2. 分层

```text
平台事件 → ① Adapter（验签/解密/归一化）
         → ③ Router（映射解析、去重、先 ACK 后异步）
         → ② Identity（P2，IM 用户 ↔ Sortie 用户）
         → ④ Interaction（卡片动作 → resolve_decision → 图 resume）
```

| 文件 | 职责 |
|------|------|
| `base.py` | `InboundEvent` / `ApprovalCard` / `CardAction` / `ImProvider` 协议 |
| `connection.py` | `ImConnection` 模型、加密存储、状态机、`ImConnectionManager` |
| `cards.py` | 一个卡片渲染到四个平台 + 钉钉签名链接 |
| `router.py` | 入站路由、去重、ACK、未映射提示 |
| `qr_login.py` | 扫码会话（微信） |
| `runner.py` | 后台循环：微信长轮询、企微 WebSocket、退避重连 |
| `service.py` | 装配（读配置、注入依赖、单例） |
| `feishu.py` / `dingtalk.py` / `wecom.py` / `wechat.py` | 各平台协议实现 |

## 3. 状态机

```text
unconfigured → connecting → connected
                    ↓            ↓
                  error ←────────┘
connected → disabled
凭据失效 → expired
```

非法迁移抛 `InvalidTransition`；同状态重复设置是幂等（重连风暴下会重复上报）。
状态变化通过 `ImConnectionManager.on_status` 广播，供 SSE 推给前端。

## 4. 平台差异（决定了实现取舍）

| 平台 | 免公网 | 卡片交互 | 关键约束 |
|------|--------|----------|----------|
| 飞书 | 长连接 | `card.action.trigger`（真交互） | 3 秒 ACK；卡片 14 天可更新 / 30 天可交互；多副本推送不广播 |
| 钉钉 | Stream | ActionCard 按钮**只能跳 URL** | HTTP 回调 1500ms；故用签名链接 `/api/im/action` |
| 企业微信 | 智能机器人 WS | `template_card` + `template_card_event` | 5 秒内更新；**同一机器人只允许一条连接**；长连接/回调互斥 |
| 微信个人号 | 长轮询 | **无卡片** | 仅单聊；协议非稳定契约；回复必须带 `context_token` |

### 4.1 长连接与回调互斥

三个平台的「长连接 / URL 回调」都是**平台侧互斥配置**，不是本地开关：

- 飞书：后台「事件与回调 → 订阅方式」必须与本地选择一致；
- 钉钉：开发管理 → 推送方式；
- 企微：切换会让另一种模式**直接失效**。

本地换 mode 时 `PATCH /api/im/connections/{id}` 会先把状态打回 `unconfigured`，
并在表单里给出就地提示。连上但长时间收不到事件时，`health` 返回
`events_seen_at` 与提示文案用于定位。

### 4.2 单消费者

飞书/钉钉推送是集群模式不广播、企微新连接会踢掉旧连接，因此**只允许一个进程
建连**：`TDT_IM_CONSUMER=true`。多副本时必须主备，不能多活。

## 5. 数据契约

```python
ImConnection      # 无明文；has_secret + secret_fingerprint
  provider        # wechat | wecom | feishu | dingtalk | slack
  mode            # qr | stream | webhook
  status          # unconfigured | connecting | connected | error | expired | disabled
  app_id          # 飞书 App ID / 钉钉 AppKey / 企微 BotID（非敏感）
  events_seen_at  # 最近一次收到事件，用于诊断订阅方式
```

凭据加密沿用 git 凭据的主密钥：`TDT_SECRET_KEY` 优先，未配置回落
`TDT_GIT_SECRET_KEY`；两者都没有时**拒绝写入**，不降级明文。

## 6. 门禁卡片

`Orchestrator._notify_gate()`：

1. 在决策中心登记 pending 决策（`notify=False`，避免与卡片重复触达）；
2. 反查该任务绑定的 IM 会话；
3. 发卡片（带 `decision_id` 与 approve / rewrite / reject / stop）；
4. 卡片发送失败 → 退回原来的全局文本通知。

按钮点击 → `resolve_decision(action=...)` → `submit_hitl(APPROVE/REJECT/...)`
→ 图 resume。`approve` 不再被当作「编辑指令重跑」。

## 7. 配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `TDT_SECRET_KEY` | 空 | 凭据主密钥；空则回落 `TDT_GIT_SECRET_KEY` |
| `TDT_IM_DB_PATH` | `data/im.sqlite` | 接入记录 |
| `TDT_IM_CONSUMER` | `true` | 是否允许本进程建连（多副本只开一个） |
| `TDT_IM_ACTION_TTL_SECONDS` | `86400` | 钉钉签名链接有效期 |
| `TDT_FEISHU_ENCRYPT_KEY` | 空 | 飞书 URL 回调模式的 Encrypt Key |
| `TDT_PUBLIC_BASE_URL` | 空 | 卡片兜底链接与钉钉按钮链接的基地址 |

可选依赖：飞书/钉钉的长连接需要 `pip install 'sortie-deck[im]'`；未安装时
`probe` 会返回可操作提示，不影响 URL 回调模式。

## 8. 已知限制

- 微信个人号仅单聊、无卡片，审批按钮请用飞书/钉钉/企微；
- 微信 iLink 协议来自 OpenClaw 插件的客户端实现，**非腾讯公开承诺的稳定契约**，
  协议变化时只需改 `wechat.py`；
- 企微/微信的出站依赖进程内的长连接，`TDT_IM_CONSUMER` 应开在跑 orchestrator
  的进程（通常是 worker）；
- 扫码会话不跨进程，多副本时前端需粘到同一台 API。
