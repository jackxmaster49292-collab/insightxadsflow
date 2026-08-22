# Architecture Decision Record

**Status:** Implemented.
**Last updated:** 2026-08-22

Each decision: context → decision → consequence. Superseding a decision means adding a new entry, not
editing an old one.

---

### ADR-001 — Greenfield, no inherited conventions
**Context.** The working directory was empty and not a git repository. Nothing to inspect or preserve.
**Decision.** Treat as greenfield; every stack choice is explicit and recorded here.
**Consequence.** No "existing repository convention" can be cited later to justify an unexplained choice.

### ADR-002 — Support both Bot API and MTProto, cleanly separated
**Context.** A bot cannot read a channel it was not added to; an account-only design forces every
customer to expose a personal account.
**Decision.** Both, behind one adapter interface with two implementations. The active type is always
visible. No silent fallback between types or accounts, ever.
**Consequence.** Larger adapter surface and a phone/2FA flow in MVP. Accepted — it is what the real use
case requires. *(Confirmed with the customer 2026-08-22.)*

### ADR-003 — Telethon for MTProto; Pyrogram rejected
**Context.** The brief named both Telethon and Pyrogram. Verified against PyPI on 2026-08-22:
Telethon 1.44.0 (2026-06-15, steady cadence); **Pyrogram 2.0.106 (2023-04-30 — no release in ~3.3
years)**; Kurigram 2.2.25 is an active community fork.
**Decision.** Telethon. Pyrogram rejected as unmaintained; Kurigram rejected as a smaller trust surface
for code that holds account credentials.
**Consequence.** Deviates from the brief's implied option set. This is exactly the "verify the library
is currently maintained" instruction being followed rather than the stale assumption.

### ADR-004 — Python backend; all-TypeScript rejected
**Context.** gramjs (`telegram` on npm) last released 2025-02-12, ~18 months stale. It is the only
serious Node MTProto client.
**Decision.** Python 3.12 backend (FastAPI + Telethon + aiogram). Node is used only to build the SPA.
**Consequence.** Two languages in the repo. Justified: the MTProto client quality gap is decisive, and
account credentials are the highest-value asset in the system.

### ADR-005 — FastAPI + Vite/React SPA
**Context.** Needs typed API, async I/O, live-updating status views, minimal ops.
**Decision.** FastAPI (async, generates the OpenAPI spec from the same models the code uses) plus a
Vite + React + TypeScript SPA served as static files by nginx.
**Consequence.** One backend language, one build step, clean API/UI boundary. No SSR — acceptable for
an authenticated control panel. *(Confirmed with the customer 2026-08-22.)*

### ADR-006 — Postgres is the job store; Redis coordinates only
**Context.** Jobs must survive Redis loss and be queryable for the UI.
**Decision.** `forwarding_jobs` in Postgres is the source of truth. Redis holds queue signalling,
locks, and rate-limit buckets. Redis payloads carry only job ids, never job content.
**Consequence.** Slightly more DB load; in exchange, a Redis flush loses nothing durable and the queue
message-tampering threat (T8) is largely designed out.

### ADR-007 — Idempotency key excludes `rule_version`
**Context.** Editing a rule must not re-deliver already-forwarded messages, but a worker still needs to
know a job predates the current configuration.
**Decision.** Key = `sha256(rule ‖ source peer ‖ source message ‖ destination peer)`. `rule_version` is
stored as a separate column and used to re-evaluate filters before sending.
**Consequence.** Edits never cause duplicates; stale jobs are still filtered correctly.

### ADR-008 — Copy mode is refused for protected content
**Context.** `copyMessage` can technically reproduce content that `forwardMessage` refuses.
**Decision.** If a source reports `has_protected_content` / `noforwards`, **both** forward and copy are
refused, with a clear skip reason.
**Consequence.** A capability a competitor might ship is deliberately not built. This is the
content-protection boundary in the brief, made structural rather than advisory.

### ADR-009 — Ambiguous Bot API timeouts fail closed
**Context.** On timeout the delivery may have succeeded. MTProto has server-side `random_id` dedupe;
the Bot API has no idempotency token.
**Decision.** MTProto: persist `random_id` with the job and reuse it on retry. Bot API: mark
`needs_attention` and surface a manual retry rather than risk a double post.
**Consequence.** A rare visible gap instead of a rare silent duplicate. Correct for a forwarding
product, where a duplicate broadcast is the worse failure.

### ADR-010 — Single-owner listener lock
**Context.** Telegram returns 409 for a second `getUpdates` on one token; two MTProto clients on one
session duplicate updates.
**Decision.** Redis lock per connection with heartbeat renewal; only the holder opens a client.
**Consequence.** The listener scales horizontally without duplicate intake, and a crashed listener's
connections are picked up automatically when the lock expires.

### ADR-011 — Chats keyed by `(peer_type, peer_id)`
**Context.** Telegram's peer docs: the id is 64-bit and *"the ID sequences of users, chats and channels
overlap, so you must use separate tables/hashmaps."*
**Decision.** Composite key everywhere; `BIGINT` in Postgres; serialized as a **string** in JSON.
`access_hash` stored per connection because it is account-specific.
**Consequence.** Removes a whole class of cross-peer collision bugs, and no JS `Number` precision risk.

### ADR-012 — Long polling by default for bots
**Context.** Webhooks need a public HTTPS endpoint and complicate local development.
**Decision.** Persistent `getUpdates` long polling (`timeout=25`) with the offset committed only after
durable persistence. Webhook mode is a documented V1 option.
**Consequence.** Works behind NAT, simple local dev, crash re-reads rather than loses. Not a
low-frequency cron — it is a continuously running poller, as the brief requires.

### ADR-013 — No object storage in MVP
**Context.** Forward-by-reference means media never needs to touch our disk.
**Decision.** No media staging in MVP. If copy mode later needs re-upload, add private storage with
signed expiring URLs at that point.
**Consequence.** Removes the insecure-file-handling threat (T14) from MVP entirely. Cost: copy mode is
limited to what the API can re-send by `file_id` / reference.

### ADR-014 — Server-side sessions, not stateless JWTs
**Context.** The brief requires real revocation and session management.
**Decision.** `app_sessions` rows keyed by a hash of the cookie value.
**Consequence.** One DB read per request (cheap, indexed); logout and "revoke all" are immediate rather
than waiting for token expiry.

### ADR-015 — No task-queue library; Postgres claim with `SKIP LOCKED`
**Context.** ARQ was the planned queue, but it pins `redis<6` and would have kept a
second, parallel record of work beside `forwarding_jobs` — which ADR-006 already makes the source of
truth.
**Decision.** Drop the queue library. Workers claim due rows with
`SELECT … FOR UPDATE SKIP LOCKED` plus a lease and heartbeat. Control-plane commands (sync, health
check, disconnect, retry) use the same mechanism via a `control_tasks` table, which is how the API
returns `202` without touching Telegram.
**Consequence.** One fewer dependency, one bookkeeping system instead of two, no Redis version
constraint, and crash recovery is just lease expiry. Cost: we own ~150 lines of claim/reclaim logic.
Redis is now purely coordination — locks, pacing buckets, rate limits — and losing it loses nothing
durable.

### ADR-016 — Capability statements live outside the adapters
**Context.** "What can this connection actually reach?" is a product statement the UI renders
verbatim, but it was originally duplicated inside each adapter, so the mock disagreed with the real
ones and the difference was invisible until a test caught it.
**Decision.** `app/adapters/capabilities.py` owns the text; bot, user, and mock adapters all return
`capabilities_for(kind)`.
**Consequence.** The panel shows the same honest limits regardless of provider, and the mock is a
faithful stand-in rather than an optimistic one.

### ADR-017 — The mock provider is deterministic across processes
**Context.** The mock's scripted state lives in process memory. The API and the worker are separate
containers, so a sync dispatched by the API and executed by the worker found an empty chat list —
the documented local setup produced an empty screen.
**Decision.** `mock_script_for()` seeds a chat set derived deterministically from the connection id,
including one content-protected channel so the refusal path is visible locally. Tests override
`script.chats` and are unaffected.
**Consequence.** `make up-all && make seed` is genuinely demonstrable end to end. Paired with
`python -m app.devtools emit`, which injects a source message through the real dispatch path, the
whole pipeline can be exercised locally without ever contacting Telegram.

### ADR-018 — nginx re-resolves the API upstream per request
**Context.** With a literal `proxy_pass http://api:8000`, nginx resolves the name once at startup and
caches the IP. Recreating the API container made every request 502 until nginx was restarted too —
found by running an actual deploy, not by reading the config.
**Decision.** Use Docker's embedded resolver (`127.0.0.11`) and assign the upstream to a variable,
which forces per-request resolution.
**Consequence.** A rolling API restart no longer takes the panel down.

### ADR-019 — The control panel moves into Telegram: bot + Mini App
**Context.** The operator wants to manage forwarding from Telegram itself rather
than a separate website.
**Decision.** Two surfaces over the same backend. A dedicated **admin bot** gives
status, pause/resume/retry and browsing through inline keyboards, and pushes
alerts. A **Mini App** button opens the existing React panel *inside* Telegram
for anything needing secrets or multi-step input. The forwarding engine is
unchanged.
**Consequence.** The React panel is reused rather than discarded, and alerts now
reach the operator instead of waiting to be noticed. Cost: two surfaces to keep
consistent, and the Mini App needs an HTTPS origin.

### ADR-020 — Secrets never enter a Telegram chat
**Context.** A pure-bot flow would have to ask for the bot token, phone number,
login code and 2FA password as chat messages — putting all of them into
Telegram's stored message history. Deleting afterwards does not undo the
transmission.
**Decision.** Every credential is entered in the Mini App over HTTPS. The bot has
no handler that accepts a secret, so there is no path to leak one.
**Consequence.** The Mini App becomes mandatory for setup, which is the price of
not weakening credential handling relative to the web panel.

### ADR-021 — Telegram identity replaces passwords, allowlist replaces roles
**Context.** A bot has no cookies and no login form, and anyone on Telegram can
message it.
**Decision.** Authenticate with Mini App `initData`, verified by HMAC against the
admin bot token, then authorize against `ADMIN_TELEGRAM_IDS`. Accounts created
this way store `password_hash = NULL`, and the password login path requires a
stored hash, so they are unreachable through the form. The allowlist middleware
is registered on **both** the message and callback observers — a callback does
not pass through message middleware, and missing it would leave every button
unguarded.
**Consequence.** No password to phish on this surface, and verification is
cryptographic rather than a claim. The trade is that Telegram account compromise
equals panel compromise, stated plainly in the docs.

### ADR-022 — Alerts go through a database outbox
**Context.** The worker detects a safety pause, but the bot owns the Telegram
connection, and calling the Bot API from the worker would lose alerts on a crash.
**Decision.** The worker writes `admin_notifications`; the bot drains and sends.
A unique `dedupe_key` collapses repeats.
**Consequence.** Alerts survive a bot restart, and a rule failing in a loop
produces one message rather than a storm — the same durability rule the
forwarding pipeline already follows.

### ADR-023 — The Mini App is removed; the bot is the only admin surface
**Context.** ADR-019 kept a React panel inside Telegram for anything needing
secrets or multi-step input. In practice that meant the product could not be set
up without a domain, a TLS certificate and a reverse proxy — and the operator
asked for everything to happen in the bot itself.
**Decision.** Supersedes ADR-019. Delete the Mini App, the React frontend, the
nginx container and Caddy. Every flow — connecting a bot, connecting an account
by phone number, composing an ad, choosing groups, editing the auto-reply,
managing rules — is an aiogram FSM conversation. The HTTP API stays, unpublished,
as the service layer the tests drive and the health endpoint.
**Consequence.** Deployment needs no domain, no certificate and no open port; the
stack only makes outbound connections. Cost: multi-step input in a chat is
clumsier than a form, and long lists need paging because `callback_data` is
capped at 64 bytes.

### ADR-024 — Credentials are accepted in the chat, deleted immediately, and the
tradeoff is stated
**Context.** ADR-020 said secrets must never enter a Telegram chat, and made the
Mini App mandatory for that reason. With the Mini App gone (ADR-023) the choice
is between accepting credentials in the chat or having no way to connect an
account at all. The operator was told plainly that chat history lives on
Telegram's servers and asked for the bot flow anyway.
**Decision.** Supersedes ADR-020. Bot tokens, phone numbers, login codes and 2FA
passwords are accepted as chat messages. Every prompt that asks for one first
states that the message will be deleted and that it was on Telegram's servers
regardless. The message is deleted the moment it is read; the value is passed
straight to the service layer and never written to FSM state, a log, or the
database. `app/adminbot/secrets.py` is the only place any of this happens.
**Consequence.** This is genuinely weaker than the HTTPS form it replaces, and
the code says so rather than implying otherwise. Deletion is best-effort by
definition — Telegram refuses to delete another account's message after 48 hours
— so it narrows the window rather than closing it.

### ADR-025 — A broadcast reuses the delivery machinery, not the forwarding tables
**Context.** "Ads" — posting the operator's own message to chosen groups — needs
per-destination durability, pacing, retry classification, flood-wait obedience
and a pre-send permission check. Those are exactly what `forwarding_jobs`
provides. The obvious move was to make `source_chat_id` nullable and reuse it.
**Decision.** Separate `broadcasts` and `broadcast_targets` tables with the same
claim/lease shape, and a separate `execute_target` that shares the error
taxonomy, backoff and safety-pause helpers. `execute_job` is left alone.
**Consequence.** Some structural duplication between two ~150-line functions,
accepted deliberately: `execute_job` is the most safety-critical function in the
system, and threading "is this a broadcast?" conditionals through its
stale-version handling, content-protection refusal and album logic would make
both paths harder to reason about. The two share everything that is genuinely
common and nothing that is not.

### ADR-026 — Auto-reply can only answer, never initiate
**Context.** An ad brings people to the account; answering them by hand does not
scale. Every nearby product design — a recipient list, an import, a "message
everyone who ever wrote" button — is unsolicited messaging.
**Decision.** The auto-reply module has exactly one entry point,
`handle_incoming(sender=...)`, called only from the listener with a message that
has already arrived. A non-private chat is refused outright. A per-person
cooldown, held in Postgres rather than a cache, means one answer per person per
window even across a restart. A guard test pins the module's public surface and
fails if a function taking a list of recipients is ever added.
**Consequence.** The safety property is structural rather than a matter of
review: there is no code path that can address someone who did not write first,
and adding one breaks the build.

### ADR-027 — Broadcast images are stored as bytes, not a Telegram file id
**Context.** The natural thing is to keep the `file_id` of the photo the operator
sent to the panel. A `file_id` is scoped to the bot that received it, so the
admin bot's id is meaningless to the account or bot that does the posting.
**Decision.** The admin bot downloads the image once at compose time and stores
the bytes in `broadcasts.media_bytes`, capped at 5 MB. Each delivery re-uploads
them.
**Consequence.** Bytes in Postgres, which is not where large media belongs, and
re-uploading per group costs bandwidth. Accepted for a single ad image: the
alternative is object storage (rejected in ADR-013) for one small blob per
broadcast.

### ADR-028 — Picker buttons address a group by index, not by id
**Context.** Telegram caps `callback_data` at 64 bytes and rejects the entire
keyboard — not just the offending button — when one exceeds it. A toggle button
carrying both a broadcast id and a chat id is 69 bytes even with the dashes
stripped.
**Decision.** The picker stores the ordered chat ids in FSM state and buttons
carry the index into that list (`pk:t7`). Selection lives in FSM state too,
because `callback_data` cannot hold a list.
**Consequence.** Every picker callback is under ten bytes regardless of how many
groups exist. The ordering becomes load-bearing: the list a keyboard was built
from is the list its callbacks resolve against, and an out-of-range index is
ignored rather than raising.

---

## Open tradeoffs

1. **MTProto account risk.** No engineering can remove it. Mitigated by conservative pacing, full
   flood-wait obedience, and honest UI language — recommend a bot connection wherever it suffices.
2. **Many-to-many rules.** MVP ships one-source→many-destinations. Many-to-many is deferred until the
   one-to-many reliability model is proven, per the brief.
3. **Album completeness.** A 2s buffer is a heuristic; a straggler emits as `partial_album` rather than
   being dropped or held indefinitely. Window is configurable.
4. **Postgres-as-queue scaling.** Fine to roughly thousands of jobs/minute on one node. If it ever
   becomes the bottleneck, move signalling to Redis Streams while keeping Postgres authoritative.
5. **Edited source messages.** MVP does **not** propagate edits — an edit after forwarding leaves the
   destination copy stale. Needs a product decision before V1.
6. **Credentials in chat history.** ADR-024 accepts a real weakening relative to an HTTPS form, at the
   operator's explicit request. Rotating a bot token after setup is cheap and worth doing; a phone
   number cannot be rotated, so the login-code and 2FA messages are the exposure that matters.
7. **One connection drives ads and auto-reply.** The panel picks the first active connection rather
   than asking. Simple and matches the intended use, but a second account cannot yet run its own ad
   with its own reply.
8. **Broadcast media in Postgres.** ADR-027. Fine for one image per ad; a video or a document library
   would need the object storage ADR-013 deferred.
