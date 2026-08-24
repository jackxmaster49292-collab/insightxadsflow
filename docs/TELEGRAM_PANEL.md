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

Send `/start`, tap **Accounts → Add account**, then:

1. a name for your own reference;
2. the phone number, with country code;
3. the login code Telegram sends to that account;
4. its two-step verification password, if it has one.

#### Read this before you start

**Telegram cancels any login code it sees an account send inside a chat.** It is
a protection against the "send me your code" scam, and it is not worked around
here.

So connecting **the account you are messaging this bot from will fail**, every
time, with *"the code was previously shared by your account"* — however
carefully you type the digits. Nothing is misconfigured when that happens.

It works when the account you are connecting is a **different** account from the
one driving the bot: Telegram never sees that account send its own code, so the
code survives.

The panel says all of this on the first screen, before asking for anything.

#### If a sign-in fails or gets stuck

Only one sign-in can be in progress at a time, and a failed one stays behind.
Open **Accounts**, tap that connection, and use **Cancel sign-in** — then start
again. Without that you would be told "already in progress" forever.

#### Adding a bot

Same flow with a token from @BotFather. A bot token is not cancelled the way a
login code is, so this always works. A bot can only post in groups where you
have added it as an administrator; an account can post anywhere it has already
joined.

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
5. **Repeat**, if the ad should keep going out — see below;
6. **Groups**, which opens a tick-box list of **groups** this account can post
   in — private chats and channels are never offered, because an ad in someone's
   DM is unsolicited messaging and a channel you own is better posted to
   directly. **Select all** takes every group at once; **Select page** takes the
   eight on screen;
7. **Send now**, which shows a summary and asks once more.

Your message is posted **exactly as you typed it** — bold, italics, links and
premium emoji all survive. Write it in Telegram the way you want it to appear
and send it to the bot; nothing is re-typed or re-parsed on the way.

Premium emoji are the one thing with a condition attached. A custom emoji is an
ordinary emoji character in the text plus an instruction to draw the premium one
instead — and Telegram honours that instruction **only for a Telegram Premium
account**. Without Premium the ad arrives showing the ordinary characters.

The panel checks the connected account and warns on the compose and confirm
screens before you send, rather than leaving you to notice it across 157 posted
ads. If it has never checked — a connection made before this existed, for
instance — it says that instead of assuming the worst; tap **Check health** on
the connection and reopen the ad.

**The preview inside this bot always shows ordinary emoji**, whatever your
account has. The preview is plain text, and a bot cannot render a custom emoji
at all — Telegram reserves those for bots with a Fragment username.

So the compose screen tells you what it captured instead:

```
Formatting kept — bold, italic · 17 premium emoji · 1 link
```

That line is the confirmation that your formatting survived. Judge the result
from it, or from a posted ad — never from the preview.

The summary tells you how many groups, whether an image is attached, and roughly
how long it will take. Once sending starts you can pause it — but messages
already posted cannot be unsent, and the confirmation screen says that.

### Repeating an ad

**🔁 Repeat** on the compose screen asks how often. Answer in hours, minutes
or both — `6h`, `90m`, `1h 30m`; a bare number means hours. `0` posts it once
and stops, which is the default.

A round is one pass over every group you picked. When it finishes, the clock
starts — so a repeat of 6 hours means six hours **after the last group receives
it**, not six hours after the first. The ad then goes out to the same groups
again, and keeps doing that until you pause or stop it. A repeating ad never
finishes on its own.

Two intervals are refused, both with the arithmetic:

* **shorter than an hour.** The account posting the same message into the same
  group every few minutes is *yours*, and Telegram restricts accounts for
  exactly that. The tool will not help you do it.
* **shorter than one round.** If 300 groups at 3 seconds apart take 15 minutes,
  a 10-minute repeat would start round two before round one had finished and
  some groups would get the ad twice in a row.

While it runs, the ad's screen shows **Rounds sent** and when the next one is
due. Each round re-checks every group, including any that refused last time —
if an admin has since let you post there, the next round gets through.

**⏸ Pause** stops the next round; **▶️ Resume** picks up where it left off
without re-posting to groups already done in the current round. **🚫 Stop**
ends it for good.

### Editing an ad that is already running

**✏️ Edit** on the ad's screen reopens the compose view — message, image,
pause, repeat and groups are all changeable. A running ad is paused first,
deliberately: changed mid-round, some groups would get the old wording and some
the new. **Save and resume** continues from where it left off; groups this
round has already posted to are not posted to again, and a group you untick
stops receiving future rounds. On an ad that already finished, the same button
reads **Run this ad again?** and does exactly that — every selected group is
posted to again.

### When a group did not get it

A finished ad that some groups refused is **not** shown as a plain green tick —
the list shows ⚠️ with a delivered count like `3/5`, and the ad's screen says
*"2 of 5 did not receive it"*.

**🧾 Groups** lists every group by name with what happened to it — problems
first with the reason under each, then groups still waiting their turn, then
the delivered ones. **📊 Events** is the same information in time order.

What the reasons mean in practice:

* **A refusal** — the account is not allowed to post there (removed, muted, or
  the group now requires admin). Asking again does not change a refusal, so it
  is not retried in this round. On a repeating ad every group is re-checked
  each round, so a permission that comes back heals on its own; on a one-shot
  ad, **Retry** covers it.
* **A network error is not a refusal.** If the connection blinks mid-delivery
  or mid-check, that group is retried automatically with increasing gaps — it
  is only given up on after several failed attempts, and then it shows up under
  **Retry** rather than disappearing.
* **A long Telegram wait** pauses the whole ad — and it now resumes by itself
  the moment the wait is over. The wait is obeyed in full, never shortened.

### Premium icons on the panel itself (operators)

**✨ Icons** on the operator home turns the panel's own icons into custom
\(premium\) emoji. Tapping **Extract now** asks Telegram — through your
connected account — which custom emoji match each icon the panel draws, and
stores their ids; nothing is hardcoded.

It upgrades both surfaces: emoji in screen text become inline custom emoji,
and a button whose label leads with a mapped emoji gets Telegram's
`icon_custom_emoji_id` — the icon drawn before the label (Bot API 10.2).

When do they render? Telegram's rule, for text and buttons both: the bot owns
a **Fragment username**, *or* the **bot's owner has Telegram Premium** and the
message is sent directly by the bot — which every panel screen is, so a
premium owner is enough here. If Telegram refuses anyway, the panel quietly
falls back to plain icons rather than breaking, and the ✨ Icons screen says
that is what happened; extract again after fixing the cause and it retries.

**You should not have to do anything.** When the panel starts and no icons are
stored yet, it fetches them itself through your connected account — one lookup
per icon, in the background. Only an *operator's* account is ever used for
this. The two manual paths below stay for re-running it or for a deployment
where no account is connected yet.

**The simplest way needs no login**: tap **📥 Send emojis** and send the
premium emoji from your own keyboard — one message, as many as you like, and
more messages add more. The ids ride in on the message itself, so nothing
connects and nothing signs in. (Your own account can never be *connected*
from this chat — Telegram burns any login code it sees an account send — but
sending emoji is just a message.) The account-search path still exists for
operators who do have a connected account.

### Renaming buttons (operators)

**🔤 Buttons** on the operator home lists every renameable button label. Tap
one, send the new text (up to 32 characters, one line), and it changes
everywhere that button appears — stored in the database, surviving restarts.
Send `-` while renaming to go back to the built-in label.

### Starting later

**🕒 Start at** on the compose screen sets when an ad begins:

* `21:30` — at that time, tonight if it is still to come, otherwise tomorrow
* `2h`, `90m`, `1d`, `3h 30m` — from now
* `now` — no waiting

The screen always shows the answer twice — `25 Aug, 11:30 — in about 13.5
hours` — because a wrong timezone looks perfectly fine as a clock time and
obviously wrong as a duration. If it comes back wrong, send your timezone at
the same prompt (`Asia/Kolkata`) and then the time.

Pressing **Send** on a scheduled ad queues it rather than posting: the ad list
shows it as 🕒 scheduled until its time comes. It is checked for problems when
you press Send, not at six in the morning.

### Clearing out groups that refuse you

Under **💭 Groups**, a **🧹 Refusing posts** entry appears when any of your
groups will not accept a post. It lists each one with the reason — not allowed
to post, no longer a member, admins only, and so on.

* Tap a group to take it out of your ads.
* Or **🗑 Remove all** to drop every lasting refusal at once.

Groups marked ⏳ are excluded from *Remove all* — a slow-mode or flood wait
clears by itself, and removing a group over one would throw away a group that
was about to work again. You can still remove those individually.

**Removing does not leave the group.** The account stays a member; the ads stop
addressing it, and you can pick it again from the group list whenever you like.
Anything a group already received stays on the record.

Nothing here is automatic, deliberately: only you can tell which refusals are
worth waiting out.

### Speed

**⚡ Speed** on the compose screen sets how fast a round works through the
groups, and each preset says what it means for *your* group count:

| | between groups | 150 groups |
|---|---|---|
| ⚡ Fast | 0.25 s | under a minute |
| 🚶 Normal | 3 s | about 7 minutes |
| 🐢 Careful | 10 s | about 25 minutes |

**Custom** takes any value from 0.25 s upward.

At the 500-group ceiling on Fast the round schedules across about **2 minutes**
— measured, not estimated: `test_an_ad_reaches_five_hundred_groups_once_each`
runs a real 500-group ad and checks every group is addressed exactly once.

If you have **more than 500 groups**, one ad will not take them all: the panel
refuses with the count and asks you to split it. Either make two ads, or raise
`MAX_BROADCAST_TARGETS` in `.env` and redeploy. The limit is an operational
bound on how much one tap can set in motion, not a licence restriction — but
raising it raises how much a single mistake sends, so it is a deliberate
change.

They cannot go out *truly* at once: it is one account over one connection, so
messages leave one after another — but on Fast several are in the air together.
Fast is roughly 4 messages a second, an order of magnitude below Telegram's
documented rate. That is where the dial stops, because going faster buys
seconds across the whole round and risks **your** account being read as a
flood. Every wait Telegram asks for is still obeyed in full, and a group with
slow mode adds its own.

### Keeping your own copy (Archive)

**🗄 Archive** (operators only) picks one group to keep copies in. There is
one archive for the whole deployment, so ads sent from any operator account
land in the same group. Make a
group, keep it to yourself, and point this at it. There are two ways, and they
differ in *who* posts the copies:

* **🤖 Use a group the bot is in** — add the bot to the group as an admin who
  may post, then send its chat id (or forward any message from it). The copies
  come from the **bot**. Prefer this: the bot keeps working even if the account
  that posts your ads is ever gone, which is the whole point of an archive.
  The bot posts one line into the group when you set it, so you find out
  immediately if it cannot.
* **Tap one of your own groups** in the list — the copies come from the same
  account that posts the ads, which has to be a member of that group.

Only one is active at a time; setting either clears the other.

After every round the bot posts two things there, in this order:

1. **The ad itself** — the same text, the same bold, the same premium emoji.
2. **An index** of every group that received it, with a link to each post.

The order is deliberate, and so is the copy. A link into a **private** group
(`t.me/c/…`) only opens for *members* of that group — so if the account that
posted is ever gone, every one of those links is dead. Links to **public**
groups (`t.me/username/…`) keep working for anyone. And a **basic** group has
no message link at all; Telegram publishes no form for them, so the index says
so rather than inventing one.

For a **private** group the index also records what the group says about
itself — its bio and its member count — because months later the title alone
is hard to recognise, and finding a private group again is exactly the hard
part. (That is the group's own description, a thing Telegram publishes about
the chat; nothing reads its members.) **Every** private group in the round is
covered — the first big round spends a couple of minutes learning them before
the archive is posted, and after that they are remembered. If Telegram asks
for a long wait mid-way, the rest carry over and every following round keeps
trying until each one is learned.

The copy is the part that survives all of that. That is why it goes first.

The copies are sent by the same connected account that posted the ads, into a
group it is already in — the bot does not need to be added anywhere.

### You get told how it went

When a round finishes the bot messages you by itself: how many of the groups
received it, how many did not, and where to look. You do not have to sit
watching the screen. A repeating ad reports once per round.

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

## Whose ads are archived (operators)

By default **everyone's** — every account that uses this bot, including any
added later, has its ads copied into your archive group along with the list of
groups each one reached. That is the switch on the **🗄 Archive** screen:

* **👥 Everyone** — the default. New accounts are included automatically.
* **👤 Only chosen accounts** — nothing is copied unless you switch it on per
  account.

To change one account either way, open **👥 Users**, tap it, and use **📁 Copy
their ads** / **🚫 Stop copying their ads**. A choice made about an account
beats the default in both directions, so "everyone except this one" and "nobody
except this one" are both one tap. **↩️ Follow the default** puts it back to
whatever the switch says.

Operator access itself is *not* granted from the panel. It stays in
`ADMIN_TELEGRAM_IDS` on the server, because on an open deployment anyone gets
an account just by messaging the bot.

## Seeing another account's groups (operators)

**👥 Users** → tap an account → **💭 Their groups** lists what that account is a
member of: group or channel, and whether it can post there. Useful when you
drive the bot from more than one of your own accounts and do not want to switch
Telegram accounts to check.

Each chat carries a link where Telegram publishes one — `t.me/username` for a
public chat, `t.me/c/…` for a private one (which opens for members), and
nothing for a basic group, which the screen says rather than leaving blank.
Buttons switch between all, groups only and channels only.

Private conversations are not listed. They are synchronized, but they are not
what this screen is for.

It shows titles, links and posting rights only. No operator can read a message,
an ad draft, or an auto-reply through this bot.
