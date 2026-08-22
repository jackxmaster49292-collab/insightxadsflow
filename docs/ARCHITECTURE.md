# Architecture

**Status:** Implemented.
**Last updated:** 2026-08-22

---

## 1. Repository baseline

The working directory was **empty and not a git repository**. There is no existing framework, package
manager, database, deployment setup, or convention to inherit or preserve. Every choice below is a
greenfield decision, recorded with rationale in [DECISIONS.md](DECISIONS.md).

Verified host tooling: Python 3.12.13 (Homebrew), Node v26.0.0 / npm 11.12.1, Docker 29.6.1 with
Compose v5.2.0, PostgreSQL 18.4 local. Redis is not installed on the host and will be supplied by
Compose.

## 2. Integration choice

**Both Bot API and MTProto, behind one adapter interface, with two separate implementations.**

Rationale: the Bot API alone cannot read a channel the customer merely subscribes to, which is the
common real case; MTProto alone forces every customer to expose a personal account even when a bot
would suffice. Supporting both, with the active type always visible, matches the product spec's
"clearly separated combination" and its no-silent-fallback rule.

### 2.1 Library selection (verified 2026-08-22, not assumed)

| Library | Latest | Released | Verdict |
|---|---|---|---|
| **Telethon** | 1.44.0 | 2026-06-15 | ✅ **Selected** for MTProto — steady cadence (1.41→1.44 over 12 months) |
| Pyrogram | 2.0.106 | **2023-04-30** | ❌ **Rejected** — no release in ~3.3 years despite being named in the brief |
| Kurigram (Pyrogram fork) | 2.2.25 | 2026-08-21 | ❌ Active, but a community fork with a smaller trust surface than Telethon |
| gramjs (`telegram`, npm) | 2.26.22 | 2025-02-12 | ❌ ~18 months stale — rules out an all-TypeScript MTProto path |
| **aiogram** | 3.30.0 | 2026-07-17 | ✅ **Selected** for Bot API — actively maintained, asyncio-native, `<3.15,>=3.10` |
| python-telegram-bot | 22.8 | 2026-06-12 | Viable alternative; aiogram preferred for asyncio ergonomics |

Because the only healthy MTProto client is Telethon, **the backend is Python**. Node was not viable for
the MTProto half.

Target Bot API level: **10.2** (current).

## 3. Component layout

| Component | Responsibility |
|---|---|
| **Admin bot** (aiogram) | **The control panel.** Every screen and every flow, plus the alert outbox drain |
| **API service** (FastAPI) | Internal: the service layer the tests drive, plus the health endpoint. Not published |
| **Telegram adapter** | Encapsulates Bot API / MTProto behind one testable interface |
| **Listener** | Detects new messages from active source chats, and sends an auto-reply to a private one |
| **Scheduler** | Releases due jobs, reclaims leases, runs health checks, evaluates safety pauses |
| **Forwarding workers** | Execute forwarding jobs *and* broadcast targets with bounded concurrency, leases, retry classification, safe shutdown |
| **PostgreSQL 18** | Source of truth: users, connections, chats, rules, jobs, broadcasts, auto-replies, events, state |
| **Redis 8** | Queue coordination, short-lived locks, rate-limit counters, transient state |
| **Private object storage** | *Optional*, only if media staging proves necessary — **not in MVP** |
| **Observability** | Structured JSON logs, metrics, health checks, error tracking |

### 3.1 Processes

```
                       ┌──────────────┐
   browser ── HTTPS ──►│  nginx       │── /        ─► static SPA bundle
                       │  (TLS, CSP)  │── /api/*   ─► api
                       └──────────────┘
                              │
          ┌───────────────────┼────────────────────┐
          ▼                   ▼                    ▼
   ┌────────────┐      ┌────────────┐       ┌────────────┐
   │    api     │      │  listener  │       │  scheduler │
   │  FastAPI   │      │  Telethon  │       │  reclaim   │
   │  uvicorn   │      │  + aiogram │       │            │
   └─────┬──────┘      └─────┬──────┘       └─────┬──────┘
         │                   │                    │
         │   enqueue         │  intake            │  release / reclaim
         └───────────┬───────┴────────────────────┘
                     ▼
              ┌────────────┐        ┌────────────┐
              │   redis    │◄──────►│  worker ×N │──► Telegram
              └────────────┘        └────────────┘
                     │                    │
                     └────────┬───────────┘
                              ▼
                       ┌────────────┐
                       │ postgres   │
                       └────────────┘
```

`api`, `listener`, `scheduler`, and `worker` are **separate containers** so that a crash or flood-wait
stall in Telegram I/O cannot take down the control panel.

### 3.2 The API never blocks on Telegram

Activating a rule, synchronizing chats, running a health check, or retrying failed destinations all
**enqueue background work and return a status immediately**. The customer polls or receives the updated
status; the HTTP request never waits for a Telegram round trip. This is an acceptance criterion, and
it is enforced by a test that asserts no Telegram adapter call occurs inside a request handler.

## 4. Telegram adapter

One interface, two implementations (`BotAdapter`, `UserAdapter`), plus `MockAdapter` used by default in
tests.

```python
class TelegramAdapter(Protocol):
    async def connect(self) -> ConnectionState: ...
    async def disconnect(self) -> None: ...
    async def health_check(self) -> HealthReport: ...
    async def list_available_chats(self) -> list[DiscoveredChat]: ...
    async def check_source_access(self, ref: ChatRef) -> AccessReport: ...
    async def check_destination_access(self, ref: ChatRef) -> AccessReport: ...
    def receive_new_messages(self) -> AsyncIterator[InboundMessage]: ...
    async def forward_message(
        self, source: ChatRef, message_ids: list[int],
        destination: ChatRef, *, random_id: int | None = None,
    ) -> DeliveryReceipt: ...
    async def send_supported_content(self, ...) -> DeliveryReceipt: ...   # only when authorized
    def classify_error(self, exc: BaseException) -> ClassifiedError: ...
    def capabilities(self) -> Capabilities: ...
```

Rules: no Telegram-specific types cross this boundary — `ChatRef`, `InboundMessage`, `DeliveryReceipt`,
and `ErrorClass` are our own. No Telethon or aiogram import exists outside `adapters/`. No
Telegram-specific code in unrelated backend modules — enforced by an AST guard test that allows the
imports only inside `adapters/` and `adminbot/`. Unit and integration tests run entirely against
`MockAdapter`.

### 4.1 `ChatRef` and the peer-identity problem

Telegram's docs state the peer ID is a 64-bit value and **"the ID sequences of users, chats and
channels overlap, so you must use separate tables/hashmaps"**. Therefore:

- `ChatRef = (peer_type, peer_id)` — never `peer_id` alone, anywhere.
- `peer_id` is `BIGINT` in Postgres, `int` in Python, and **serialized as a JSON string** in the API so
  no JSON consumer can round it through a float.
- MTProto `access_hash` is **per-account**, so it is stored per connection, encrypted, and is never
  shared between connections or exposed through the API.

## 5. Event intake

### 5.1 User connections (MTProto)
Telethon's persistent update connection with `catch_up` enabled. Update state (`pts`, `qts`, `date`,
`seq`) is persisted per connection so a restart resumes rather than replays. Per-chat
`last_processed_message_id` cursors provide a second dedupe layer.

### 5.2 Bot connections (Bot API)
**Long polling** by default: `getUpdates` with `timeout=25`, a persisted `offset`, and an explicit
`allowed_updates` list (`message`, `channel_post`, `edited_message`, `edited_channel_post`). The offset
is committed to Postgres only after the batch is durably persisted, so a crash re-reads rather than
loses. Webhook mode is a documented V1 option for deployments with a public HTTPS endpoint.

This is a **persistent, continuously running poller** — not a low-frequency scheduled task. Interval,
`allowed_updates`, and resource impact are documented in [OPERATIONS.md](OPERATIONS.md).

### 5.3 Single-owner constraint (important)
Telegram permits only one `getUpdates` consumer per bot token (a second gets **409 Conflict**), and two
MTProto clients sharing one session produce duplicate updates. The listener therefore takes a **Redis
lock per connection, with heartbeat renewal**, and only the lock holder opens the client. This makes
the listener horizontally scalable without duplicate intake.

### 5.4 Album buffering
Grouped media arrives as several updates sharing `media_group_id` (Bot API) / `grouped_id` (MTProto).
The listener buffers by group id for a short bounded window (default 2s, configurable) and emits **one
logical `InboundMessage`** carrying all message ids, delivered with the plural forward call. A partial
group that never completes is emitted after the window with a `partial_album` note on the event.

## 6. Queue and workers

Durable jobs in Postgres (source of truth) with Redis for coordination and pacing. There is no queue
library: workers claim due rows with `SELECT … FOR UPDATE SKIP LOCKED` (ADR-015). Each job carries:
connection ID, rule ID, rule version, source chat, source message id, destination chat, idempotency
key, attempt count, not-before timestamp, lease owner and expiry, and current status.

- **Bounded concurrency**, global and **per connection**, so one connection cannot consume all worker
  capacity.
- **Backpressure**: when a connection's in-flight limit is reached, jobs stay `pending` with a
  `not_before` push rather than spinning.
- **Leases**: a worker claims a job with a lease and heartbeat. The scheduler reclaims jobs whose lease
  expired, which is how crashed workers recover.
- **Graceful shutdown**: on SIGTERM, stop claiming, finish or release in-flight leases, flush events.

### 6.1 Pacing
Two layers, both transparent and bounded — never randomized:

1. **Rule delay** — the customer's configured, bounded inter-destination delay.
2. **Platform-safe token buckets** in Redis, seeded from the documented limits: ~1 msg/sec per chat,
   **20 msgs/min per group**, ~30 msgs/sec per bot overall. Conservative defaults for MTProto.

Buckets shape *normal* pacing. A flood wait from Telegram always overrides them and is obeyed in full.

## 7. Deployment

Single VPS, Docker Compose: `nginx`, `api`, `listener`, `scheduler`, `worker` (scalable), `postgres`,
`redis`. Details, restart-recovery procedure, and health checks in [OPERATIONS.md](OPERATIONS.md).

## 8. Failure handling

Full retry classification, ambiguous-timeout policy, safety-pause rules, and recovery matrix are in
[OPERATIONS.md](OPERATIONS.md) §3–§6. The two load-bearing principles:

- **Fail closed on authorization uncertainty.** If destination eligibility cannot be confirmed at
  delivery time, skip and record the reason. Never guess.
- **Never ignore a Telegram-provided wait.** `retry_after` (Bot API 429) and `FloodWaitError.seconds`
  (MTProto) are honoured in full; backoff never shortens them.
