# Operations & Reliability

**Status:** Implemented.
**Last updated:** 2026-08-22

---

## 1. Local setup

Prerequisites: Docker 29+ with Compose v5, and (for running outside containers) Python 3.12 and Node 22+.
The host has Python 3.12.13, Node v26.0.0, Docker 29.6.1 / Compose v5.2.0, PostgreSQL 18.4.

```bash
cp .env.example .env        # then fill in the required values below
make up                     # docker compose up -d --build
make migrate                # alembic upgrade head
make seed                   # demo user + mock connection + mock chats (MockAdapter only)
make test                   # pytest + vitest, Telegram fully mocked
```

Panel at `http://localhost:8080`. Seed credentials are printed by `make seed` and only work when
`TELEGRAM_PROVIDER=mock`.

`make up` starts only Postgres and Redis (on host ports 5433/6380) for running the backend directly;
`make up-all` runs everything in containers. Both work from the same `.env` — Compose overrides the
two connection URLs with internal service names.

### 1.1 Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres DSN |
| `REDIS_URL` | ✅ | Redis DSN |
| `APP_SECRET_KEY` | ✅ | Session/CSRF signing |
| `ENCRYPTION_KEK` | ✅ | Base64 256-bit key-encryption key. **Never** stored in the database |
| `ENCRYPTION_KEK_VERSION` | ✅ | Integer, for rotation |
| `TELEGRAM_API_ID` | for MTProto | From my.telegram.org |
| `TELEGRAM_API_HASH` | for MTProto | From my.telegram.org |
| `TELEGRAM_PROVIDER` | ✅ | `mock` (default) \| `live` |
| `PANEL_ORIGIN` | ✅ | Exact CORS origin |
| `WORKER_CONCURRENCY` | | Default 8 |
| `PER_CONNECTION_INFLIGHT` | | Default 2 |
| `ALBUM_BUFFER_MS` | | Default 2000 |
| `BOT_POLL_TIMEOUT_S` | | Default 25 |
| `SAFETY_PAUSE_THRESHOLD` | | Consecutive serious failures before auto-pause. Default 5 |
| `MAX_ATTEMPTS` | | Default 5 |
| `EVENT_RETENTION_DAYS` | | Default 90 |
| `SENTRY_DSN` | | Optional error tracking |

`TELEGRAM_PROVIDER=live` is the only way to reach real Telegram servers, and it is never set in test or
CI configuration.

### 1.2 Exercising forwarding locally

The mock provider receives no real updates, so a message can be injected through the **real** dispatch
path — the same code the listener calls:

```bash
docker compose exec api python -m app.devtools emit <connection_id> --message-id 9001 \
  --text "Public launch is live"
```

It reports how many jobs were created and how many duplicates were suppressed, so replaying the same
message id demonstrates duplicate prevention directly. It refuses to run when
`TELEGRAM_PROVIDER=live`.


## 2. Processes

| Service | Command | Scale | Notes |
|---|---|---|---|
| `api` | `uvicorn app.main:app` | N | Stateless |
| `listener` | `python -m app.listener` | N | Redis lock per connection; only the holder opens a client |
| `scheduler` | `python -m app.scheduler` | **1** | Lease reclaim, health checks, retention, safety evaluation |
| `worker` | `python -m app.worker` | N | Bounded global and per-connection concurrency |
| `nginx` | — | 1 | TLS, static SPA, security headers |
| `postgres` | — | 1 | Source of truth |
| `redis` | — | 1 | Coordination only; nothing durable |

### 2.1 Intake cost
Each bot connection holds one long-poll `getUpdates` with `timeout=25`, so a quiet bot makes roughly
**2.4 requests/minute** and consumes one idle socket. Each user connection holds one persistent MTProto
TCP connection. Both are negligible CPU; the practical ceiling per listener process is socket count, not
compute. `allowed_updates` is restricted to `message`, `channel_post`, `edited_message`,
`edited_channel_post` to avoid pulling irrelevant update types.

## 3. Retry classification

Every provider exception passes through `classify_error()` and lands in exactly one class. Unclassified
errors are `UNKNOWN` and treated conservatively — never as retryable-forever.

| Class | Examples | Action |
|---|---|---|
| `TRANSIENT` | Network timeout, connection reset, Bot API 5xx, MTProto `-500`/`ServerError` | Exponential backoff with jitter (1s → 2s → 4s → 8s → 16s), cap `MAX_ATTEMPTS` |
| `RATE_LIMIT` | Bot API `429` + `parameters.retry_after`; MTProto `FloodWaitError.seconds` | **Sleep the full provider-specified duration.** Never shorten it. If > 300s, pause the rule and surface it |
| `AUTH` | Bot API `401`; `AuthKeyUnregisteredError`, `SessionRevokedError`, `UserDeactivatedError` | **No retry.** Pause the connection, mark rules `disconnected`, notify |
| `PERMISSION` | `CHAT_WRITE_FORBIDDEN`, `ChatAdminRequiredError`, `USER_BANNED_IN_CHANNEL`, Bot API `403` | **No retry.** Mark destination ineligible, skip, record reason |
| `PERMANENT_CONTENT` | `MESSAGE_ID_INVALID` (deleted), `MEDIA_EMPTY`, `ChatForwardsRestrictedError`, unsupported/uncopyable type | **No retry.** Skip with reason, visible to the customer |
| `UNKNOWN` | Anything unmapped | Limited retries, then `dead_letter` / `needs_attention` |

Backoff never shortens a Telegram-provided wait, and jitter is bounded and applied only within the
`TRANSIENT` class for orderly processing — never to disguise traffic.

## 4. Ambiguous timeouts

The hard case: the request timed out, so the delivery may or may not have happened.

- **MTProto** — `messages.forwardMessages` takes a `random_id` per message and Telegram deduplicates
  identical `random_id`s server-side within a time window. We **persist the `random_id` with the job**
  and reuse the exact same value on retry, which makes the retry safely idempotent.
- **Bot API** — there is no idempotency token. A blind retry can double-post. We therefore **fail
  closed**: the job moves to `needs_attention`, an event records `ambiguous_timeout`, and the customer
  sees the destination with an explicit "may or may not have been delivered — retry manually?" state.
  We prefer a visible gap over a silent duplicate.

## 5. Rules edited while jobs are queued

Jobs store `rule_version`. When a worker claims a job whose `rule_version` is older than the rule's
current version, it **re-evaluates the current filters and destination membership** before sending:

- Destination removed from the rule → `skipped` (`destination_removed`).
- Message no longer passes current filters → `skipped` (`filtered_after_edit`).
- Rule paused/deleted/disconnected → `skipped` (`rule_inactive`).
- Otherwise → deliver, using the original idempotency key so an edit cannot cause a re-delivery.

## 6. Recovery matrix

| Failure | Behaviour |
|---|---|
| `api` restart | Stateless; no forwarding impact |
| `worker` crash mid-job | Lease expires; scheduler returns the job to `pending`; retry is idempotency-key protected |
| `listener` crash | Redis lock expires; another listener acquires it and resumes from the persisted cursor |
| `scheduler` down | Jobs accumulate as `pending`; nothing is lost; leases reclaim on restart |
| Redis flush/restart | Locks and buckets rebuild. **No durable state lost** — jobs live in Postgres |
| Postgres restart | Processes reconnect with backoff; in-flight jobs reclaimed by lease expiry |
| Telegram connection drop | Adapter reconnects with backoff; cursor resumes; repeated failures trigger safety pause |
| Full host reboot | `restart: unless-stopped` brings services back; cursors and jobs resume from Postgres |
| Bot 409 Conflict | Another consumer holds `getUpdates`; listener releases the lock and backs off |

**Restart drill** (run before every release): start the stack, activate a rule, deliver one message,
`docker compose kill worker listener`, send two more source messages, restart, and assert that both are
delivered exactly once and no duplicate of the first appears.

## 7. Safety pause

A rule or connection auto-pauses when:
- Any `AUTH` class error occurs → **connection** paused immediately.
- `SAFETY_PAUSE_THRESHOLD` consecutive serious failures on one rule → **rule** paused.
- A flood wait longer than 300s is returned → rule paused with the wait surfaced.

Paused state is durable, visible on Home and Rule Detail with the reason, and the listener stops
creating new jobs for it. Resume is always an explicit customer action.

## 8. Acceptance checklist

Reviewable, one line per master acceptance criterion.

- [x] Customer connects an authorized bot **or** account through a documented secure flow — *tests/integration/test_connections_and_sync.py + live stack*
- [x] Chats show which are source-eligible and which are destination-eligible, with reasons — *test_ineligible_destination_reports_a_reason*
- [x] Customer creates a persistent source→destination rule — *test_forwarding.py::build_rule*
- [x] New eligible source messages process automatically while the customer is offline — *restart drill; worker delivers with no client attached*
- [x] The HTTP API never blocks on forwarding (asserted by test) — *test_guards.py::test_api_never_calls_telegram_inside_a_request_handler*
- [x] Text and documented media types forward correctly — *test_filters.py + e2e*
- [x] Unsupported / protected / unauthorized messages are skipped safely with a visible reason — *test_protected_content_failure_is_skipped_with_a_clear_reason*
- [x] Each destination has an independent durable result — *test_each_destination_has_an_independent_result*
- [x] A repeated source event cannot duplicate a successful destination delivery — *test_replayed_update_creates_zero_new_jobs; live replay suppressed 3 duplicates*
- [x] Transient failures retry; permanent and authorization failures do not — *test_transient_failure_is_retried_and_then_succeeds / test_permission_failure_is_not_retried*
- [x] Flood waits and rate limits are respected, never bypassed — *test_flood_wait_duration_is_obeyed_in_full*
- [x] Pause, resume, edit, delete, disconnect all work **after process restarts** — *restart drill (§6)*
- [x] Repeated serious errors auto-pause the rule or connection and notify via the dashboard — *e2e step 9 + test_auth_failure_pauses_the_whole_connection*
- [x] Tokens, sessions, login codes, credentials, and message content never appear in logs or responses — *test_security.py::test_no_secret_reaches_the_log_sink + test_guards.py secret sweep*
- [x] Cross-user access is impossible, covered by tests — *test_every_object_route_is_scoped_to_its_owner*
- [x] No subscription quotas or monetization limits exist in the codebase — *test_no_quota_or_subscription_concepts_exist_in_the_codebase*
- [x] Safeguards are documented as safety controls, not advertised as unlimited capacity — *README + PRODUCT_SPEC §11*
- [x] Project runs from a clean checkout using the documented commands — *docker compose up -d; verified*
- [x] Migrations, tests, lint, type checks, and builds all pass — *141 backend + 24 frontend tests; ruff/mypy/tsc clean*
- [x] README covers Telegram setup, supported types, safe use, limitations, recovery, deployment — *README.md*

## 9. Observability

- **Logs** — structured JSON, redacted (SECURITY §6), with correlation ids.
- **Metrics** — jobs by status, delivery latency, retries by class, flood-wait seconds observed,
  queue depth, per-connection in-flight, listener lock ownership.
- **Health** — `GET /health` (liveness) and a deep check covering Postgres, Redis, scheduler heartbeat,
  and per-connection listener status.
- **Alerts** — queue depth sustained above threshold, scheduler heartbeat stale, any connection in
  `paused_safety`, `dead_letter` count rising.

## 10. Deployment

Single VPS, Docker Compose, `restart: unless-stopped`. Deploy: pull, `make migrate` (migrations are
backward compatible for one release so `api` can roll before `worker`), then recreate services.
Nightly `pg_dump` plus WAL archiving; **backups must never include `ENCRYPTION_KEK`**, which lives only
in the environment — a database backup alone must not be sufficient to decrypt sessions. Restore drill
documented and run quarterly.
