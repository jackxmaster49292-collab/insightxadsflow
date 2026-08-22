# InsightAdFlow — Telegram ads, auto-reply and forwarding

Post your own message to Telegram groups you have **already joined**, answer people who message you
first, and automatically forward messages between chats you are **authorized to read and post in** —
all from a Telegram bot. There is no website to host.

> **Status: implemented.** FastAPI + Telethon/aiogram backend, PostgreSQL and Redis under Docker
> Compose, controlled entirely through a Telegram bot. Telegram is **fully mocked by default** —
> nothing contacts real Telegram servers unless you set `TELEGRAM_PROVIDER=live`.

---

## What it does

Three things, all driven from a chat with your own admin bot:

**Ads.** Write a message in the bot, pick the groups, send. It posts to each group in turn, paced to
stay inside Telegram's limits, and tells you exactly which groups received it and which did not.

**Auto-reply.** Someone sees the ad and messages your account; the bot answers them. It can only ever
answer — there is no recipient list and no way to make one, and one person is answered once per
waiting period.

**Forwarding.** Connect a bot or an account → synchronize your chats → pick sources and destinations →
create a rule → turn it on. New eligible messages are forwarded in the background while you are
offline.

No manual copy, paste, download, upload, or resend. No web panel, no domain, no TLS certificate: the
stack only makes outbound connections.

## What it is not

Not a campaign manager, marketing CRM, subscription platform, sales funnel, team collaboration suite,
or analytics product. There are **no Free/Pro/VIP tiers, no quotas, and no billing** anywhere in the
codebase.

## Safe use — please read

This tool respects Telegram. It **will not** help you evade it.

- It operates only on chats you have legitimately connected and are authorized to read from or post to.
- It **always** obeys Telegram rate limits and flood waits, and slows, pauses, or stops when Telegram
  says so.
- It **refuses** to relay content from chats with content protection enabled — in both forward and copy
  mode. Using `copyMessage` to launder protected content is circumvention, and it is deliberately not
  implemented.
- It contains no anti-detection behaviour, no restriction evasion, no account or proxy rotation, no
  CAPTCHA bypass, no scraping, and no unsolicited messaging.
- An ad posts **only** to groups the connected account has already joined. Nothing here joins a group,
  reads a member list, or imports an invite link — and a guard test fails the build if such code
  appears.
- Auto-reply **only ever answers someone who messaged you first**. It has one entry point, it refuses
  group chats outright, and a guard test pins that surface so a "message everyone" function cannot be
  added quietly.
- **Telegram can still restrict your bot or your personal account.** Misuse is your responsibility, and
  this software will not help you evade the consequences. Using an MTProto user-account connection
  carries meaningfully more risk than a bot connection — prefer a bot wherever it suffices.

You are expected to comply with the [Telegram API Terms](https://core.telegram.org/api/terms).

## Connection types

|  | Bot connection | User account (MTProto) |
|---|---|---|
| Setup | Token from [@BotFather](https://t.me/botfather) | Phone + login code + 2FA |
| Read a channel it was added to | ✅ | ✅ |
| Read a channel you only subscribe to | ❌ | ✅ |
| Read group messages | Admin / privacy-mode-off only | ✅ |
| Media download cap | 20 MB | Larger |
| Risk if misused | Bot ban | **Personal account restriction** |

The panel always shows which connection is active and what it can access. The system **never** silently
falls back from a bot to an account, or between accounts.

## Supported message types (MVP)

Text · photos + captions · videos + captions · documents · audio and voice · links · inline buttons
(forward mode) · albums / grouped media (forward mode) · polls (forward mode).

**Skipped, with a visible reason:** protected (`noforwards`) content, service messages, paid-media,
giveaway and invoice messages, stories, and premium-gated content. Nothing is ever silently converted,
stripped, or altered — every deviation produces a forwarding event you can read.

## Known limitations

- Not every message can be forwarded. Permissions, message-type restrictions, protected content,
  deleted messages, rate limits, and network failures all prevent it, by design.
- MVP forwards only messages that arrive **after** a rule is activated — no history backfill.
- MVP does not propagate later edits or deletions of an already-forwarded message.
- On an ambiguous Bot API timeout the job is marked *needs attention* rather than retried, so you may
  see a delivery gap instead of a possible duplicate. This is deliberate.

## Quick start

Generate an encryption key first — it lives only in the environment, never in the database, so a
database backup alone cannot decrypt Telegram session material:

```bash
cp .env.example .env
python3 -c "import base64,os;print('ENCRYPTION_KEK='+base64.b64encode(os.urandom(32)).decode())"
```

Paste that into `.env`, then bring up the whole stack:

Set `ADMIN_BOT_TOKEN` (a bot from @BotFather) and `ADMIN_TELEGRAM_IDS` (your numeric id, from
@userinfobot) in the same file, then bring up the whole stack:

```bash
./deploy.sh
```

Send `/start` to your bot — that is the panel. Nothing is published to the internet.

To exercise forwarding without Telegram, inject a message through the real dispatch path — the same
code the listener calls:

```bash
docker compose exec api python -m app.devtools emit <connection_id> --message-id 9001 --text "Launch is live"
```

Run it twice with the same `--message-id` and the second run reports the duplicates suppressed rather
than delivering again.

For backend development outside containers, `make up` starts just Postgres and Redis, then
`make migrate && make api` (and `make worker` in another shell). Run the tests with:

```bash
make check
```

Telegram is **fully mocked by default** (`TELEGRAM_PROVIDER=mock`); set `TELEGRAM_PROVIDER=live` only
when you intend to reach real Telegram servers, and never in a test or CI configuration.

Required environment variables, including `ENCRYPTION_KEK` and the `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
you obtain from [my.telegram.org](https://my.telegram.org), are documented in
[docs/OPERATIONS.md](docs/OPERATIONS.md) §1.1.

## Telegram setup

**Bot:** create it with @BotFather, copy the token, add the bot to each source channel (bots receive all
messages from channels where they are a member) and grant post rights in each destination. For groups,
either make it an admin or disable privacy mode, or it will only see commands and replies.

**User account:** get an `api_id` and `api_hash` from my.telegram.org, then connect in the bot with
the account's phone number, the login code, and its 2FA password if it has one. Each is deleted from
the chat the moment it is read.

One real limit: **Telegram cancels any login code it sees an account send inside a chat.** Connecting
the account you are messaging the bot *from* therefore fails every time, whatever you type. Connect a
different account, or connect a bot — a bot token is not cancelled that way. The panel states this
before asking for anything. Your 2FA password is used once in memory
to complete sign-in and is **never stored, hashed, or logged**. Session material is encrypted at rest
with envelope encryption and can be revoked from the panel, which also calls `auth.logOut` so Telegram
invalidates it server-side.

## Managing it from Telegram

The bot is the whole admin surface: connecting an account by phone number,
composing an ad, picking groups, writing the auto-reply, and managing forwarding
rules are all conversations with it. It also **pushes an alert** when a rule or
connection auto-pauses, rather than waiting for you to look.

```bash
ADMIN_BOT_TOKEN=123456789:AA...   # a SEPARATE bot from any forwarding bot
ADMIN_TELEGRAM_IDS=123456789      # the operators; empty means nobody
ACCESS_MODE=closed                # or "open" — see below
```

**Who can use it.** `closed` (the default) means only the operator ids. `open`
means anyone who messages the bot, after accepting a terms screen that states
plainly what the tool does and that Telegram can restrict *their* account if
their messages are reported. Operators get a **Users** screen — counts and a
suspend button, never anyone's content.

Before opening it up, know this: every connected Telegram account reaches
Telegram **from your server's single IP**, and Telegram correlates that. A
handful of people is unremarkable; dozens of strangers broadcasting from one
datacentre address is a pattern Telegram acts on. There is no fix for that which
is not evasion, so the mitigation is operational — keep the group small enough
to know, and suspend accounts that misuse it.

Credentials — bot tokens, phone numbers, login codes, 2FA passwords — are typed
into the chat, because that is where the panel is. Every prompt says so first,
the message is deleted the instant it is read, and the value never reaches
conversation state, a log, or the database. Telegram still held it briefly, which
is why **rotating your bot token after setup is worth doing**. The full tradeoff
is written up in [docs/TELEGRAM_PANEL.md](docs/TELEGRAM_PANEL.md) §6 and ADR-024.

## How it fits together

| Process | Role |
|---|---|
| `api` | FastAPI, internal only. The service layer the tests drive, plus the health endpoint. **Never blocks on Telegram** — every command enqueues durable work and returns `202`. |
| `listener` | Reads new messages, and answers a private one when auto-reply is on. One Redis lock per connection, so only one process ever opens a client for a given connection. |
| `worker` | Claims due forwarding jobs and broadcast targets with `SELECT … FOR UPDATE SKIP LOCKED`, delivers them, and heartbeats a lease so a crash returns the work rather than losing it. |
| `scheduler` | Reclaims expired leases, runs health checks, enforces retention. Exactly one instance. |
| `adminbot` | **The control panel.** Every screen and every flow. Also drains the alert outbox. Exactly one instance — Telegram allows one `getUpdates` consumer per token. |
| `postgres` | Source of truth for everything, including the job queue. |
| `redis` | Coordination only — locks, pacing buckets, rate limits. Losing it loses nothing durable. |

## Documentation

| Doc | Contents |
|---|---|
| [DEPLOY_VPS.md](docs/DEPLOY_VPS.md) | **Step-by-step VPS deployment** — hardening, Docker, database, backups |
| [TELEGRAM_PANEL.md](docs/TELEGRAM_PANEL.md) | **The control panel** — ads, auto-reply, forwarding, setup, security |
| [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) | Scope, safety boundary, terminology, features, MVP definition |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Integration choice, library verification, components, intake, queue |
| [SECURITY.md](docs/SECURITY.md) | Threat model, envelope encryption, isolation, redaction, abuse posture |
| [DATABASE.md](docs/DATABASE.md) | Entities, keys, indexes, invariants, retention, sensitive fields |
| [API.md](docs/API.md) | Endpoint contract (`openapi.yaml` generated at implementation step 2) |
| [OPERATIONS.md](docs/OPERATIONS.md) | Setup, env vars, retry classification, recovery, acceptance checklist |
| [TESTING.md](docs/TESTING.md) | Mock strategy, required tests, end-to-end scenario, CI gates |
| [DECISIONS.md](docs/DECISIONS.md) | ADRs and open tradeoffs |
| [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Slice-by-slice build order, assumptions, open questions |

## Operational safety controls

Bounded worker concurrency · queue backpressure · per-connection in-flight limit · message size and
media constraints · maximum retry attempts · application request rate limiting · Telegram flood-wait
handling · automatic pause on serious platform or authorization errors.

These exist for reliability and platform compliance. They are **not** monetization limits, and this
project makes no "unlimited" claim — Telegram itself may restrict any account or operation at any time.
