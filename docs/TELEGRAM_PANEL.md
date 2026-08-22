# Telegram Control Panel

**Status:** Implemented.
**Last updated:** 2026-08-22

The admin panel lives inside Telegram. A bot gives you day-to-day control and
pushes alerts; a Mini App button opens the full panel for setup. The forwarding
engine is untouched — it never knew what its control surface was.

---

## 1. Why two surfaces

| Surface | What it does | Why |
|---|---|---|
| **Bot** (chat + inline buttons) | Status, pause/resume, retry, activity, **push alerts** | Always at hand, and it can reach *you* instead of waiting to be checked |
| **Mini App** (the React panel, opened inside Telegram) | Connect a bot/account, create and edit rules, browse chats | Credentials go over HTTPS. Typing them into a chat would put them in Telegram's message history |

That split is the whole design. **No secret is ever typed into a Telegram
chat** — not a bot token, not a phone number, not a login code, and above all
not a 2FA password.

## 2. Authentication

There is no password on this surface. Telegram signs the Mini App launch payload
with a key derived from the admin bot's token, so the backend can prove who
opened the panel:

```
secret_key        = HMAC_SHA256(<admin_bot_token>, "WebAppData")
data_check_string = every field except `hash`, sorted, joined with \n
valid             = hex(HMAC_SHA256(data_check_string, secret_key)) == hash
```

`POST /api/v1/auth/telegram` verifies that, then issues the same session cookie
the web panel uses — so every other endpoint works unchanged.

Three separate gates, in order:

1. **Signature** — proves the launch came from *our* bot. A payload signed by any
   other bot is rejected.
2. **Freshness** — `auth_date` older than `MINIAPP_MAX_AGE_S` (default 24h) is
   rejected, and a future-dated one is too.
3. **Allowlist** — being verified is not being authorized. Only Telegram user ids
   in `ADMIN_TELEGRAM_IDS` may proceed; everyone else gets a clear refusal and an
   audit record.

`ADMIN_TELEGRAM_IDS` is **empty by default**, so a misconfigured deployment is
locked rather than open. The bot refuses to start at all with an empty allowlist,
because the only thing it could do is reject everyone.

Accounts created this way have `password_hash = NULL`. The password login path
explicitly requires a stored hash, so a Telegram admin can never be reached
through the password form with any input.

## 3. Two rules that bite if ignored

**The admin bot token must differ from every forwarding bot token.** Telegram
permits a single `getUpdates` consumer per token; a second one receives
**409 Conflict**. Sharing a token makes the admin bot and the forwarding listener
fight over the same update stream, and both misbehave.

**`callback_data` is limited to 1–64 bytes.** Buttons carry ids only — never chat
titles or filter text. `rule:<uuid>:resume` is 43 bytes; a test asserts every
callback we generate stays under the limit.

## 4. What the bot can and cannot do

| Action | Bot | Mini App |
|---|---|---|
| See connection health, rule status, 24h counts | ✅ | ✅ |
| Pause / resume a rule | ✅ | ✅ |
| Retry failed destinations | ✅ | ✅ |
| Browse chats and eligibility reasons | ✅ | ✅ |
| Recent forwarding events | ✅ | ✅ |
| Trigger a chat sync | ✅ | ✅ |
| **Connect a bot or account** | ❌ by design | ✅ |
| **Create or edit a rule** | ❌ by design | ✅ |

The two ❌ rows are deliberate: both need secrets or multi-step input, and both
belong on HTTPS.

## 5. Push alerts

The advantage the web panel could never have. When a rule or connection
auto-pauses, the worker writes to an `admin_notifications` outbox and the bot
delivers it:

```
⚠️ Rule paused automatically

Announcements → Partners has been paused.

The rule was paused automatically after repeated serious failures.

Open the panel to review and resume it.
```

The outbox is a table, not a direct Bot API call from the worker, so an alert
survives a bot restart — the same durability rule the forwarding pipeline
follows. A `dedupe_key` collapses repeats, so a failing rule cannot turn into a
notification storm.

## 6. Setup

1. **Create the admin bot** — talk to `@BotFather`, `/newbot`, copy the token
   into `ADMIN_BOT_TOKEN`. This bot does no forwarding.
2. **Find your Telegram user id** — message `@userinfobot`. Put the number in
   `ADMIN_TELEGRAM_IDS` (comma-separated for several operators).
3. **Serve the Mini App over HTTPS** — set `MINIAPP_URL` to that origin. Telegram
   only accepts `https://` for a `web_app` button; with anything else the
   full-panel button is hidden rather than shown broken.
4. **Register the Mini App** — in `@BotFather`: `/mybots` → your bot → *Bot
   Settings* → *Menu Button* → set it to `MINIAPP_URL`.
5. `docker compose up -d` and send `/start` to your bot.

```bash
ADMIN_BOT_TOKEN=123456789:AA...          # separate from any forwarding bot
ADMIN_TELEGRAM_IDS=123456789,987654321   # empty = nobody
MINIAPP_URL=https://panel.example.com    # must be https
```

## 7. Honest limitations

- **Telegram account compromise = panel compromise.** There is no second factor
  on this surface. Whoever controls your Telegram account controls the panel.
  Keep 2FA enabled on your own Telegram account.
- **The allowlist is the only authorization.** Anyone can message the bot; the
  middleware runs before every handler, on both messages and button callbacks,
  and rejects non-admins identically so the panel's existence is not confirmed.
- **HTTPS is required for the Mini App.** Without it the bot still works, but
  connecting accounts and editing rules have nowhere to happen.
- **The bot is a control surface, not a forwarding path.** It never reads or
  relays customer content; it only shows counts, statuses and reason codes.

## 8. Processes

| Service | Command | Scale |
|---|---|---|
| `adminbot` | `python -m app.adminbot.main` | **1** — one `getUpdates` consumer per token |

Control commands from the bot enqueue durable `control_tasks` exactly like the
HTTP API does. Tapping *Sync* returns "queued" immediately; the worker does the
Telegram I/O. The bot never blocks on Telegram.
