# Database Model

**Status:** Implemented. PostgreSQL 18 + Alembic migrations.
**Last updated:** 2026-08-22

---

## 1. Conventions

- Primary keys are `UUID` (`gen_random_uuid()`), except join tables which use composite keys.
- All timestamps are `TIMESTAMPTZ`, stored UTC. Display timezone is a per-user setting.
- Telegram peer ids are `BIGINT` (64-bit per Telegram's peer docs) and are **always** paired with
  `peer_type`, because user/chat/channel id sequences overlap. Serialized as **strings** in JSON.
- Encrypted columns are `BYTEA` and suffixed `_ciphertext`. They are never selected into API responses.
- Enums are native Postgres enum types.
- Every table has `created_at`; mutable tables have `updated_at` maintained by trigger.

## 2. Entities

### `users`
`id` · `email` (CITEXT, **UNIQUE**) · `password_hash` (Argon2id) · `is_active` · `timezone`
(default `UTC`) · `created_at` · `updated_at`

### `app_sessions`
Server-side sessions so revocation is real, not advisory.
`id` · `user_id →users` · `token_hash` (**UNIQUE**, SHA-256 of the cookie value — the raw token is
never stored) · `issued_at` · `expires_at` · `revoked_at` · `last_seen_at` · `ip_hash` · `user_agent`
Index: `(user_id, expires_at)`.

### `telegram_connections`
`id` · `user_id →users` · `kind` ENUM(`bot`,`user`) · `label` · `status` ENUM(`pending`,
`awaiting_code`, `awaiting_2fa`, `active`, `error`, `paused_safety`, `disconnected`) ·
`bot_token_ciphertext` (BYTEA, null for user) · `telegram_account_id` BIGINT (bot user id / account id,
null until verified) · `telegram_username` · `phone_hash` (SHA-256, user kind only — the raw phone is
never stored) · `last_health_check_at` · `last_successful_check_at` · `last_error_code` ·
`last_error_message_safe` · `consecutive_failure_count` · `connect_lock_key` · `created_at` ·
`updated_at`

- **UNIQUE** `(user_id, kind, telegram_account_id)` where `telegram_account_id IS NOT NULL` —
  prevents connecting the same bot/account twice.
- Partial **UNIQUE** on `(user_id)` where `status IN ('pending','awaiting_code','awaiting_2fa')` —
  enforces "prevent duplicate simultaneous connection attempts" in the database, not just the app.

> The raw 2FA password is **never** stored, hashed, or logged. It is used once, in memory, to complete
> `SignInWithPassword`, then discarded.

### `telegram_sessions` (MTProto only)
`id` · `connection_id →telegram_connections` (**UNIQUE**, `ON DELETE CASCADE`) ·
`session_ciphertext` BYTEA · `wrapped_dek` BYTEA · `key_version` INT · `created_at` · `rotated_at`
Envelope encryption — see [SECURITY.md](SECURITY.md) §4.

### `telegram_chats`
`id` · `connection_id →telegram_connections` (CASCADE) · `peer_type` ENUM(`user`,`chat`,`channel`) ·
`peer_id` BIGINT · `access_hash_ciphertext` BYTEA (per-account, MTProto only) · `title` · `username` ·
`chat_kind` ENUM(`private`,`group`,`supergroup`,`channel`,`other`) · `is_public` BOOLEAN NULL ·
`has_protected_content` BOOLEAN NULL · `is_active` · `last_synced_at` · `last_error_code` ·
`last_error_message_safe` · `created_at` · `updated_at`

- **UNIQUE** `(connection_id, peer_type, peer_id)` — the overlapping-id-sequence rule made structural.
- Index `(connection_id, is_active)`, and a trigram index on `title` for search.
- `access_hash` is stored **per connection** because it is account-specific and not portable.

### `connection_chat_access`
Eligibility snapshot, separated from chat metadata so re-checks do not churn `telegram_chats`.
`chat_id →telegram_chats` (**PK**, CASCADE) · `can_read_source` BOOLEAN · `can_post_destination`
BOOLEAN · `source_reason_code` · `destination_reason_code` · `checked_at` · `check_source` ENUM(`sync`,
`explicit_check`, `pre_delivery`)

Reason codes are a closed vocabulary (`ok`, `not_a_member`, `bot_not_admin`, `privacy_mode_enabled`,
`write_forbidden`, `banned`, `protected_content`, `channel_private`, `unknown`) so the UI can explain
ineligibility without leaking raw API errors.

### `forwarding_rules`
`id` · `user_id →users` · `connection_id →telegram_connections` · `name` · `status` ENUM(`draft`,
`active`, `paused`, `error`, `disconnected`) · `version` INT (bumped on every edit) · `forward_mode`
ENUM(`forward`,`copy`) · `delay_ms` INT (bounded, `CHECK (delay_ms BETWEEN 0 AND 3600000)`) ·
`keyword_include` TEXT[] · `keyword_exclude` TEXT[] · `keyword_match_mode` ENUM(`substring`,`word`) ·
`media_types` TEXT[] · `preserve_links` BOOLEAN · `preserve_caption` BOOLEAN · `max_attempts` INT ·
`paused_reason_code` · `last_activity_at` · `created_at` · `updated_at`

### `forwarding_rule_sources` / `forwarding_rule_destinations`
`rule_id →forwarding_rules` (CASCADE) · `chat_id →telegram_chats` · `position` INT (destinations only)
**PK** `(rule_id, chat_id)` — a chat cannot be listed twice on one side of a rule.

### `forwarding_jobs`
`id` · `rule_id` · `rule_version` INT · `connection_id` · `source_chat_id` · `source_message_ids`
BIGINT[] (an array so albums are one job) · `destination_chat_id` · `idempotency_key` TEXT
(**UNIQUE**) · `status` ENUM(`pending`,`leased`,`succeeded`,`failed`,`skipped`,`needs_attention`,
`dead_letter`) · `attempt_count` INT · `not_before` TIMESTAMPTZ · `lease_owner` TEXT ·
`lease_expires_at` TIMESTAMPTZ · `destination_message_id` BIGINT NULL · `last_error_class` ·
`last_error_code` · `created_at` · `updated_at`

- Claim index: `(status, not_before)` `WHERE status = 'pending'`.
- Reclaim index: `(lease_expires_at)` `WHERE status = 'leased'`.

**Idempotency key** = `sha256(rule_id ‖ source_peer_type ‖ source_peer_id ‖ min(source_message_ids) ‖
destination_peer_type ‖ destination_peer_id)`.

`rule_version` is deliberately **excluded** from the key: editing a rule must not cause an already
delivered message to be re-delivered. `rule_version` is stored separately so a worker can detect that a
job predates the current rule and re-evaluate filters before sending (see [OPERATIONS.md](OPERATIONS.md) §5).

### `broadcasts`
An "ad": the customer's own message, to be posted to groups they chose.
`id` · `user_id` · `connection_id` · `name` · `status`
ENUM(`draft`,`scheduled`,`sending`,`paused`,`completed`,`cancelled`) · `body_text` TEXT ·
`media_kind` ENUM(`none`,`photo`) · `media_bytes` BYTEA NULL · `media_filename` NULL ·
`delay_ms` INT · `scheduled_for` NULL · `started_at` NULL · `completed_at` NULL ·
`paused_reason_code` NULL
`media_bytes` holds the image itself rather than a Telegram `file_id`, because a `file_id` is scoped to
the bot that received it and is meaningless to the connection doing the posting (ADR-027). Capped at
`MAX_BROADCAST_MEDIA_BYTES`.
`draft` exists because composing spans several Telegram messages and must survive a bot restart.

### `broadcast_targets`
One group, one delivery, one durable row — the same claim/lease shape as `forwarding_jobs`.
`id` · `broadcast_id` · `chat_id` · `position` · `status` ENUM `job_status` · `attempt_count` ·
`not_before` · `lease_owner` NULL · `lease_expires_at` NULL · `mtproto_random_id` NULL ·
`destination_message_id` NULL · `last_error_class` NULL · `last_error_code` NULL
**Unique** `(broadcast_id, chat_id)` — the guarantee that enqueueing twice cannot produce two
deliveries to the same group. Partial indexes on `not_before WHERE status='pending'` and
`lease_expires_at WHERE status='leased'`, matching the job queue.
`chat_id` is a foreign key into `telegram_chats`, never a raw peer id: a broadcast cannot address a chat
the account was never confirmed to be in.

### `auto_replies`
One per connection — the same connection that broadcasts.
`id` · `user_id` · `connection_id` **UNIQUE** · `enabled` · `body_text` TEXT · `cooldown_s` INT ·
`sent_count` INT
`CHECK (cooldown_s >= 60)`. There is no recipient column and no recipient table, because auto-reply can
only ever answer someone who wrote first (ADR-026).

### `auto_reply_log`
Who has already been answered, and when.
**PK** `(connection_id, peer_type, peer_id)` · `replied_at` · `reply_count`
A table rather than a cache: an empty cache after a restart would answer everyone a second time. The
claim is a single `INSERT … ON CONFLICT DO UPDATE … WHERE replied_at < cutoff`, so two listeners racing
on the same incoming message cannot both send.

### `forwarding_events`
Durable, append-only history, shared by both pipelines.
`id` · `rule_id` NULL · `broadcast_id` NULL · `job_id` NULL · `connection_id` · `source_chat_id` ·
`source_message_ids` BIGINT[] · `destination_chat_id` NULL ·
`outcome` ENUM(`forwarded`,`skipped`,`failed`,`retry_scheduled`,`paused`) ·
`reason_code` · `detail_safe` TEXT · `attempt` INT · `occurred_at`
`CHECK ((rule_id IS NULL) <> (broadcast_id IS NULL))` — every row belongs to exactly one pipeline, so
the activity screen can render any row without guessing where it came from.
Indexes `(rule_id, occurred_at DESC)`, `(broadcast_id, occurred_at DESC)` and
`(connection_id, occurred_at DESC)` for the activity views.
`detail_safe` is redacted at write time and is the only field the UI renders.

### `idempotency_keys` (HTTP layer)
Prevents replay of activate/pause/resume/retry commands.
`user_id` · `key` · **PK** `(user_id, key)` · `endpoint` · `request_hash` · `response_status` ·
`response_body` JSONB · `state` ENUM(`in_progress`,`completed`) · `created_at` · `expires_at`
A replay with a matching `request_hash` returns the stored response; a mismatch returns `409`.

### `connection_update_state`
Durable intake cursors.
`connection_id` (**PK**, CASCADE) · `bot_update_offset` BIGINT · `mtproto_pts` INT · `mtproto_qts` INT ·
`mtproto_date` INT · `mtproto_seq` INT · `updated_at`

### `source_cursors`
Second dedupe layer per source chat.
**PK** `(connection_id, chat_id)` · `last_processed_message_id` BIGINT · `last_seen_at`

### `audit_events`
`id` · `user_id` · `action` · `object_type` · `object_id` · `ip_hash` · `user_agent` · `metadata`
JSONB (redacted) · `created_at`
Actions: connection create/verify/disconnect/revoke, sync, rule create/edit/activate/pause/resume/
delete, retry-failed, data export, account deletion.

### `app_settings`
`user_id` (**PK**, CASCADE) · `timezone` · `notification_prefs` JSONB · `updated_at`

## 3. Invariants

Enforced by constraint where possible, by transactional service logic where not. Each has a test.

| # | Invariant | Mechanism |
|---|---|---|
| 1 | A rule cannot be `active` without ≥1 eligible source and ≥1 eligible destination | Service check in activation transaction + trigger |
| 2 | All of a rule's sources and destinations belong to the rule's connection | FK + `CHECK` via trigger comparing `connection_id` |
| 3 | A job references an existing rule, source chat, destination chat, and connection | FKs |
| 4 | One idempotency key cannot produce two successful deliveries | `UNIQUE (idempotency_key)` |
| 5 | Deleting a connection disables dependent rules and prevents new jobs | `ON DELETE` + transactional cascade to `status='disconnected'`, pending jobs → `skipped` |
| 6 | Chat authorization changes affect future jobs immediately | Pre-delivery revalidation reads `connection_chat_access` |
| 7 | A chat cannot be both source and destination of the same rule *unless* explicitly confirmed | Service validation — silent loops are a real hazard |
| 8 | `delay_ms` is bounded | `CHECK` constraint |
| 9 | No paused/disconnected rule produces new jobs | Listener filters on `status='active'`; asserted in tests |
| 10 | Secrets are never returned by a serializer | Ciphertext columns excluded from all Pydantic response models; test asserts this by reflection |

## 4. Retention

| Data | Retention |
|---|---|
| `forwarding_events` | 90 days rolling (configurable), then deleted by a scheduled job |
| `forwarding_jobs` terminal rows | 30 days, then deleted; aggregate counts preserved in events |
| `auto_reply_log` | 30 days — comfortably longer than any usable cooldown, so purging can never let the same person be answered twice |
| `idempotency_keys` | 24 hours |
| `audit_events` | 365 days |
| `app_sessions` expired | 30 days |
| Sessions/tokens on disconnect | Deleted immediately, plus MTProto `auth.logOut` |

## 5. Sensitive fields

`telegram_connections.bot_token_ciphertext`, `telegram_sessions.session_ciphertext`,
`telegram_sessions.wrapped_dek`, `telegram_chats.access_hash_ciphertext`, `users.password_hash`,
`app_sessions.token_hash`, `telegram_connections.phone_hash`.

Never logged, never serialized, never exported by the data-export flow in raw form.
