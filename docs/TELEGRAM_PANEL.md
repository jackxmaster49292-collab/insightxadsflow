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
| **Users** *(operators only)* | Who is using this deployment; suspend an account. |

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

`ADMIN_TELEGRAM_IDS` are the **operators**. Empty means nobody, and the bot
refuses to start — a misconfigured deployment is locked rather than open.

By default only those ids can use the bot at all. To let other people in, see
§6.

### 2.2 Connect an account

Send `/start`, tap **Accounts → Add account**, give it a name, and the bot sends
you a **QR code**. On the phone holding the account you want to connect:

**Settings → Devices → Link Desktop Device**, then scan it.

If the account has two-step verification, send that password afterwards. It is
used once and never stored.

#### Why a QR and not a login code

Telegram **cancels any login code it sees your account send inside a chat**. So
typing the code into this bot burns it, and the sign-in fails with *"the code
was previously shared by your account"* even though the digits were right.

That protection is working as intended and is not worked around here. A QR
simply has no code to leak — nothing secret enters the conversation at all.

The codes expire in seconds, so the bot keeps sending fresh ones until you scan.
If you take too long it stops and clears the attempt so you can start again.

**Accounts → Add account by phone instead** is still there. It works only when
the account you are connecting is *not* the one you are messaging the bot from,
because then Telegram never sees that account send its own code.

#### Adding a bot

Same flow with a token from @BotFather. A bot can only post in groups where you
have added it as an administrator; an account can post anywhere it has already
joined.

#### If a sign-in gets stuck

Only one sign-in can be in progress at a time. If one fails and is left behind,
open **Accounts**, tap that connection, and use **Cancel sign-in** — then start
again.

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

## 6. Who can use it

`ACCESS_MODE` in `.env` decides:

| Value | Who gets in |
|---|---|
| `closed` *(default)* | Only the Telegram ids in `ADMIN_TELEGRAM_IDS` |
| `open` | Anyone who messages the bot, after accepting the terms |

The default is `closed` on purpose: pulling a new version must never silently
open an existing deployment to everyone who finds the bot.

`ADMIN_TELEGRAM_IDS` means **operators** — the people who run this deployment.
They can list accounts and suspend one. They **cannot** read anyone's ads,
rules or connections; suspending does not need that, and reading it would be a
privacy breach the product does not make (ADR-032).

### Before opening it up

Every connected Telegram account reaches Telegram **from this server's single
IP**, and Telegram correlates that. A handful of people is unremarkable. Dozens
of strangers all broadcasting from one datacentre address is a pattern Telegram
acts on, and the accounts it acts against are theirs.

There is no engineering fix for this that is not evasion — proxy rotation is
explicitly out of scope — so the mitigation is operational: keep the population
small enough that you know who is in it, and suspend accounts that misuse it.

### The terms screen

A new account sees one screen and nothing else until it accepts. It states
plainly that the tool posts from *their* account, that Telegram can restrict
that account if messages are reported, that this software will not help them
get around it, and that credentials typed into the chat were on Telegram's
servers for a moment.

The gate is in the middleware, not in the handlers, for the same reason the
access check is: a handler can forget, and forgetting once would let someone
use the tool without ever seeing what they are responsible for.

### Suspending an account

**Users → (pick one) → Suspend.** It takes effect immediately and stops work
already queued, not just new work:

* their forwarding rules pause;
* sending broadcasts pause and queued deliveries are cancelled;
* their connections drop out of the listener, releasing the Telethon client;
* every delivery path re-checks the owner before sending, so anything the
  cascade missed still cannot go out.

Reinstating lets them back in but **resumes nothing**. Their rules and ads stay
paused until they restart them. Auto-resuming a broadcast someone was suspended
over is the wrong default.

Messages already delivered stay where they are. Suspending cannot unsend
anything, and the confirmation screen says so.

---

## 7. Security model

* `AccessMiddleware` runs before **every** handler, registered on both the
  message and the callback observers. A callback does not pass through message
  middleware, and missing that would leave every button unguarded.
* It decides four things in order: allowed in, not flooding the bot, not
  suspended, terms accepted. Each has its own failure and its own message.
* Rejections give the same reply either way, and a denied attempt is audited in
  its own transaction so the record survives the rejection that caused it.
* Handlers use `user_id`-scoped repositories throughout, so one account cannot
  reach another's data even if the gate were bypassed. With the bot open, that
  isolation is load-bearing rather than theoretical, and has its own tests.
* Operator-only screens re-check on the handler. Hiding a button is
  presentation; a callback can be replayed by anyone who has seen it.
* Accounts created through the bot store `password_hash = NULL`, and the
  password login path requires a stored hash, so they cannot be logged into
  with any password.
* One person's updates to the bot are rate-limited (60/minute), so a stuck
  client or a script cannot occupy the panel.

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

## 8. Screen mechanics

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

## 9. Alerts

The worker never calls the Bot API. When a rule or connection pauses itself, the
worker writes a row to `admin_notifications` and the bot drains that outbox and
sends it. Alerts therefore survive a bot restart, and a `dedupe_key` collapses a
failing rule's repeats into one message rather than a storm (ADR-022).
