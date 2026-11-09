# IM Integration

## 1. Overview

Route gate approvals and mission updates into enterprise IM, and let humans approve
from inside the chat.

| Item | Value |
|------|-------|
| Code | `src/sortie_deck/im/` |
| Storage | `data/im.sqlite` (connections), `data/im_events.sqlite` (inbound dedupe) |
| API | `/api/im/*`, inbound `/api/im/webhook/{provider}` |
| UI | Settings → Access → IM integrations (`ImIntegrationsPanel.tsx`) |

Binding is **workspace-scoped**: one connection per channel, one mission per chat.

## 2. Layers

```text
platform event → ① Adapter (verify / decrypt / normalise)
               → ③ Router (mapping, dedupe, ACK-then-async)
               → ② Identity (P2, IM user ↔ Sortie user)
               → ④ Interaction (card action → resolve_decision → graph resume)
```

| File | Responsibility |
|------|----------------|
| `base.py` | `InboundEvent` / `ApprovalCard` / `CardAction` / `ImProvider` protocol |
| `connection.py` | `ImConnection` model, encrypted store, state machine, manager |
| `cards.py` | one card rendered to four platforms + DingTalk signed links |
| `router.py` | inbound routing, dedupe, ACK, unmapped-chat guidance |
| `qr_login.py` | scan session (WeChat) |
| `runner.py` | background loops: WeChat long-poll, WeCom WebSocket, backoff |
| `service.py` | assembly (config, dependency injection, singleton) |
| `feishu.py` / `dingtalk.py` / `wecom.py` / `wechat.py` | platform protocols |

## 3. State machine

```text
unconfigured → connecting → connected
                    ↓            ↓
                  error ←────────┘
connected → disabled
invalid credentials → expired
```

Illegal transitions raise `InvalidTransition`; re-setting the same state is
idempotent (reconnect storms report repeatedly). Changes are broadcast through
`ImConnectionManager.on_status` for SSE.

## 4. Platform differences (and why the code looks like this)

| Platform | No public IP | Card interaction | Constraints |
|----------|--------------|------------------|-------------|
| Feishu | long connection | `card.action.trigger` (real) | 3s ACK; cards updatable 14 days / interactive 30 days; cluster push is **not** broadcast |
| DingTalk | Stream | ActionCard buttons **URL only** | 1500ms on HTTP callback; hence signed links via `/api/im/action` |
| WeCom | AI-bot WS | `template_card` + `template_card_event` | update within 5s; **one connection per bot**; long-connection and callback are mutually exclusive |
| WeChat DM | long-poll | **no cards** | DM only; protocol is not a stable contract; replies need `context_token` |

### 4.1 Long connection vs callback is a platform-side switch

For all three enterprise platforms the choice is configured **in the platform
console**, not only locally. Changing mode locally resets the connection to
`unconfigured`; the form shows an inline reminder. When a connection is up but
silent, `health` exposes `events_seen_at` and a hint pointing at the subscription mode.

### 4.2 Single consumer

Feishu/DingTalk cluster push is not broadcast and WeCom drops the previous socket,
so only one process may connect: `TDT_IM_CONSUMER=true`. Multi-replica deployments
must be active/standby.

## 5. Data contract

```python
ImConnection      # no plaintext; has_secret + secret_fingerprint
  provider        # wechat | wecom | feishu | dingtalk | slack
  mode            # qr | stream | webhook
  status          # unconfigured | connecting | connected | error | expired | disabled
  app_id          # Feishu App ID / DingTalk AppKey / WeCom BotID (not secret)
  events_seen_at  # last inbound event, used to diagnose subscription mismatch
```

Secrets reuse the git-credential master key: `TDT_SECRET_KEY` first, falling back
to `TDT_GIT_SECRET_KEY`. With neither configured, writes are **rejected** rather
than stored in plaintext.

## 6. Gate cards

`Orchestrator._notify_gate()`:

1. registers a pending decision (`notify=False`, so no duplicate notice);
2. resolves the mission's bound IM chat;
3. sends an approval card carrying `decision_id` and approve / rewrite / reject / stop;
4. on failure, falls back to the legacy text fan-out.

A button click calls `resolve_decision(action=...)` → `submit_hitl(APPROVE/REJECT/...)`
→ graph resume. `approve` is no longer treated as "edit instruction and rerun".

## 7. Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `TDT_SECRET_KEY` | empty | Master key; falls back to `TDT_GIT_SECRET_KEY` |
| `TDT_IM_DB_PATH` | `data/im.sqlite` | Connection store |
| `TDT_IM_CONSUMER` | `true` | Whether this process may open long connections |
| `TDT_IM_ACTION_TTL_SECONDS` | `86400` | DingTalk signed link TTL |
| `TDT_FEISHU_ENCRYPT_KEY` | empty | Encrypt Key for Feishu URL callbacks |
| `TDT_PUBLIC_BASE_URL` | empty | Base URL for card fallback and DingTalk buttons |

Optional extra: Feishu/DingTalk long connections need `pip install 'sortie-deck[im]'`.
Without it, `probe` returns an actionable error; URL callback mode still works.

## 8. Known limits

- WeChat DM only, no cards — use Feishu/DingTalk/WeCom for approval buttons;
- the WeChat iLink protocol comes from OpenClaw's client implementation and is
  **not a stable public contract**; changes are contained in `wechat.py`;
- WeCom/WeChat outbound needs the in-process socket, so `TDT_IM_CONSUMER` should be
  enabled on the process that runs the orchestrator (usually the worker);
- QR sessions are process-local; multi-replica deployments need API stickiness.
