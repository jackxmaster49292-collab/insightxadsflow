# The Telegram control panel

**Status:** Implemented.
**Last updated:** 2026-08-22

The bot is the whole admin surface. There is no website, no domain and no login
form — you talk to a bot on Telegram and it does everything.

> Superseded ADR-019 and ADR-020: this used to be a bot plus a Mini App. See
> ADR-023 and ADR-024 for why that changed and what it cost.

---

## 1. What you can do

| Screen | What it is for |
|---|---|
| **Ads** | Write your own message and post it to groups you choose. |
| **Auto-reply** | Answer people who message your account first. |
| **Forwarding** | Copy new messages from one chat into others, automatically. |
| **Accounts** | Connect a Telegram account by phone number, or a bot by token. |
| **Groups** | The groups each connection has joined, and where it may post. |
| **Activity** | What was sent, skipped or failed, and why. |

Commands: `/panel`, `/ads`, `/rules`, `/help`, `/cancel`.

`/cancel` abandons whatever multi-step flow you are in the middle of. Every
prompt mentions it.

---

## 2. Setting up, in order

### 2.1 Create the panel bot

Message **@BotFather**, send `/newbot`, and keep the token it gives you. This bot
is the panel. It must be a **different** bot from any forwarding bot: Telegram
allows only one program to receive a given bot's updates, and a second one gets
`409 Conflict`.

Put the token and your numeric Telegram id (ask **@userinfobot**) in `.env`:

```
ADMIN_BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=123456789
```

`ADMIN_TELEGRAM_IDS` empty means **nobody** can use the panel. That is
deliberate — a misconfigured deployment is locked rather than open.

### 2.2 Connect an account

Send `/start`, tap **Accounts → Add account**, and follow the prompts:

1. a name for your own reference;
2. the phone number, with country code;
3. the login code Telegram sends you;
4. the two-step verification password, if the account has one.

**Send the login code with spaces or dashes between the digits** — `1 2 3 4 5`.
Telegram cancels a login code it sees posted as plain digits in a chat. That
protection is on your side; the prompt says so and asks you to work with it.

Adding a bot instead is the same flow with a token from @BotFather. A bot can
only post in groups where you have added it as an administrator; an account can
post anywhere it has already joined.

### 2.3 Sync groups

**Accounts → (pick one) → Sync groups.** This reads the list of groups the
connection has already joined and records where it is allowed to post. It never
joins anything for you.

Nothing else works until this has run at least once — the group picker has
nothing to offer otherwise, and it says so.

---

## 3. Posting an ad

**Ads → New ad**, then:

1. a name, for your own reference;
2. the message, exactly as it should appear;
3. optionally an image, sent as a *photo* (a caption becomes the ad text);
4. the pause between groups — 3 seconds is the default;
5. **Groups**, which opens a tick-box list of groups this account can post in;
6. **Send now**, which shows a summary and asks once more.

The summary tells you how many groups, whether an image is attached, and roughly
how long it will take. Once sending starts you can pause it — but messages
already posted cannot be unsent, and the confirmation screen says that.

While it is sending, the ad's screen shows progress, per-group outcomes, and a
**Retry** button for groups that did not receive it. Retry never re-posts to a
group that already got the message.

### What the pause is for

Telegram documents roughly 20 messages per minute to one group and about 30
messages per second overall. A 3-second pause keeps one ad comfortably inside
both. If a group has slow mode enabled, Telegram will ask for a longer wait and
the system obeys it in full rather than working around it.

The panel refuses a pause that would push the last delivery more than six hours
out, and tells you the number so you can lower it.

---

## 4. Auto-reply

**Auto-reply → Edit reply**, write the text, then **Turn on**.

It answers people who message the connected account first — typically someone who
saw an ad in a group and wrote to you. It cannot do anything else:

* there is no recipient list, and no way to create one;
* a message in a group never produces a reply;
* the same person is answered once per waiting period (24 hours by default),
  and that record is in the database, so a restart does not answer everyone
  again.

It uses the same account as your ads. That pairing is the point: the ad brings
someone to the account, and the reply meets them there.

---

## 5. Forwarding rules

**Forwarding → New rule**: a name, then the chat to copy *from*, chosen from a
numbered list of chats the connection can read. The rule is created as a draft;
open it to choose the groups to copy into, then resume it.

Rule screens show status, source, destination count, per-destination delivery
counts and a plain-language reason for anything paused.

---

## 6. Security model

Anyone on Telegram can find and message a bot, so the allowlist is the entire
security model for this surface.

* `AdminOnlyMiddleware` runs before **every** handler, registered on both the
  message and the callback observers. A callback does not pass through message
  middleware, and missing that would leave every button unguarded.
* Non-allowlisted senders get the same reply either way, and the attempt is
  audited in its own transaction so the record survives the rejection.
* Handlers use the same `user_id`-scoped repositories as the HTTP API, so one
  admin cannot reach another's data even if the allowlist were bypassed.
* Accounts created through the bot store `password_hash = NULL`, and the password
  login path requires a stored hash, so they cannot be logged into with any
  password.

**Telegram account compromise equals panel compromise.** There is no second
factor on this surface. That is the honest statement of the tradeoff.

### Credentials in chat history

Bot tokens, phone numbers, login codes and 2FA passwords are typed into the chat,
because that is where the panel is. Telegram stores chat history on its servers,
so those messages existed there for a moment regardless of what happens next.

What the code does about it:

* every prompt says the message will be deleted, before you send anything;
* the message is deleted the instant it is read, on both sides;
* the value goes straight to the service layer — never into conversation state,
  never into a log, never into the database except as a hash;
* the log redaction filter is a backstop, not the plan.

Deletion is best-effort by definition: Telegram refuses to delete another
account's message after 48 hours. **Rotate your bot token after setup** — that is
cheap. A phone number cannot be rotated, which is why the login-code and 2FA
messages are the exposure that actually matters.

This is weaker than the HTTPS form it replaced. See ADR-024.

---

## 7. Screen mechanics

* Screens are **edited in place** as you navigate, so the chat stays one panel
  rather than an endless scroll.
* A panel older than about 48 hours cannot be edited by Telegram's rules; tapping
  a button on one sends a fresh panel instead of failing.
* `callback_data` is capped at **64 bytes**, and Telegram rejects the whole
  keyboard when one button exceeds it. Buttons carry ids only. The group picker
  addresses a group by its index in a list held in conversation state (ADR-028).
* Everything interpolated into a screen is escaped for MarkdownV2. Group titles
  are attacker-influenced — someone can name a group `*bold*` — and one unescaped
  character makes Telegram reject the message, so the screen simply never
  appears. `tests/integration/test_bot_flows.py` renders every screen through a
  checker for exactly this.

---

## 8. Alerts

The worker never calls the Bot API. When a rule or connection pauses itself, the
worker writes a row to `admin_notifications` and the bot drains that outbox and
sends it. Alerts therefore survive a bot restart, and a `dedupe_key` collapses a
failing rule's repeats into one message rather than a storm (ADR-022).
