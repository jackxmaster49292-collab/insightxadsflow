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

### ADR-029 — The allowlist becomes an operator list; access is a deployment mode
**Context.** ADR-021 made ``ADMIN_TELEGRAM_IDS`` both the identity and the
authorization model: those ids were the only people who could use the bot at
all. The operator then wanted ordinary people to use it too, which that design
has no room for.
**Decision.** Supersedes the authorization half of ADR-021. ``ACCESS_MODE``
decides who may use the bot — ``closed`` (the operator ids only) or ``open``
(anyone). ``ADMIN_TELEGRAM_IDS`` keeps its identity role and now means
*operator*: someone who can list accounts and suspend one. Default is
``closed``, so pulling this version cannot silently open an existing
deployment; opening it is an explicit line in ``.env``.
**Consequence.** Multi-tenancy was already structural — every repository is
``user_id``-scoped — so the change is in the gate, not the data model. What is
genuinely new is that data isolation is now load-bearing rather than
theoretical, which is why the bot surface gets its own isolation tests.

### ADR-030 — Nobody uses the tool before reading what it does
**Context.** With open access, most people arrive knowing nothing about the
tool. The two facts that matter to them are not obvious: it posts from *their*
Telegram account, and Telegram can restrict that account if the messages are
reported.
**Decision.** A new account sees one screen and nothing else until it accepts.
The gate lives in the middleware, not in handlers, for the same reason the
access check does — a handler can forget. The accept callback is the single
exemption. The text is a plain statement of what happens and where the
responsibility sits, not legal cover: it says the account risk is theirs, that
this software will not help them evade Telegram, that auto-reply cannot message
anyone who did not write first, and that credentials typed into the chat were on
Telegram's servers for a moment.
**Consequence.** One extra tap before first use, and a defensible record that
every account was told. It also puts the product's actual boundaries in front of
the person most likely to test them.

### ADR-031 — Suspension stops queued work, and reinstatement resumes nothing
**Context.** An operator decides to stop an account while it has rules running
and a broadcast halfway through two hundred groups. Setting a flag would leave
all of that going.
**Decision.** Suspending pauses their rules, pauses sending broadcasts, cancels
queued jobs and targets, and drops their connections out of the listener's
intake query so the Telethon client is released. Every delivery path
additionally re-checks the owner immediately before sending, so a row the
cascade somehow missed still cannot go out. Reinstating deliberately resumes
nothing — the person restarts what they want, and can see what is paused and
why.
**Consequence.** Suspension is a real stop rather than a flag, and the extra
per-delivery check is one indexed primary-key lookup. Auto-resuming a broadcast
someone was suspended over is the wrong default, so it is not offered.

### ADR-032 — An operator sees counts, never content
**Context.** Moderating an open deployment needs enough signal to spot an
account behaving unlike the others. The tempting version shows the operator
everything.
**Decision.** The user screens show connection, rule and broadcast counts, join
date, terms status and suspension state. They do not show ad text, group lists,
rule configuration or any message. Suspension does not require any of it.
**Consequence.** An operator cannot investigate a specific complaint from inside
the panel, which is the deliberate trade: reading everyone's messages to handle
the rare report is a worse default than not being able to.

### ADR-033 — Per-account connection ceiling, and a throttle on the bot itself
**Context.** Open access removes the assumption that the only user is
trustworthy. Two resources have no natural bound: MTProto connections, each a
live Telethon client holding a socket and update state in the listener, and
updates to the bot itself.
**Decision.** ``MAX_CONNECTIONS_PER_USER`` (default 3) and a per-person
rate-limit bucket on bot updates (60/minute), reusing the existing Redis
limiter.
**Consequence.** Both are operational controls in the same category as
``worker_concurrency`` — they bound what one account can pin down. Neither is a
product or monetization limit, and the guard test that bans plan/quota/tier
vocabulary still passes.

### ADR-034 — QR is the account sign-in; the phone code cannot work from a chat
**Context.** The phone/code flow failed in production with Telegram replying
*"the code was entered correctly, but sign in was not allowed, because this code
was previously shared by your account"*. Telegram cancels any login code it sees
an account send inside a Telegram chat. Since the panel **is** a Telegram chat,
typing the code there burns it before it can be used. The earlier mitigation —
asking people to space out the digits — does not work; Telegram's detection is
not a digit-pattern match.
**Decision.** QR sign-in becomes the default: the bot sends a QR image, the
customer scans it from *Settings → Devices → Link Desktop Device*, and Telethon
completes the login. Nothing secret enters the conversation, so there is no code
to cancel. Telegram's tokens expire in seconds, so a detached watcher refreshes
the code until it is scanned or the attempt times out. The phone route is kept
behind its own button, labelled with why it usually fails and when it does work
— connecting an account that is *not* the one messaging the bot.
**Consequence.** Sign-in works from inside Telegram without weakening anything;
if anything it is stronger, since a QR cannot be forwarded to an attacker the
way a code can. Costs: one small pure-Python dependency (``segno``) to render
the image, and a background task per attempt. A 2FA password is still typed,
because Telegram does not cancel those — it is deleted on read as before.

### ADR-035 — A failed sign-in must be clearable from the panel
**Context.** A partial unique index allows one in-progress connection attempt
per account. When the phone sign-in failed it left a row in ``awaiting_code``
forever, so every retry was refused with "already in progress" — and the panel
offered no way to remove it. The deployment was stuck with no path forward.
**Decision.** A connection in ``pending`` / ``awaiting_code`` / ``awaiting_2fa``
shows exactly one action, *Cancel sign-in*, which deletes the row and drops the
held client. Sync and health checks are hidden there, since neither means
anything on an account that never signed in. No confirmation: there is nothing
to lose, and the customer is usually looking at it precisely because they are
stuck.
**Consequence.** Every dead end in the sign-in flow now has an exit. The QR
watcher also clears the attempt on timeout or hard failure, so the common case
does not need the button at all.

### ADR-036 — QR removed; phone sign-in only, with its limit stated up front
**Context.** ADR-034 made QR the default because Telegram cancels any login code
it sees an account send inside a chat, which makes phone sign-in fail for the
account driving the bot. The operator asked for QR to be removed and the phone
flow kept, having been shown that consequence and the alternatives.
**Decision.** Supersedes ADR-034. The QR flow, its adapter methods and the
``segno`` dependency are removed. Phone → code → 2FA is the only account
sign-in. The constraint is not hidden: the very first screen, before a name or a
number is asked for, states that Telegram cancels codes sent in chats and that
this works only for an account *other* than the one messaging the bot. When a
code is rejected the message says the same thing and adds that retrying will
fail identically, rather than implying a typo.
**Consequence.** Connecting the operator's own account from inside the bot is no
longer possible — that is the accepted cost of the decision, not an oversight.
Connecting a second account still works. ADR-035's *Cancel sign-in* becomes
load-bearing rather than a convenience: a burned code leaves a half-finished
connection every time, and that is now the expected path rather than an edge
case. Anyone wanting to connect the driving account needs a sign-in surface
outside Telegram, which this deployment deliberately no longer has (ADR-023).

### ADR-037 — An ad carries Telegram's entities, not Markdown
**Context.** An ad composed with bold text and premium emoji was posted as plain
text with fallback emoji. Only ``body_text`` was stored, so everything Telegram
describes as an *entity* — bold, links, custom emoji — was dropped between
composing and posting.
**Decision.** Store the entities as data, in ``broadcasts.body_entities``, and
pass them to the send call. Not Markdown: markup cannot express a custom emoji
at all, and round-tripping through it corrupts any message containing a literal
asterisk or underscore. Offsets pass through untouched because the Bot API and
MTProto both count UTF-16 code units — recomputing them in Python's code points
would shift every entity after an emoji. A neutral ``TextEntity`` keeps Telegram
types out of the engine, and each adapter converts at its own edge.
**Consequence.** An ad is posted exactly as it was written. Premium emoji need
Telegram Premium on the sending account; Telegram rejects them otherwise, and
that rejection now names the cause rather than reading as a generic failure. An
entity type we do not recognise is dropped rather than guessed at — losing one
piece of formatting beats the whole message being refused.

### ADR-038 — An ad targets groups; forwarding may still target a channel
**Context.** Synchronizing an account discovers everything in its dialog list —
719 chats in the reported case, mostly private conversations and channels. The
group picker offered every chat the account could post in.
**Decision.** The ad picker is restricted to ``group`` and ``supergroup``. A
private chat was already impossible (``sync.NON_DESTINATION_KINDS``), which is
the part that matters: an ad in someone's DM is unsolicited messaging.
Excluding channels is a product choice — a channel you own is better posted to
directly. Forwarding rules keep the wider set, because copying into a channel
you run is a legitimate thing to want.
**Consequence.** The Groups screen shows groups and states how many other chats
exist, so the gap between "719 synced" and "40 listed" reads as a filter rather
than a bug. Private chats and channels stay synchronized, because forwarding
uses them as sources.

### ADR-039 — An ad repeats on a floor of one hour, measured from the round finishing
**Context.** The requested feature is an ad that posts to the same groups again
after an interval, indefinitely. Two ways to build it are wrong. Scheduling the
next round *N* minutes after the previous one **started** means a slow round
across 300 groups leaves almost no gap, and on a bad day round two begins while
round one is still running — the same group receives the ad twice in a row.
Allowing any interval means a five-minute repeat, which is not a schedule but a
flood.
**Decision.** ``repeat_every_s`` is measured from the moment the round finishes,
in ``settle()``. A repeat below ``min_broadcast_repeat_s`` (default one hour) is
refused, as is any repeat shorter than one round's own estimated duration —
both with the arithmetic in the message. ``0`` means post once, which stays the
default: repeating is turned on deliberately.
**Consequence.** The floor protects the customer, not the service: the account
posting the same message into the same group every five minutes is theirs, and
it is theirs that Telegram restricts. A repeating ad never completes on its own
— stopping it is always a decision someone makes, which is the point. Rounds
are counted (``repeat_count``) and the next start time is shown.

### ADR-040 — The next round is paced like the first
**Context.** Reopening a round is a bulk update, and the obvious implementation
gives every target the same ``not_before``.
**Decision.** ``reopen_for_repeat`` staggers by ``delay_ms`` from the target's
position, exactly as the first round is staggered.
**Consequence.** Without this, round two hands the worker all 500 targets at
once and posts to every group as fast as the connection allows — which is what
the pause between groups exists to prevent, on the round where it matters most.
Covered by ``test_the_next_round_is_paced_like_the_first``, verified by
restoring the bug. Every target is reopened, including ones that refused last
round: a refusal is a fact about a moment, and re-checking is how regained
permission gets noticed.

### ADR-041 — A preview is clipped by its escaped length
**Context.** The compose screen clipped the ad at 400 raw characters, hiding the
end of nearly every real ad. Raising the clip to 2800 raw characters would have
been a bug: MarkdownV2 escaping nearly doubles a body of punctuation, and a
4096-character reply is a 400 from Telegram — which blanks the whole screen,
the same failure class as ADR-021's unescaped underscore.
**Decision.** ``views.preview()`` budgets against the **escaped** length and
walks whole characters, so a cut can never land between a backslash and what it
escapes and leave a dangling one. Both ad screens and the auto-reply screen use
it.
**Consequence.** Ads now show whole. ``test_the_longest_possible_ad_still_fits_a_telegram_message``
renders a maximum-length ad of nothing but escapable characters on both screens
and asserts the result is under 4096; with the raw-length clip it produced 5952.
The preview is still plain text — a bot cannot render a custom emoji at all, so
the formatting summary remains the only honest way to confirm premium emoji
survived without posting the ad.

### ADR-042 — "Completed" is not "delivered", and the screens say which
**Context.** A broadcast reaches ``completed`` when its round has no work left —
including when every single target was refused. The list showed the same ✅ for
an ad that reached 500 groups and one that reached none, and the customer read
"succeeded" where the truth was "gave up".
**Decision.** ``delivery_icon`` corrects the status icon by the counts: finished
with attempts but zero deliveries is ⚠️, and the list shows ``delivered/attempted``
beside each ad. The detail screen adds a sentence — "2 of 5 did not receive it" —
and the Events screen now names the **group** beside each reason, because twelve
identical "not allowed to post" lines say something is wrong but not where.
Group titles are attacker-influenced and escaped like everything else.
**Consequence.** The status enum is untouched — ``completed`` still means the
machine finished — only its presentation stops implying delivery. Skips also
call ``settle()`` (they always did), so a fully-refused round completes rather
than hanging; what changed is that it no longer completes *quietly*.

### ADR-043 — A live ad is editable, and an edit pauses it first
**Context.** An ad that repeats for weeks will need its wording, groups or
interval changed. The only path was Stop → new ad → re-pick 500 groups.
**Decision.** ✏️ Edit reopens the same compose screen (one screen, so two
cannot drift apart). A ``sending`` ad is paused first with its own reason code —
edited mid-round, some groups get the old wording and some the new, with no
record of which. ``replace_targets`` now **keeps the row** of every group that
stays selected, preserving what already happened to it this round; only removed
groups lose their row. Save-and-resume re-queues, and only pending targets are
re-timed. Sending a ``completed``/``cancelled`` ad again reopens every target
first — without that there is nothing pending and the ad would sit in "sending"
forever with no work that could ever settle it; the confirm screen calls this
"Run this ad again?" and says every group is posted to again.
**Consequence.** The edit invariant is the delivery invariant: a group already
posted to this round is never posted to twice, verified by
``test_editing_the_groups_keeps_what_already_went_out``. Repeat intervals may
now also be given in minutes (``90m``, ``1h 30m``); the parser is strict because
an interval misread by a factor of sixty posts every minute instead of every
hour, from the customer's own account. The hourly floor is unchanged.

### ADR-044 — A check that errors is retried; only a check that refuses skips
**Context.** The pre-send eligibility check failed closed: any exception during
the check skipped the group permanently. Fail-closed is right — nothing may be
sent without a passing check — but the network blinking mid-check produced a
permanent "skipped", which the customer reads as "this group refused you" when
the truth was "nothing was learned". One group of 158 was lost to exactly this.
**Decision.** A *thrown* check goes through ``_handle_failure`` — the same
taxonomy as a failed send: transient errors retry with backoff (bounded by
``max_attempts``, then a visible dead letter), a Telegram wait is obeyed in
full, an auth failure pauses the connection, and a permanent error skips. A
check that *returns* a refusal still skips immediately: Telegram saying no is a
fact, and asking again does not change it.
**Consequence.** Fail-closed is untouched — a send still requires a check that
actually passed; what changed is that the check itself gets the retries the
send always had. Verified end to end: the check erroring leaves the target
``pending`` with nothing sent, and the next attempt delivers.

### ADR-045 — A flood-wait pause ends by itself
**Context.** A Telegram wait past ``flood_wait_pause_threshold_s`` paused the
whole broadcast — and nothing ever resumed it. The wait was obeyed and then the
ad sat paused until the customer noticed and tapped Resume, which read as the
tool stopping at random.
**Decision.** The pause gets its own reason code, ``BROADCAST_FLOOD_WAIT``,
whose text makes its promise: "posting continues by itself the moment the wait
is over". The scheduler's reclaim loop resumes such broadcasts once the
earliest pending target's ``not_before`` — set from Telegram's own number — has
passed. Only this code (and its legacy spelling) is swept: a pause the customer
chose, or one made for editing, ends when *they* say so, never by a sweep.
**Consequence.** The wait is still obeyed in full and never shortened — the
sweep runs every 15 s *after* the deadline, so if anything the wait runs long.
Guarded by ``test_the_sweep_never_resumes_a_pause_the_customer_chose``.

### ADR-046 — The per-group report
**Context.** "1 of 158 did not receive it" answers *how many*; the customer's
actual questions are *which group* and *why that one* — and, symmetrically,
"did group X get it?".
**Decision.** 🧾 Groups on the ad screen lists every target by name with its
outcome: problems first with their reason under them, then groups still
waiting, then delivered ones. Ten per page keeps a page of hostile-length
titles under Telegram's 4096. Titles are escaped like everything else.
**Consequence.** Sorting problems first means the one refused group leads page
one instead of hiding on page fourteen. The Events screen remains the
chronological view; this is the per-group one.

### ADR-047 — Premium panel icons: extracted, applied centrally, degraded honestly
**Context.** The operator asked for the panel's own unicode icons to render as
Telegram custom (premium) emoji. Two Telegram rules bound what is possible for
*any* bot: button labels cannot carry entities at all, and a bot may only send
custom emoji in message text if it owns a **Fragment username** — otherwise
Telegram rejects the whole message.
**Decision.** Three parts. *Extraction*: custom emoji ids are Telegram
documents, so nothing is hardcoded — an operator taps ✨ Icons and the ids are
fetched through their own connected account (``messages.searchCustomEmoji``,
one query per icon the panel draws) and stored in ``panel_emoji``; rows present
is the on-switch. *Application*: one transform at the send boundary rewrites
every mapped emoji in outgoing message text as ``![🔥](tg://emoji?id=N)`` — a
single regex pass, longest emoticon first, so a variation-selector form cannot
be wrapped twice and the transform never revisits its own output. Applied
centrally in ``_deliver`` rather than in forty views, so coverage is every
screen at once; button labels are separate objects and stay plain
automatically. *Degradation*: if Telegram rejects a premium message — the
Fragment rule, or length growth — the transform is suspended for the process,
the plain text is resent, and the operator screen says exactly why the icons
are not showing. A degraded icon is a shrug; a blank panel is an outage.
**Consequence.** On a bot with a Fragment username the whole panel upgrades,
including emoji inside ad previews that happen to be in the extracted set. On
any other bot the first premium send fails once, quietly, and everything stays
plain — with the reason stated on the ✨ Icons screen instead of left to be
discovered. The MarkdownV2 checker learned the custom-emoji token; the fallback
emoji embedded in each token is what renders anywhere the premium one cannot.

### ADR-048 — Button icons too: Bot API 10.2 corrected ADR-047's premise
**Context.** ADR-047 stated buttons can never carry custom emoji. The operator
pushed back, and the installed Bot API 10.2 proves them right: 
``InlineKeyboardButton`` (and ``KeyboardButton``) gained ``icon_custom_emoji_id``
— an icon drawn before the label — usable by bots with a Fragment username *or*
in messages the bot sends directly when the bot's **owner has Telegram
Premium**. The panel's screens are exactly such messages. The old
Fragment-only note on message entities is gone from the current API docs.
**Decision.** The same central transform now upgrades keyboards: a button whose
label leads with a mapped emoji gets the icon and loses the leading emoji from
its text, so it is not drawn twice. A button whose label is *only* an emoji
(the pager arrows) is left alone — a button must keep visible text. The
transform copies buttons rather than mutating them, so cached Screen objects
stay plain. The fallback resends both the original text **and** the original
keyboard on rejection.
**Consequence.** On this deployment the owner has Premium, so the panel's
icons — text and buttons — should render premium with no Fragment purchase.
Verification of a claim against the installed API, not against memory, is what
settled this; memory said no and was out of date.

### ADR-049 — Icons arrive by message, and button labels live in the database
**Context.** The ✨ Icons screen demanded a connected account for extraction —
but the account an operator drives the bot from cannot connect itself
(ADR-036: Telegram burns any login code it sees an account send in a chat), so
a solo operator was locked out of their own feature. Separately, the operator
asked for every button label to be editable, stored in the database.
**Decision.** *Icons:* a second extraction path that needs no login and no
connection — the operator taps 📥 Send emojis and sends premium emoji from
their own keyboard. A custom emoji is a character plus an entity naming the
premium document, so the ids ride in on the message itself; the collector
slices characters by **UTF-16 offsets** (Telegram's counting — an emoji is a
surrogate pair there, and slicing by Python index would map the wrong
character). Messages merge, so icons can be added over several sends.
*Labels:* a ``panel_buttons`` table keyed by the built-in default text — the
one identity a button keeps across forty screens. 🔤 Buttons lists every
renameable label; the transform swaps exact matches centrally in the send
path, before the premium-icon pass (whose stripping would otherwise break the
keys). ``-`` resets to the built-in.
**Consequence.** No row means the built-in label — a default, not a fallback;
custom labels are plain text with no rejection risk, so they persist through
the premium-icon fallback and only the icons ever degrade. The renameable list
is append-only, because callbacks carry indexes into it.

### ADR-050 — A batch must not be weighed against its own ceiling
**Context.** An ad across 150 groups showed "1 leased · 145 pending": one group
in flight at a time, whatever the settings said. The cause was in the claim
path, not the configuration. ``claim_batch`` leases the whole batch first, and
``in_flight_count`` then counted **those same rows** as in-flight, so the
ceiling was measured against the very work being admitted. The first target
already saw the batch at or over the limit, and admission collapsed towards
one no matter how high the ceiling was raised. Forwarding had the identical
defect.
**Decision.** ``in_flight_count`` takes ``exclude_ids``; both claim paths pass
the batch's own ids, so the baseline is *other* work only. Separately,
broadcasts get their own ceiling (``broadcast_inflight``, 8) above the
forwarding one, justified by the shape of the work: every target of a
broadcast is a **different** group, so Telegram's per-group limit (20/min) can
never bind and the connection-wide pacer (~30/s) is the real gate. The
combined count is kept, so a connection doing both cannot run two budgets.
**Consequence.** A 150-group round on Fast pacing (250 ms) finishes in under a
minute instead of seven and a half. Nothing about limit-obedience changed: the
pacer, every FloodWait, and the fail-closed pre-send check are untouched.
Verified by restoring the bug — the concurrency test fails ``1 == 8``, which is
the screenshot.

### ADR-051 — Speed is a preset with its arithmetic shown, and a round reports itself
**Context.** ``delay_ms`` is a number whose meaning only appears once
multiplied by the group count — 3 s reads as harmless and is seven minutes
across 150 groups. And a round that takes a minute or an hour ended silently:
the outcome, which is the entire reason for running it, had to be discovered
by opening a screen.
**Decision.** ⚡ Speed offers Fast (250 ms) / Normal (3 s) / Careful (10 s),
each button labelled with what it means *for this ad's group count*, plus
Custom. ``min_broadcast_delay_ms`` (250 ms) is where the dial stops: below it
the gain is seconds across a whole round and the risk is the customer's own
account being read as a flood. When a round settles, an alert is pushed with
delivered/total, how many missed it, and where to look — deduped by round
number, so a repeating ad reports once per round and a re-settle cannot
double-send.
**Consequence.** The screen states the honest ceiling rather than implying
simultaneity: one account over one connection sends one message after another,
several in the air at once, ~4/s on Fast — an order of magnitude under
Telegram's documented rate. Counts are taken before ``reopen_for_repeat``,
which is the only moment a round's outcome exists in full.

### ADR-052 — A public face, and a role question asked once
**Context.** The bot had no profile description and no route to support,
updates or policy links, and ``/start`` dropped everyone straight into an
advertiser panel regardless of why they came.
**Decision.** ``setMyDescription``/``setMyShortDescription`` are published at
startup, best-effort like the command menu. Both are **plain text** — Telegram
allows no entities there, so no clickable links and no custom emoji whatever
the panel's own screens can do; the links go in as bare URLs and the clickable
versions live on an About screen inside the bot, where URL buttons work.
``/start`` asks which side you are on — Advertiser, Publisher/Group owner,
Insights — but **only on a first start**: once an account or an ad exists the
question is answered, and re-asking would put a question in front of the thing
someone came to use. Links come from config; an unset one produces no button.
**Consequence.** The 512/120-character ceilings are asserted with every link
configured, because an over-long description is refused wholesale and the
profile silently keeps its previous text.

### ADR-053 — The publisher side says it is not built
**Context.** "Publisher / Group owner" implies a marketplace: listings,
pricing, payment held until delivery, moderation. None of it exists — every
line of this product posts *your* message to groups *you* joined.
**Decision.** The role exists on the first screen and its own screen states
plainly that it is not open, describes what it would need, and points at the
Advertiser side which is fully built. A "tell me when it opens" button records
``publisher_interest_at`` — a timestamp rather than a flag, so an operator can
see demand over time, which is the only thing that would justify building it.
**Consequence.** A screen that looked like a feature and did nothing would
cost more trust than an empty one that is honest. The third role is named
**Insights** and shows the delivery numbers the bot already records — not
"analyst" and nothing implying analysis the code does not perform. "Campaign"
remains guard-banned vocabulary (``test_the_word_campaign_is_not_used``), which
is why the operator's suggested name for it was not used.

### ADR-054 — The panel fetches its own icons, from an operator's account only
**Context.** ADR-047 shipped extraction behind a button, and ADR-049 added a
send-the-emoji path. Both still required a person to do something, and the
operator's objection was fair: the ids come from Telegram either way, and there
is nothing a human contributes to the process.
**Decision.** ``icon_setup.ensure_icons()`` runs in the background at panel
startup: if ``panel_emoji`` is empty and an **operator's** active user account
exists, it asks Telegram for a custom emoji matching each icon the panel draws
and stores the result. Backgrounded, because forty lookups must not stand
between the process starting and the panel answering; skipped entirely when a
map already exists, so a restart is not forty needless calls; paced at 100 ms,
because a burst of forty is the kind of thing a rate limiter notices. Both
manual paths remain and now share the same fetcher, so they cannot drift.
**Consequence.** Only an operator's connection is ever used. Decorating the
deployment's own panel with a *customer's* Telegram session would be using
their account for something they never asked for, and
``test_only_an_operators_account_is_ever_used`` holds that line. Everything is
best-effort: no operator account, a Telegram failure mid-fetch, or an emoji
with no premium version each leave the panel plain rather than broken — a
partial map is the normal outcome, not a failure.

### ADR-055 — A custom-emoji id is accepted as input, everywhere text is
**Context.** Automatic extraction found 1 icon of 43 on a real account, and the
operator had ids in hand but no way to use them: pasted into a button label the
digits rendered as digits, and there was no way to put a given id into an ad at
all.
**Decision.** Ids are accepted in three places, all sharing one parser.
``![🔥](tg://emoji?id=N)`` — Telegram's own MarkdownV2 spelling — is parsed into
real entities wherever text is taken, with offsets in **UTF-16 code units**
because that is Telegram's unit and an emoji is a surrogate pair there. The
icon collector also takes typed ``<emoji> <id>`` pairs, since an id alone
cannot say which panel icon it replaces. Renaming a button treats an id as the
button's ``icon_custom_emoji_id`` — a separate column, because Telegram draws
it from a separate field — and refuses an icon with no words, which Telegram
would reject anyway.
**Consequence.** A bare id is only read as one at 15-20 digits. Prices, counts
and years in an ad stay text, which the ``$ 0.5`` and ``2026`` cases pin down.
An icon chosen by hand for one button beats the automatic emoji-to-icon pass.
Markup and keyboard-inserted formatting in the *same* message are an either/or:
rewriting markup shifts every later offset, and silently misplacing bold text
would be worse than not serving that combination.

### ADR-056 — Icons come from the account's own packs first
**Context.** ``messages.searchCustomEmoji`` returned 1 of 43 icons on a live
account. It surfaces what Telegram *suggests*, which is sparse.
**Decision.** ``installed_custom_emoji()`` walks the account's installed emoji
packs (``messages.getEmojiStickers`` then ``getStickerSet``) and keys every
document by its ``alt`` — the plain emoji it is drawn in place of. One pass
covers most of a panel. Only what the packs miss is then searched one emoticon
at a time, at the old pace.
**Consequence.** The Bot API has neither call, so ``BotAdapter`` returns empty
for both and says so. A partial map remains the normal outcome.

### ADR-057 — The archive keeps the post, not a list of links
**Context.** The operator asked for the links of their posts to arrive
automatically in a group of their own, "in case my account is deleted, so at
least I still have the old posts". That reason decides the design, because a
list of links does not serve it: ``t.me/c/<id>/<msg>`` opens only for *members*
of the group, so from a deleted account every one of those links is dead. Links
to public groups survive; links to private ones do not.
**Decision.** The archive posts **the ad itself** into a group the customer
keeps — same text, same entities, so bold and premium emoji survive — and only
then an index of where it went. The copy is the artifact that outlives the
account; the index merely points at other people's groups. Each line states
what its link is worth: a public ``t.me/<username>/<id>``, a private
``t.me/c/…`` marked *members only*, or, for a basic group, that Telegram
publishes no message-link form at all. Sent by the same connected account that
posted the ads, into a group it already belongs to, so no bot invite is needed.
**Consequence.** The archive runs inside ``settle()`` **before**
``reopen_for_repeat``, which is the only moment the round's
``destination_message_id`` values still exist — afterwards they are cleared and
the links are gone. Verified by moving the call after the reopen: the repeating
case then archives nothing. Best-effort throughout: the ads are already
delivered by the time this runs, and a missing copy is a smaller loss than a
round marked failed over its own bookkeeping. Only groups that actually
received the ad are indexed — a record that claimed otherwise would be a lie in
the one place kept as evidence.

### ADR-058 — A supergroup id without the -100 prefix produces no link
**Context.** ``t.me/c/`` addresses a supergroup by its internal id, which is
``peer_id`` with Telegram's ``-100`` prefix removed.
**Decision.** If the prefix is absent, no link is returned rather than one
built from the raw number.
**Consequence.** Stripping a prefix that is not there would silently address a
*different* chat — a link in a permanent record pointing at someone else's
group. An empty field is the honest answer.

### ADR-059 — The index carries a private group's bio, because a title is not an identity
**Context.** The operator's follow-up named the real failure mode: "a private
group is very hard to find again — you don't remember the name". A
members-only link plus a title is not enough to recognise a group months
later.
**Decision.** For groups whose link is not durable — private supergroups and
basic groups — the archive index adds what the chat says about itself: its
description (whole — Telegram caps one at 255 characters) and its member count. Learned via
``chat_details`` on the adapter (``GetFullChannel``/``GetFullChat`` under
MTProto, ``getChat`` under the Bot API), cached on the chat row with a
30-day staleness, and fetched at most 25 per archived round with a 500 ms
gap, because ``GetFullChannel`` is among Telegram's most eagerly rate-limited
calls — coverage converges over rounds instead of arriving as one burst.
**Consequence.** ``ChatDetails`` is deliberately metadata-only: a description
and a *count* Telegram publishes on the chat itself, never a participant list
— ``getparticipants`` remains guard-banned, and nothing here enumerates a
person. Public groups spend none of the lookup budget: a username is already
a durable way back. A failed lookup costs the bio line only; the copy and the
links are the record, and they go out regardless.

### ADR-060 — Every private group is identified, at the operator's word
**Context.** ADR-059 capped detail lookups at 25 per round. The operator
rejected the cap, correctly: "if it stays limited, what was the point — take a
little time but do it, and try again in the next round as many times as it
takes." An archive that identifies only some of the groups is not the record
they asked for.
**Decision.** The cap is gone. Every private group in the round is walked,
still paced at 500 ms. A Telegram wait up to 60 s is **obeyed in full, in
place**, then that chat is tried once more; a longer wait stops the walk —
stopped, never shortened — and the remainder carries to the next round. A
failed lookup leaves the chat unmarked, so every later round asks again until
it is learned. A ten-minute budget backstops one round's walk; at the normal
pace that allows ~1000 lookups, double the broadcast ceiling, so it only fires
under repeated waits — exactly when pressing on would lengthen them.
**Consequence.** The first round over 150 private groups spends about two
minutes in the walk before the copy and index go out; the operator chose that
price knowingly, and later rounds cost nothing because details are cached. A
test regression here was instructive: an error string spelled ``FLOOD_WAIT_42``
began *actually waiting 42 seconds* once the walk learned to obey waits — the
suite caught the new behaviour working.

### ADR-061 — The archive may be delivered by the bot, and that route wins
**Context.** The operator expected to add the *bot* to a group as admin and
have the copies land there. ADR-057 had the *account* deliver them, so the bot
being admin changed nothing. Their expectation turns out to be the better
design for the stated purpose: an archive the account delivers stops the day
that account is lost — the exact event the archive insures against.
**Decision.** ``AppSetting.archive_bot_chat_id`` holds a raw Telegram id,
deliberately not a foreign key to ``telegram_chats``: that table is what a
connected *account* can see, and this chat is one the **bot** was added to.
When set it wins over the account route, because two destinations would double
every copy. Delivery goes through ``BotAdapter`` — the existing boundary — so
no new Telegram code enters the codebase and a mock deployment stays mocked.
Group details are still learned through the account, which alone is a member
of the groups being described.
**Consequence.** The id is **proved before it is saved**: the bot posts one
line into the chat, so a missing invite or a missing post permission surfaces
during setup rather than silently swallowing every archive afterwards. The id
can be typed or supplied by forwarding any message from the group, and only a
forward from a *chat* counts — a forward from a person carries their id, and
an archive pointed at a private conversation is not what "the group I added
the bot to" means.

### ADR-062 — Mode before credential when building an adapter
**Context.** ``archive.bot_adapter()`` checked for a bot token before checking
``live_telegram``, so on any deployment without a token — every test — it
returned ``None`` and the archive silently took the "not configured" path.
**Decision.** Check the mode first and return the mock adapter, exactly as
``build_adapter`` already orders it; require the token only on the live path.
**Consequence.** A mock deployment needs no credential to pretend, and five
tests that looked like feature failures were this ordering. Worth repeating as
a rule: when a factory has both a mode switch and a credential check, the mode
comes first, or the credential silently decides behaviour it was never meant
to decide.

### ADR-063 — Callback filters must not overlap, and a test proves it
**Context.** The 🤖 *Use a group the bot is in* button answered "that group is
not in your list". Its handler was fine; it never ran. ``set_archive`` was
filtered on ``startswith("arch:")``, which also matches ``arch:bot``, and being
registered earlier it won — then read the word "bot" as a chat id. Nothing in
the code was wrong except that **registration order silently decided
behaviour**.
**Decision.** Filters are narrowed so no two handlers claim the same callback:
``arch:s:``/``arch:off`` for one handler, ``arch:bot`` for the other, and the
``ad:``/``rule:`` catch-alls now exclude ``ad:new``/``rule:new`` explicitly. A
test renders every panel screen, collects each ``callback_data`` a button can
emit, and asserts exactly one non-fallback handler matches it.
**Consequence.** The test found two more instances immediately —
``ad:new`` and ``rule:new`` were each claimed by two handlers, working only
because the specific ones happened to be registered first. A latent version of
the same outage, in the two most-used buttons in the panel. The sweep is built
by rendering screens rather than from a list of strings, because a list is the
thing that stops matching the code it describes.

### ADR-064 — The archive is operator-only, and the service is the gate
**Context.** The archive shipped on the home screen for every user. The
operator asked for it on the admin id and nowhere else — reasonable, since it
copies ad content into a chat the deployment's owner controls.
**Decision.** Three layers, and the third is the one that counts. The button
moves into the operator row. The handlers call ``_require_operator``, and the
message step checks too — being in a conversation state is not authorization,
since an operator list can change between the question and the answer. And
``destination_for`` refuses for a non-operator regardless of what the row says.
**Consequence.** "Only the admin id, nowhere else" becomes a property of the
system rather than of one screen: a row left from before the restriction, a
direct database edit, or a future API all archive nothing.
``test_a_row_belonging_to_a_non_operator_archives_nothing`` holds that by
revoking operator status with the setting already saved.

### ADR-065 — Home introduces the product; status lives where it belongs
**Context.** Home was a status board — connection health, ad counts, rule
counts, a 24-hour tally — which told a returning operator things they could
also get from the screen that owns each, and told a newcomer nothing about
what the bot is for. ``/panel`` was the advertised way in, competing with the
``/start`` every Telegram user already types.
**Decision.** Home is now an introduction: one line on what the product does,
then a sentence each on Ads, Auto-reply and Forwarding, then the buttons.
Connection health moved into *Accounts*, which already listed every connection
— it gained the "N of M working" line so nothing was dropped. ``/start`` is the
advertised command; ``/panel`` is off the menu but still answered, because a
command someone has typed for weeks should not begin doing nothing.
**Consequence.** The paused-rule warning is gone from home, and that loses
nothing: pausing a rule already pushes an alert, which arrives whether or not
anyone opens this screen. Home now reads the same on the first visit and the
thousandth, which is what an introduction should do.

### ADR-066 — Stopping an ad archives what it already delivered
**Context.** An ad stopped after 12 of 156 groups left no archive at all.
``settle()`` returns early for anything that is not still ``sending``, and
``cancel()`` set the status without archiving — so the record vanished at
exactly the moment it was most wanted, since the 12 deliveries are real and
cannot be unsent.
**Decision.** ``cancel()`` archives first, then cancels. Its ``adapter`` is
optional, because the bot route needs no account client and building an MTProto
connection on a Stop tap would be waste; the panel builds one only when the
destination is a synced group. Without an adapter, bios are not learned that
time — cached ones still appear — and the account route refuses **loudly**,
because silence there is indistinguishable from "no archive configured".
**Consequence.** Only groups that actually received the ad appear, so a stopped
ad's index never claims a group it never reached. An ad that delivered nothing
archives nothing, since an empty index is noise in the one place kept as
evidence. Verified by removing the call: the stopped-ad test fails on an empty
archive.

### ADR-067 — The archive runs on every settled round, not only the sent ones
**Context.** A round that delivered to 7 of 8 groups archived nothing and
logged nothing. The cause was a guard: ``settle()`` called ``store_round`` only
``if adapter is not None``. Every path that settles a round *after a send*
carries the adapter, but the path that settles after a **skip** —
``_terminal`` — did not pass one. So whenever the group that happened to finish
the round was refused rather than sent, the archive was silently skipped. The
round-finished alert still went out, which made it look like everything had
worked.
**Decision.** ``settle()`` calls ``store_round`` unconditionally and lets it
decide: the bot route needs no account adapter, and the account route logs why
it cannot proceed. ``_terminal`` also takes and forwards the adapter, so the
skip path can still learn group bios.
**Consequence.** Two failures in one: a feature that did nothing, and a guard
that made "did nothing" indistinguishable from "was never asked". Reproduced by
a test whose *last* group refuses, which fails on an empty archive with the
guard restored. The general lesson is the older one from ADR-062: a condition
that decides whether work happens must not also be the reason no one hears
about it.

### ADR-068 — Archiving is a property of an account, not of operator status
**Context.** ADR-068 originally granted operator access from the panel, so the
owner's second Telegram account could be promoted without editing a file. The
owner rejected it, and they were right: what they actually wanted was for other
accounts' **ads** to reach their archive, not for those accounts to gain the
power to suspend people and change the panel. Granting admin rights to solve an
archiving problem is a much larger key than the lock needed.
**Decision.** Operator access is once again the settings file alone —
``ADMIN_TELEGRAM_IDS``, decided outside the product, never handed out by it.
What replaces the grant is ``User.archive_ads``: three-valued, where ``None``
follows the deployment default (``AppSetting.archive_all_users``, on) and
``True``/``False`` is a decision about that one account which outranks the
default in **both** directions. That is what makes "everyone except this one"
and "nobody except this one" expressible with one switch and one flag.
**Consequence.** The Users screen offers *Copy their ads* / *Stop copying their
ads*, and *Follow the default* once a per-account choice exists — "never chosen"
and "chosen to be off" behave alike today but only one should move when the
default is flipped. The archive gate no longer asks whether the ad's owner is
an operator; it asks whether that account is archived, which is a different and
much narrower question.

### ADR-070 — Users are told the operator keeps a copy of their ads
**Context.** With the default on, every account's ads are copied into the
operator's group. ADR-032 says an operator sees counts and never content, and
the terms screen told users only that their access could be suspended. Shipping
the copy while the terms implied otherwise would make the product lie to the
people it is asking to accept them.
**Decision.** The terms gained a line: the operator keeps a copy of the ads
sent through this bot and the list of groups each went to, and private messages
are never read. It sits above the accept button, where it is read before
anything can be sent.
**Consequence.** ADR-032 still holds for what it covered — the Users screen
shows counts, no operator can browse anyone's messages, and auto-reply content
stays private. What changed is ads posted *through* this deployment, and now
the disclosure matches the code.

### ADR-069 — One archive per deployment, not per operator
**Context.** With the archive keyed to a user, promoting a second account left
it archiving nowhere — the setting belonged to the first account. The owner
means "my Logs group", not "this account's Logs group".
**Decision.** The archive destination resolves to whichever operator saved one,
most recent first, so any operator account's ads land in the same place.
**Consequence.** Setting it from a second account moves it for everyone, which
is the intent; the ordering makes the last decision the live one. This is right
because operators are trusted by definition here — the same trust that already
lets them suspend accounts.

### ADR-071 — The terms gate is removed; its one unique warning moved
**Context.** ADR-030 put a terms screen in front of every new account. The
operator asked for it gone — they wanted the bot to feel like a normal bot, and
they are its only user today. This also withdraws ADR-070's disclosure, which
lived on that screen.
**Decision.** The screen, the accept callback, the middleware gate and
``users.terms_accepted_at`` are all removed rather than left disabled. Of the
warnings the screen carried, only one lived nowhere else — that Telegram can
restrict an account people report as spam — and it moved to the confirmation
before an ad is sent, which is the moment it applies and where anyone would
look for it. The credentials warning was already on the sign-in screens; the
never-joins-groups and auto-reply-cannot-initiate properties are enforced in
code and guard-tested, not merely promised in prose.
**Consequence.** Nothing now tells a *future* third-party user that their ads
are copied to the operator's archive. That is a live gap the moment this
deployment has users other than its owner, and the honest place to close it
would be a line on the About screen or a note when an account is switched on
for archiving — not a gate. Flagged rather than silently accepted, because the
setting defaults to copying everyone.

### ADR-072 — The role picker and the publisher waitlist are removed
**Context.** ADR-052 asked new arrivals which side of a marketplace they were
on, and ADR-053 kept an honest "not built" screen for the publisher side. With
the terms gate gone the operator wanted ``/start`` to be the panel, full stop.
**Decision.** Both screens, the ``role:`` callbacks and
``users.publisher_interest_at`` are removed. Adding a bot connection goes off
the Accounts screen too — this deployment posts from a real account, and the
bot-connection path was a second way to do a thing nobody does here. The HTTP
API keeps it, so nothing that existed stops working.
**Consequence.** ``/start`` shows the panel to everyone, first time or
thousandth. The waitlist recorded nothing worth keeping — demand for a side
that was never built, from a deployment with one user.

### ADR-073 — A connection screen reports what that account has done
**Context.** The connection screen said what an account *is* — type, status,
groups known. What anyone opens it for is what it has been doing.
**Decision.** All-time counters per connection: ads created, delivered, did not
arrive, retrying. Not windowed like the home summary was, because a 24-hour
view of an account that last ran an ad on Tuesday reads as though it has never
done anything. Hidden entirely when all four are zero, since four zeroes on a
new account is noise.
**Consequence.** "Did not arrive" folds skipped and failed together on purpose:
from the outside they are the same event, and the per-group report already
separates them with a reason each.

### ADR-074 — An ad can be deleted, once it has stopped
**Context.** Finished and cancelled ads accumulated in the list with no way to
clear them.
**Decision.** 🗑 Delete appears only when an ad is *not* running, behind a
confirmation that states plainly what deleting does not do. The status is
re-checked in the handler, not only by hiding the button: a callback can be
replayed, and deleting mid-flight would drop target rows the worker is holding
leases on.
**Consequence.** While an ad runs the same slot holds Stop — one destructive
button at a time, and they mean different things. Deleting removes the ad and
its per-group record; it cannot unsend a single message, which the confirmation
says.

### ADR-075 — Auto-reply answers only while the account is advertising
**Context.** Auto-reply answered anyone who wrote in, at any hour, for as long
as it was switched on. The operator asked for it to work the way a competing
bot describes: replies tied to the broadcast.
**Decision.** A reply goes out only when that account has an ad ``sending`` —
which a repeating ad remains between rounds — or one that finished within
``auto_reply_after_ad_hours`` (24). The window matters more than the round: a
one-shot ad is over in a minute and the people who saw it are not.
**Consequence.** This is narrower than what it replaces, and narrower is
better here. An account answering strangers around the clock is behaving like
a bot; one answering while it advertises is answering the people who saw the
ad. It also bounds the damage of a wrong reply text — it can only reach people
who wrote during a campaign window. The scope is per account: someone else
advertising says nothing about whether people are writing to *you*.

### ADR-076 — Forwarding leaves the panel
**Context.** The operator said the forwarding button was no use to them.
**Decision.** The whole panel surface goes — the button, the ``/rules``
command, the rule screens, the compose flow and its states. The group picker
becomes ads-only, which is what it now serves. The models, the HTTP API and the
worker's delivery path stay: they are a separate surface with their own tests,
and removing them was neither asked for nor free.
**Consequence.** Forwarding is no longer reachable from Telegram. Anything
already created keeps running through the worker, and the API can still drive
it. If the panel surface is wanted back it is a re-add, not a rebuild.

### ADR-077 — Auto-join is refused, again
**Context.** The operator asked whether the bot could join groups
automatically.
**Decision.** No, and it is not a judgement call: the founding brief lists
auto-join among the capabilities this product does not implement, and
``test_nothing_in_the_codebase_joins_chats_or_collects_members`` fails the
build if ``join_chat``, ``joinchannel`` or ``importchatinvite`` appears
anywhere in ``app/``.
**Consequence.** Groups are joined by the person, and ``Sync groups`` picks
them up. The practical argument matches the principled one: entering groups
uninvited is the behaviour Telegram restricts accounts for, and the account at
risk is the customer's own.

### ADR-078 — An ad can be given a start time, in the customer's own clock
**Context.** ``scheduled_for`` and ``BroadcastStatus.scheduled`` had been in the
model since the beginning and nothing used them. Starting an ad meant being
awake to press Send.
**Decision.** 🕒 Start at accepts a clock time (``21:30``) or a relative one
(``2h``, ``1d``, ``3h 30m``), and ``now`` to clear it. The scheduler promotes
due ads on the reclaim cadence, so 09:00 means within fifteen seconds of it,
each in its own transaction — one ad that fails validation must not hold up the
rest of the morning's.
**Consequence.** Validation happens when *Send* is pressed, not when the timer
fires: discovering at six in the morning that an ad had no groups is the worst
possible moment to find out. If it fails at start time anyway — groups deleted
since — the ad goes back to a draft and an alert says why, rather than
disappearing.

### ADR-079 — A start time is always confirmed twice
**Context.** A clock time needs a timezone, and the wrong one is a five-and-a-
half-hour error that renders as a perfectly plausible number. This is the
failure mode a start time can least afford, because nobody is watching when it
misfires.
**Decision.** Every start time is shown as both an absolute local time *and* a
relative one — "25 Aug, 11:30 — in about 13.5 hours". A wrong zone is invisible
in the first form and unmissable in the second. The zone comes from
``AppSetting.timezone``, and answering the time prompt with a zone name
(``Asia/Kolkata``) sets it — which is exactly what someone does when the
confirmation comes back wrong.
**Consequence.** Relative times bypass the question entirely, so ``2h`` is
always correct regardless of what the stored zone says. An unparseable zone
falls back to UTC rather than raising: a bad row should cost a wrong-looking
time on a screen, not a dead scheduler.

### ADR-080 — Groups that refuse posts are surfaced, never pruned automatically
**Context.** A round of 156 groups delivered to 12. The other 144 were tried,
refused and tried again on the next round — wasted time, and an account
repeatedly knocking on doors that are shut. The eligibility snapshot already
recorded which groups those were and why; nothing showed it in a usable form.
**Decision.** 🧹 Refusing posts lists them with the reason for each. Removal is
a decision the customer makes — one group at a time, or all the lasting ones at
once. Nothing prunes itself: a refusal can be a fact about the group or a fact
about this minute, and automation would eventually discard a good group over a
wait that had already cleared, invisibly.
**Consequence.** Temporary refusals — slow mode, a flood wait, an unknown code
— are marked ⏳ and excluded from *Remove all*, though they can still be removed
individually. "Remove" means removed from the ads, and the screen says so
twice: the account stays a member and the group can be chosen again.

### ADR-081 — Dropping a group keeps what it already received
**Context.** Removing a group from ads could take its target rows with it,
including rows recording a delivery that actually happened.
**Decision.** ``drop_chats`` deletes only rows that never succeeded.
**Consequence.** A group that worked for months and then shut its doors keeps
the record of every ad it did receive, which is what the archive index is built
from. Erasing that to tidy a list would be destroying evidence to save a row.

### ADR-082 — An operator can see which chats an account belongs to
**Context.** The operator drives this bot from two of their own Telegram
accounts and wanted to see either one's groups without switching accounts.
ADR-032 had held the Users screen to counts only.
**Decision.** 👥 Users → an account → 💭 Their groups lists **groups and
channels only**, each with a link where Telegram publishes one, and whether the
account can post there. Views for all / groups / channels. Metadata only: no
operator can read a message here and that has not changed.
**Consequence.** Private conversations are synchronized but excluded — the
first version listed everything, and 500 chats titled with somebody's name or
"." buried the 237 that mattered across 123 pages. Links follow the same three
cases as ADR-058: a username opens for anyone, ``t.me/c/`` opens for members,
and a basic group has no form at all, which the screen says rather than leaving
blank.

This does widen what an operator sees about *other* people — group memberships
say a good deal about someone — and with the terms gate gone (ADR-071) nobody
is told. It is a smaller step than the archive already takes, which copies
every account's ad content to the operator's own group, and the same judgement
applies: on a single-owner deployment it costs nothing, and on a shared one it
is one of the things that would need saying.

### ADR-083 — A members-only chat carries its bio; a public one does not
**Context.** On the operator's view of an account's chats, a public link
unfurls by itself — Telegram shows the title, the description and a Join button
under it. A ``t.me/c/`` link shows nothing, so a private group was a title and
a number with nothing to recognise it by.
**Decision.** Private chats show their description (clipped to 120) and member
count under the link. Public ones show neither, because Telegram is already
saying it and repeating it would be noise.
**Consequence.** Details are learned for **the page being viewed and no other**
— six chats, about three seconds, cached from then on. Fetching all 237 would
take two minutes of Telegram's most rate-limited lookup for chats nobody is
looking at. ``page_of_chats`` is shared between the fetch and the render so the
two cannot disagree about which six; a second implementation would drift and
quietly fetch the wrong ones.

### ADR-084 — The set of panel emoji is read from the source, not written down
**Context.** ``PANEL_EMOJI`` was a hand-kept tuple. It had already lost twelve
of them — including 🔗 on the Accounts button and ☑️/⬜️, the tick boxes in the
group picker — and the loss was silent: a new button simply never got a premium
icon and nobody noticed.
**Decision.** ``emoji_scan.panel_emoji()`` reads ``views`` and ``handlers``
with ``inspect.getsource`` and returns every emoji it finds. Matching is
generous on purpose — a spare lookup for an emoji in a comment costs half a
second once, a missed one costs a plain icon among premium ones forever.
Variation selectors are preserved, because ``⚠`` and ``⚠️`` are different
strings that Telegram indexes differently.
**Consequence.** Drift is now impossible by construction, which also makes the
obvious test vacuous: asking "is every emoji in the source covered?" proves
nothing when the covered set *is* the source. The real risk is the regex
missing a Unicode block, so the guard checks the scanner against
``unicodedata`` instead — every character it calls a symbol must be one the
scanner found. Verified by deleting the Geometric Shapes block: the guard names
``▶ U+25B6``, which is exactly the miss that left the Resume button plain.

### ADR-085 — The icons screen names what has no premium version
**Context.** The operator has joined the packs and wanted to supply the rest by
hand — but only a count was shown, so finding *which* ones meant comparing the
panel against itself.
**Decision.** After extraction the screen lists every icon with no match, on
one copyable line, above the instructions for sending a replacement pair.
**Consequence.** The list is the work queue: each one is answered with
``<emoji> <id>`` and disappears from it. When it empties the screen says so
rather than showing an empty heading.

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
9. **One IP for every account.** Every MTProto connection reaches Telegram from the deployment's
   single address, and Telegram correlates that. A handful of people is unremarkable; dozens of
   strangers broadcasting from one datacentre IP is a pattern Telegram acts on. There is no
   engineering fix that is not evasion — proxy rotation is explicitly out of scope — so the mitigation
   is operational: keep the population small enough to know, and suspend accounts that misuse it.
   ``ACCESS_MODE=open`` prints this warning at startup and in `deploy.sh`.
10. **The driving account cannot connect itself.** ADR-036. Telegram burns any login code it sees
   an account send in a chat, and there is no sign-in surface outside Telegram any more. Connecting
   a second account works; connecting the one you message the bot from does not, and the panel says
   so before you start rather than after a code is spent.
11. **Credentials typed into the chat.** The login code and any 2FA password are typed, deleted on
   read, with the warning shown first. Still the weakest moment in the flow (ADR-024).
12. **Moderation is reactive.** An operator learns about abuse from a report or from a broadcast count
   that looks wrong, not from the system. Content-based detection would mean reading everyone's
   messages, which ADR-032 rules out.
