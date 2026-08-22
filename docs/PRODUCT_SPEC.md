# InsightAdFlow — Product Specification

**Status:** Implemented.
**Last updated:** 2026-08-22

---

## 1. What this product is

InsightAdFlow does three things, all controlled from a **Telegram bot** — there is no web panel.

**Ads.** The customer writes their own message in the bot, chooses groups the connected account has
already joined, and sends. Each group receives it once, paced to stay inside Telegram's limits.

**Auto-reply.** When someone messages the connected account — typically after seeing an ad — the bot
answers them once. It can only ever answer; see §3.1.

**Forwarding.** The customer connects a Telegram bot or an authorized Telegram account, picks source
chats they are allowed to read, picks destination chats they are allowed to post to, creates a
forwarding rule, and turns it on. New eligible messages are then forwarded automatically while the
customer is offline.

The three are deliberately connected: an ad brings people to the account, and the auto-reply meets them
there, on that same connection.

## 2. What this product is explicitly not

Not built, not in the database, not in the API, not in the UI:

- Campaign management, campaign history, marketing CRM, sales funnels, conversion attribution
- Subscription plans, billing, payments, quotas, Free/Pro/VIP tiers
- Team roles, approvals, collaboration
- Advertising copy generation
- Advanced analytics or engagement metrics
- A general admin dashboard
- Unrelated third-party integrations

## 3. Safety boundary (non-negotiable)

"No artificial limits" means **no invented product/monetization limits**. It does **not** mean evading
Telegram.

The system **will not** implement, and I will not add later on request without re-raising this:

| Prohibited | Why |
|---|---|
| Anti-detection behaviour | Evasion of platform enforcement |
| Restriction / ban evasion | Evasion of platform enforcement |
| Account or proxy rotation to bypass limits | Evasion of platform enforcement |
| CAPTCHA bypass | Evasion of platform enforcement |
| Spam automation, unsolicited DMs | Abuse |
| Unauthorized scraping/collection of users | Abuse + privacy |
| Posting to chats without permission | Unauthorized access |
| Circumventing private-channel access controls | Unauthorized access |
| **Copying content out of protected/`noforwards` chats** | Content-protection circumvention |

### 3.1 Where the ad and auto-reply features stop

Broadcasting and automatic replies are the two features closest to the prohibited list, so the
boundaries are stated rather than implied — and each is enforced by a guard test, not by review:

| Not built | Why |
|---|---|
| Joining a group, importing an invite link | An ad posts only where the account already is |
| Reading or exporting a member list | Collecting users, and a privacy violation |
| Any function taking a list of people to message | Unsolicited messaging |
| Messaging someone who has not written first | Unsolicited messaging |
| Replying in a group | Posting where nobody asked |
| Message spinning, randomised text to look human | Anti-detection |

A broadcast target is a **foreign key into synchronized membership**, never a raw peer id or a
username — so a broadcast cannot address a chat the account was never confirmed to be in.

Auto-reply has exactly one entry point, which takes a single sender that has already messaged the
account. A per-person cooldown, held in Postgres rather than a cache, means one answer per person per
window even across a restart.

That last row of the previous table is a design decision worth stating plainly: `copyMessage` can technically reproduce
content that `forwardMessage` refuses to forward. **We treat that as circumvention.** If a source chat
reports `has_protected_content` (Bot API) or `noforwards` (MTProto), both forward mode *and* copy mode
are refused, and the customer sees a clear skip reason.

Every source and destination carries an explicit, visible authorization/eligibility status, revalidated
immediately before each delivery. Where authorization cannot be confirmed, the system **fails closed**:
skip the delivery and record the reason rather than guessing.

## 4. Terminology (binding across UI, DB, API, docs)

| Term | Meaning |
|---|---|
| **Connection** | A Telegram bot, or an authorized Telegram user-account session |
| **Source chat** | A chat the connection is authorized to *read* new messages from |
| **Destination chat** | A chat the connection is authorized to *send or forward* to |
| **Forwarding rule** | A persistent source→destination automation configuration |
| **Forwarding job** | One attempt to process one source message for one destination under one rule |
| **Forwarding event** | A durable record of an outcome: forwarded, skipped, failed, retried, paused |
| **Broadcast** (shown as **Ad**) | The customer's own message, to be posted to groups they chose |
| **Broadcast target** | One group of one broadcast: one delivery, one durable row |
| **Auto-reply** | A stored answer sent to people who message the connection first |

The word **campaign** does not appear in the UI, database, API, or docs.

## 5. Connection models

Both are supported and **cleanly separated**. The customer always knows which is active, and the system
never silently falls back from one to the other, or between accounts.

### 5.1 Bot connection (Bot API 10.2)

- Auth: bot token from @BotFather.
- Reading: a bot receives **all messages from channels where it is a member**. In groups it receives
  everything only if it is an admin or privacy mode is disabled; otherwise only commands and replies.
- Cannot read message history, cannot join chats on its own — an admin must add it.
- Best when the customer **controls** the source chats.
- File download cap: **20 MB** (`getFile`). Send cap: 50 MB.

### 5.2 User connection (MTProto, Telethon 1.44)

- Auth: `api_id`/`api_hash` + phone → login code → 2FA password if enabled.
- Reads any chat the account is a member of, including channels it merely subscribes to.
- Required for the common real-world case where the customer does not own the source channel.
- Higher risk: account-level restrictions apply. Surfaced honestly in the UI.

### 5.3 Capability matrix shown in the UI

| Capability | Bot | User account |
|---|---|---|
| Read a channel it was added to | ✅ | ✅ |
| Read a channel it only subscribes to | ❌ | ✅ |
| Read group messages | Admin / privacy-off only | ✅ |
| Read history | ❌ | ✅ |
| Forward preserving attribution | ✅ | ✅ |
| Download media | ≤ 20 MB | Larger |
| Risk if misused | Bot ban | **Account restriction** |

## 6. Customer workflow

Everything below happens in a chat with the admin bot.

**Setup (once).** Send `/start` → **Accounts → Add account** → name, phone number, login code, 2FA
password if the account has one → **Sync groups**, which reads the groups the account has already
joined and records where it may post.

**Posting an ad.** **Ads → New ad** → name → the message → optionally an image → the pause between
groups → tick the groups → **Send now**. A confirmation screen states the group count, whether an image
is attached and roughly how long it will take, and says plainly that posted messages cannot be unsent.
Progress and per-group outcomes are visible while it runs; **Retry** covers groups that did not receive
it and never re-posts to one that did.

**Auto-reply.** **Auto-reply → Edit reply** → the text → **Turn on**. It cannot be switched on with
nothing to say.

**Forwarding.** **Forwarding → New rule** → name → the chat to copy from → open the rule to choose the
groups to copy into → resume it. Then leave it running while offline and inspect only exceptions,
failures, pauses, and basic activity.

No manual copy, paste, download, upload, or resend at any point.

## 7. Feature scope

### 7.0 Ads and auto-reply

**Ads.** Text and an optional image. Validated before anything is queued — an empty message, no groups,
text past Telegram's 4096-character limit (1024 with an image), more than `MAX_BROADCAST_TARGETS`
groups, or a pause that would push the last delivery past six hours are each refused with a sentence
that says what to change and by how much. One row per group, so the same group cannot be queued twice.
Pause, resume, stop and retry are all available while sending; stopping cannot unsend.

The image is stored as **bytes**, not a Telegram `file_id`: a `file_id` is scoped to the bot that
received it, so the admin bot's id is meaningless to the connection doing the posting (ADR-027).

**Auto-reply.** One per connection: the reply text, an on/off switch, and the waiting period before the
same person may be answered again. Enabling it with no text is refused.

### 7.1 Connections
Secure connect flow per type; login code and 2FA support without ever storing the raw 2FA password;
session material encrypted at rest; tokens/codes/session strings never written to ordinary logs;
status, last successful check, authorization errors, reconnect; disconnect and revoke-session;
duplicate simultaneous connection attempts prevented.

### 7.2 Chat synchronization
A sync action discovers available chats. Each chat shows: name, Telegram identifier, type
(channel/group/supergroup/other), public/private where available, **source eligibility**,
**destination eligibility**, last sync time, last error with a safe explanation, active/inactive.

Appearing in a list never confers eligibility. Access and permissions are revalidated before a job is
created *and* again immediately before it executes.

### 7.3 Forwarding rules
One or more sources; one or more destinations; active/inactive toggle; keyword include list; keyword
exclude list; media-type filters (text, image, video, document, audio, poll, other); link preservation
where supported; caption/text preservation where supported; **bounded, configurable delay** between
destination deliveries; duplicate prevention; retry for transient failures; pause/resume; last activity
and current status.

MVP ships **one source → many destinations**. Many-to-many is allowed only if it does not complicate
UX or reliability; if implemented, rules resolve into unique jobs with duplicate delivery prevented.

Preview text, shown before activation:

> When a new eligible message appears in **Source A**, forward it to **Destination 1**,
> **Destination 2**, and **Destination 3**, subject to the configured filters and platform-safe
> processing rules.

### 7.4 Automatic forwarding
Detect new messages via the appropriate event mechanism (MTProto updates; Bot API long polling with a
persisted offset). Create durable jobs **outside the HTTP request lifecycle**. Deliver to each eligible
destination. Record a result for every source-message/destination pair. Keep running while the customer
is offline. Never send the same source message twice to the same destination under the same rule.
Continue with other destinations when one fails, unless the error indicates a connection-wide
restriction. Automatically pause on serious authorization/restriction/repeated-error conditions.

**We do not promise every message can be forwarded.** Permissions, message-type restrictions, protected
content, deletions, rate limits, and network failures can all prevent it.

### 7.5 Message handling

| Type | MVP (bot) | MVP (user) | Notes |
|---|---|---|---|
| Text | ✅ | ✅ | |
| Photo + caption | ✅ | ✅ | |
| Video + caption | ✅ | ✅ | |
| Document | ✅ | ✅ | |
| Audio / voice | ✅ | ✅ | |
| Links | ✅ | ✅ | Preserved via forward |
| Inline buttons | Forward mode only | Forward mode only | Not reconstructed in copy mode |
| Albums / grouped media | ✅ forward mode | ✅ forward mode | Buffered by `media_group_id` / `grouped_id`; deferred in copy mode |
| Polls | ✅ forward | ✅ forward | Quiz copy needs known answers — skipped in copy mode |
| Service / paid-media / giveaway / invoice | ❌ skipped | ❌ skipped | `copyMessage` cannot copy these |
| Protected (`noforwards`) content | ❌ skipped | ❌ skipped | **Deliberate** — see §3 |
| Stories, premium-gated content | ❌ skipped | ❌ skipped | Deferred |

Nothing is silently converted, stripped, or altered. Every deviation produces a forwarding event with a
reason code.

### 7.6 Monitoring (intentionally small)
Connection status; active rules; source/destination selection; rule status (active/paused/error/
disconnected); recent forwarding events; counts for forwarded/skipped/failed/retried/paused over a
selectable recent period; customer-safe error details; start/pause/resume/edit/delete; a simple
activity log. Nothing more.

## 8. Optional enhancements (only after core is stable)

| Feature | Purpose | Priority |
|---|---|---|
| Keyword include/exclude filters | Prevent irrelevant content forwarding | MVP |
| Media-type filters | Forward only selected content types | MVP |
| Per-rule delay | Control pacing between destination deliveries | MVP |
| Duplicate prevention | Avoid repeats after reconnect/restart | MVP |
| Failed-destination retry | Retry transient failures only | MVP |
| Chat labels/tags | Organize large chat lists | V1 |
| Message prefix/suffix | Customer-approved label or disclaimer | V1 |
| Destination groups by label | One rule targets a saved destination set | V1 |
| Quiet hours | Stop forwarding during configured local hours | V1 |
| Dry-run preview | Show destinations without sending | V1 |
| Health alerts | Notify when a connection or rule pauses | V1 |
| Rule templates | Recurring configurations | V1 |
| Basic import/export | Back up rule config as JSON | V1 |

Delays are transparent, bounded, and used for orderly pacing — never randomized to evade enforcement.

## 9. UI pages

**Login** — secure login, logout, session expiry handling, recovery path.
**Home** — connection health, active rule count, recent activity, current errors, primary CTA.
**Telegram Connections** — connect, inspect, synchronize, reconnect, disconnect; explains source and
destination capabilities of the active connection type.
**Chats** — searchable table filtered by source-eligible, destination-eligible, public/private, type,
active, error state; refresh sync; inspect why a chat is ineligible.
**Forwarding Rules** — list with sources, destination count, filter summary, status, last activity,
controls; create/edit flow ending in a preview before activation.
**Rule Detail** — configuration, sources/destinations, filters, recent events, failed destinations,
retry action, pause/resume, delete.
**Settings** — timezone, notification preferences, security settings, data export/deletion, session
management.

Accessible controls, readable statuses, explicit loading/empty/error/permission-denied states, keyboard
navigation, confirmation on destructive actions, safe error messages.

## 10. MVP definition

Complete when: secure login; explicitly connected bot(s) and/or authorized account(s); encrypted
token/session storage; chat synchronization; clear source-read and destination-post eligibility; active
forwarding rules; source→many-destination automatic forwarding; text plus the reliable media types
above; keyword include/exclude filtering; media-type filtering; per-destination durable jobs;
background operation while offline; duplicate prevention; bounded transparent delays; transient-failure
retry; pause/resume/edit/delete; recent activity and error visibility; safe handling of protected and
unsupported messages; tests with Telegram fully mocked by default; documented local setup; migrations
and seed/demo configuration; security and operational-recovery documentation.

## 11. Operational safety controls (not monetization limits)

Bounded worker concurrency; queue backpressure; per-connection in-flight job limit; maximum message
size and media constraints; maximum retry attempts; application request rate limiting; Telegram
flood-wait handling; automatic pause on serious platform or authorization errors.

These are documented in the UI and README as **safety controls**. The product never advertises
"unlimited" Telegram capacity, because Telegram itself may restrict any account or operation.

## 12. Acceptance criteria

See [OPERATIONS.md](OPERATIONS.md) §8 for the reviewable checklist. The product is acceptable only when
every item in the master acceptance list passes, including: the HTTP API never blocks on forwarding, a
repeated source event cannot duplicate a successful delivery, flood waits are respected rather than
bypassed, controls survive process restarts, secrets never appear in logs or API responses, cross-user
access is impossible and covered by tests, and there are no subscription quotas anywhere in the code.
