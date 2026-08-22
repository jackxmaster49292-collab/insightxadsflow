# API Contract

**Status:** Implemented.
**Last updated:** 2026-08-22

`openapi.yaml` will be generated from FastAPI's typed models at implementation step 2 and committed;
this document is the reviewable contract it must match.

---

## 1. Conventions

- Base path `/api/v1`. JSON only. `snake_case` fields.
- Auth via `HttpOnly` session cookie. Every endpoint except `/auth/register`, `/auth/login`, and
  `/health` requires authentication.
- **Telegram identifiers are strings in JSON** (`"peer_id": "-1001234567890"`) so no JSON consumer `Number`
  rounding is possible, and always paired with `peer_type`.
- Pagination is cursor-based: `?limit=50&cursor=<opaque>` → `{"items": [...], "next_cursor": "..."}`.
  `limit` max 200.
- `Idempotency-Key` is **required** on connection creation and on activate / pause / resume /
  retry-failed; optional elsewhere.
- Every response carries `X-Correlation-Id`.
- Errors are uniform and safe — no provider strings, no stack traces:

```json
{ "error": { "code": "destination_not_eligible",
             "message": "This chat can no longer receive messages from the connection.",
             "correlation_id": "01J...", "details": {} } }
```

- A resource belonging to another user returns **404**, never 403 — existence is not disclosed.

## 2. Authentication

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/register` | Rate limited 3/hr/IP |
| `POST` | `/auth/login` | Rate limited 5/15min per IP **and** account; sets session cookie |
| `POST` | `/auth/logout` | Deletes the server-side session row |
| `GET` | `/me` | Current user, timezone, counts |

## 3. Telegram connections

| Method | Path | Notes |
|---|---|---|
| `GET` | `/telegram/connections` | List; **never** returns tokens or session material |
| `POST` | `/telegram/connections/bot` | Body `{label, bot_token}`. Token validated then immediately encrypted. Returns `202` + connection in `pending` |
| `POST` | `/telegram/connections/user/start` | Body `{label, phone}`. Sends login code. → `awaiting_code` |
| `POST` | `/telegram/connections/user/verify` | Body `{connection_id, code}`. → `active` or `awaiting_2fa` |
| `POST` | `/telegram/connections/{id}/2fa` | Body `{password}`. Used in memory only, **never stored** |
| `GET` | `/telegram/connections/{id}` | Status, capabilities, last successful check, last safe error |
| `POST` | `/telegram/connections/{id}/sync` | **202**, enqueues sync. Never blocks |
| `POST` | `/telegram/connections/{id}/health-check` | **202**, enqueues check |
| `POST` | `/telegram/connections/{id}/disconnect` | Body `{revoke: bool}`. `revoke:true` also calls `auth.logOut` |

`bot_token`, `code`, and `password` are accepted **only** in a request body over TLS — never in a query
string, never in a URL, never logged.

Connection response includes a `capabilities` object so the UI can explain what the active connection
can actually do:

```json
{ "id": "…", "kind": "user", "status": "active", "label": "Main account",
  "telegram_username": "example", "last_successful_check_at": "2026-08-22T09:12:00Z",
  "capabilities": { "can_read_subscribed_channels": true, "can_read_group_messages": true,
                    "can_read_history": true, "max_download_bytes": null },
  "last_error": null }
```

## 4. Chats

| Method | Path | Notes |
|---|---|---|
| `GET` | `/telegram/chats` | Filters: `connection_id`, `source_eligible`, `destination_eligible`, `type`, `is_public`, `is_active`, `has_error`, `q` |
| `GET` | `/telegram/chats/{id}` | Detail incl. eligibility reason codes |
| `POST` | `/telegram/chats/{id}/check-access` | **202**, enqueues a fresh access check |

```json
{ "id": "…", "peer_type": "channel", "peer_id": "-1001234567890",
  "title": "Announcements", "chat_kind": "channel", "is_public": true,
  "has_protected_content": false,
  "source_eligible": true,  "source_reason_code": "ok",
  "destination_eligible": false, "destination_reason_code": "bot_not_admin",
  "last_synced_at": "2026-08-22T09:10:00Z" }
```

`access_hash` is never present in any response.

## 5. Forwarding rules

| Method | Path | Notes |
|---|---|---|
| `GET` | `/forwarding-rules` | List with status, destination count, filter summary, last activity |
| `POST` | `/forwarding-rules` | Creates in `draft`; validates eligibility of every source and destination |
| `GET` | `/forwarding-rules/{id}` | Full config + preview string |
| `PATCH` | `/forwarding-rules/{id}` | Bumps `version`; queued jobs re-evaluate (OPERATIONS §5) |
| `POST` | `/forwarding-rules/{id}/activate` | **202**. Requires ≥1 eligible source and destination |
| `POST` | `/forwarding-rules/{id}/pause` | **202** |
| `POST` | `/forwarding-rules/{id}/resume` | **202** |
| `DELETE` | `/forwarding-rules/{id}` | Cancels pending jobs; events retained |
| `GET` | `/forwarding-rules/{id}/events` | Paginated; filter by `outcome`, `destination_chat_id`, time range |
| `POST` | `/forwarding-rules/{id}/retry-failed` | **202**. Retries only `failed`/`needs_attention` — never replays successes |

Create/update body:

```json
{ "name": "Announcements → partners",
  "connection_id": "…",
  "source_chat_ids": ["…"],
  "destination_chat_ids": ["…", "…"],
  "forward_mode": "forward",
  "delay_ms": 1500,
  "keyword_include": ["launch"],
  "keyword_exclude": ["draft"],
  "keyword_match_mode": "word",
  "media_types": ["text", "photo", "video", "document"],
  "preserve_links": true,
  "preserve_caption": true }
```

Rejected at validation with a specific `error.code`: an ineligible source or destination, a chat from a
different connection, a source that is also a destination without explicit confirmation, `delay_ms`
outside bounds, an empty destination list, or `forward_mode: "copy"` on a source with
`has_protected_content` — the last is refused deliberately, not as a bug.

## 6. Status and operations

| Method | Path | Notes |
|---|---|---|
| `GET` | `/activity` | Recent events across all rules, filterable |
| `GET` | `/health` | Unauthenticated liveness |
| `GET` | `/usage/summary` | Counts of forwarded / skipped / failed / retried / paused over `?period=24h\|7d\|30d`. **Operational counters only — not quota accounting** |
| `GET` | `/audit-events` | Paginated audit trail |

## 7. Deliberately absent

No payment, subscription, plan, quota, billing, or admin API. No endpoint returns a raw Telegram
session, bot token, or `access_hash`. No endpoint accepts a URL that the server will fetch.

A test asserts by reflection that no response model in the codebase exposes a field whose name matches
the secret denylist.
