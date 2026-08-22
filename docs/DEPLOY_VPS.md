# Deploying to a VPS (first-time guide)

Written for someone who has just bought a VPS and has not run one before.
Every command is meant to be copy-pasted in order.

**Total time:** about 30 minutes.

---

## Before you start

You need two things:

| | What | Where from |
|---|---|---|
| 1 | **VPS IP + root password** | Your provider's welcome email |
| 2 | **Admin bot token** | [@BotFather](https://t.me/botfather) → `/newbot`. Must be a *different* bot from any forwarding bot |

Also get your numeric Telegram user id from [@userinfobot](https://t.me/userinfobot) — you will need it
for the allowlist.

**No domain, no TLS certificate and no open port.** The control panel is your
Telegram bot, and the bot reaches Telegram by making outbound requests. Nothing
in this stack accepts an inbound connection, so there is nothing to point a
domain at and nothing exposed to scan.

---

## Step 1 — First login and basic hardening

```bash
ssh root@<your VPS IP>
```

Change the root password immediately — providers email it in plain text:

```bash
passwd
```

Create a normal user (running everything as root is how servers get wrecked):

```bash
adduser insight
usermod -aG sudo insight
```

Copy your SSH key over so you can log in without a password. **From your own
machine**, in a new terminal:

```bash
ssh-copy-id insight@<your VPS IP>
```

No SSH key yet? Run `ssh-keygen -t ed25519` on your machine first, press Enter
through the prompts, then run the command above.

Now test it in that same new terminal — **do not close your root session until
this works**:

```bash
ssh insight@<your VPS IP>
```

Once that works, disable password logins and root SSH:

```bash
sudo sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

Firewall — **SSH only**. Nothing in this stack accepts an inbound connection, so
there is no other port to open:

```bash
sudo apt update && sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw --force enable
sudo ufw status
```

If you previously opened 80 and 443 for the old web panel, close them:

```bash
sudo ufw delete allow 80/tcp
sudo ufw delete allow 443/tcp
```

Automatic security updates:

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

---

## Step 2 — Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in so the group takes effect, then check:

```bash
docker run --rm hello-world
```

---

## Step 3 — Get the code

The repository is private, so authenticate first:

```bash
sudo apt install -y gh git
gh auth login          # choose GitHub.com → HTTPS → login with a browser
gh repo clone insightxpro/insightadsflow
cd insightadsflow
```

---

## Step 4 — Configuration

```bash
cp .env.example .env
```

Generate the two secrets. **Run each and paste the output into `.env`:**

```bash
python3 -c "import base64,os;print('ENCRYPTION_KEK='+base64.b64encode(os.urandom(32)).decode())"
python3 -c "import secrets;print('APP_SECRET_KEY='+secrets.token_urlsafe(48))"
python3 -c "import secrets;print('POSTGRES_PASSWORD='+secrets.token_urlsafe(24))"
```

Then edit `.env`:

```bash
nano .env
```

Set these (Ctrl+O to save, Ctrl+X to exit):

```bash
ENCRYPTION_KEK=<from above>
APP_SECRET_KEY=<from above>
POSTGRES_PASSWORD=<from above>

ENVIRONMENT=production
COOKIE_SECURE=true

ADMIN_BOT_TOKEN=<from @BotFather>
ADMIN_TELEGRAM_IDS=<your numeric id from @userinfobot>

TELEGRAM_PROVIDER=live

# Only needed to connect a Telegram *account* by phone number.
# Get them from https://my.telegram.org
TELEGRAM_API_ID=<from my.telegram.org>
TELEGRAM_API_HASH=<from my.telegram.org>
```

> **`ADMIN_TELEGRAM_IDS` empty means nobody can use the panel.** That is
> deliberate: a misconfigured deployment is locked rather than open to everyone
> who finds the bot. `deploy.sh` refuses to continue without it.

> **`ENCRYPTION_KEK` is the one to be careful with.** It encrypts your bot tokens
> and Telegram sessions, and it lives only in this file — never in the database.
> Lose it and every stored connection becomes undecryptable and must be
> reconnected. Keep a copy somewhere safe, and **never** put it in a database
> backup: a backup alone must not be enough to decrypt your sessions.

---

## Step 5 — Start it

```bash
./deploy.sh
```

That is the whole thing. The script builds, starts postgres and redis, **waits
for them to be genuinely ready**, checks the database is usable, applies
migrations, starts everything else, and then reports honestly — including
dumping the logs of anything that is crash-looping.

Order matters here. Starting all containers at once is what produces
`host 'postgres' does not resolve`: the workers come up before the database
container exists, and Docker's DNS does not answer for a container that is not
running yet.

```bash
./deploy.sh --rebuild   # force a clean image rebuild
./deploy.sh --status    # what is running
./deploy.sh --logs      # follow the backend logs
```

Re-running it is safe and never deletes data.

Every process waits up to 60 seconds for the database and Redis to become
reachable before giving up, so a slow VPS is not mistaken for a broken one. A
rejected password or a missing schema still fails immediately — waiting cannot
fix those, and the real message should not be delayed by a minute.

---

## Step 6 — The database

**You do not install PostgreSQL.** It runs as a container, already configured.
There is nothing to set up by hand — no `apt install postgresql`, no `createdb`,
no `psql` user creation. Compose did it.

One command creates the tables:

```bash
docker compose run --rm api alembic upgrade head
```

That is the whole database setup. Verify:

```bash
docker compose exec postgres psql -U insight -d insight -c "\dt"
```

You should see 18 tables (`users`, `forwarding_rules`, `forwarding_jobs`, …).

### Where the data lives

In a Docker **volume** called `insightadflow_pgdata`, managed by Docker and
independent of the containers. `docker compose down` does **not** delete it;
`docker compose down -v` **does** — that flag wipes your database, so avoid it.

```bash
docker volume ls | grep pgdata
```

### Backups

Take one before any upgrade, and on a schedule:

```bash
mkdir -p ~/backups
docker compose exec -T postgres pg_dump -U insight insight | gzip > ~/backups/db-$(date +%F).sql.gz
```

Automate it daily at 3am:

```bash
crontab -e
```

Add this line (adjust the path if you cloned elsewhere):

```
0 3 * * * cd ~/insightadsflow && docker compose exec -T postgres pg_dump -U insight insight | gzip > ~/backups/db-$(date +\%F).sql.gz
```

To restore:

```bash
gunzip -c ~/backups/db-2026-08-22.sql.gz | docker compose exec -T postgres psql -U insight -d insight
```

> Keep `ENCRYPTION_KEK` **out** of the backup directory and store it separately.
> The split is the point: a stolen backup is useless without the key.

---

## Step 7 — Check it works

Nothing is published to the internet, so the health check runs inside the
network:

```bash
docker compose exec api curl -s localhost:8000/api/v1/health
```

Expected:

```json
{"status":"ok","database":true,"redis":true,"telegram_provider":"live"}
```

---

## Step 8 — Set it up from Telegram

Open Telegram and send `/start` to your admin bot. Everything from here happens
in that chat.

1. **Accounts → Add account.** Give it a name, then the phone number of the
   account you want to connect, with the country code.
2. Send the login code Telegram delivers to that account, then its two-step
   verification password if it has one.
3. **Accounts → (your account) → Sync groups.** This reads the groups the
   account has already joined. Wait a few seconds, then check **Groups**.
4. If the sign-in fails, open the connection and tap **Cancel sign-in** before
   trying again — only one sign-in can be in progress at a time.
5. **Ads → New ad** → name → your message → optionally an image → **Groups** →
   tick them → **Send now**.

**Important:** Telegram cancels any login code it sees an account send inside a
chat. So this cannot connect the account you are messaging the bot *from* — it
fails every time with "the code was previously shared by your account". Connect a
**different** account, or connect a bot instead.

Each credential message is deleted the moment the bot reads it, and the bot says
so before asking. Telegram's servers still held it briefly, which is why the
next section matters.

---

## Step 9 — Rotate the bot token

Now that setup is done, rotate the token you pasted into `.env`:

```bash
# In @BotFather: /mybots → your bot → API Token → Revoke current token
nano .env          # paste the new ADMIN_BOT_TOKEN
./deploy.sh
```

This is worth doing because the token was in a terminal, a file, and possibly
your shell history. It costs a minute and invalidates anything that leaked.

---

## Everyday commands

```bash
docker compose ps                          # what is running
docker compose logs -f worker              # follow one service
docker compose logs --tail=100 adminbot    # recent lines
docker compose restart worker              # restart one service
docker compose down                        # stop everything (data is kept)
docker stats --no-stream                   # memory and CPU
```

Deploy an update:

```bash
cd ~/insightadsflow
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose run --rm api alembic upgrade head
```

---

## If something is wrong

**Upgrading from a version that had a web panel**

Earlier releases shipped a React panel behind Caddy, and the Compose project was
named `insight-store`. Both changed, which has two consequences worth knowing
before you run `./deploy.sh`:

*Old containers keep their ports.* Remove them first, or the new stack cannot
bind:

```bash
docker compose -p insight-store down --remove-orphans
```

*The database volume is per-project, so the rename orphans it.* Your old data is
in `insight-store_pgdata` and the new stack creates `insightadflow_pgdata`. If
you have connections or rules worth keeping, move the data across before
starting:

```bash
# 1. Dump from the old volume
docker run --rm -v insight-store_pgdata:/var/lib/postgresql \
  -e POSTGRES_PASSWORD="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)" \
  -d --name pgold postgres:18-alpine
sleep 8
docker exec pgold pg_dump -U insight insight > /tmp/insight.sql
docker rm -f pgold

# 2. Start the new stack, then load it
./deploy.sh
docker compose exec -T postgres psql -U insight insight < /tmp/insight.sql
```

If you have nothing worth keeping — which is the usual case for a fresh
deployment — skip all of that and just reconnect your account in the bot. The old
volume can then be deleted with `docker volume rm insight-store_pgdata`.

**Bot does not respond to `/start`**

```bash
docker compose logs adminbot | tail -30
```

- `Telegram rejected ADMIN_BOT_TOKEN` → wrong or revoked token
- `ADMIN_TELEGRAM_IDS is empty` → allowlist not set; the bot refuses to start
  rather than run an unprotected panel
- Bot replies "not authorized" → your numeric id is not in `ADMIN_TELEGRAM_IDS`

**409 Conflict in the logs**

You used the same bot token for the admin bot and a forwarding connection.
Telegram allows only one `getUpdates` consumer per token. Create a second bot.

**Worker / listener / scheduler keep restarting**

Read the top of the log — they now print a diagnosis rather than a traceback:

```bash
docker compose logs worker | head -20
```

| Message | What to do |
|---|---|
| `The database host 'postgres' is the correct name, but it does not resolve` **after retrying for 60s** | Historically this was also caused by a `POSTGRES_PASSWORD` containing `@`, which truncated the host for the driver while the diagnostic read it correctly. Fixed — the password is percent-encoded now — but if you see it on an old build, `git pull` first | The name is right, so nothing to edit — the container is not running. `docker compose ps`, then `docker compose logs postgres`. Usually caused by starting with only one compose file: the prod file alone does not define postgres at all. `./deploy.sh` handles the ordering for you |
| `Cannot resolve the database host 'db'` | `DATABASE_URL` is set in `.env` and points at a host that does not exist inside the Docker network. **Delete the line** — Compose sets it for every service. Verify with `docker compose config \| grep DATABASE_URL` |
| `Postgres rejected the password` | `POSTGRES_PASSWORD` was changed *after* the volume was created. Postgres only applies it when initialising a new data directory. Restore the old password, or `docker compose down -v` to start fresh — **that deletes all data** |
| `The database is reachable but has no tables yet` | Run `docker compose run --rm api alembic upgrade head` |
| `Nothing is listening on the database host` | `docker compose ps` — the postgres container is not up |

> The most common cause is a stale `DATABASE_URL` or `REDIS_URL` in `.env`. Both
> are commented out in `.env.example` on purpose: Compose owns them, and setting
> them there only takes effect when something runs *outside* Compose — at which
> point it points somewhere wrong. If your `.env` has them, remove them.

**Nothing is being forwarded**

```bash
docker compose logs listener | tail -30
docker compose logs worker | tail -30
```

Then check the rule in the panel — the per-destination list gives a reason for
every skip.

**Out of disk**

```bash
df -h
docker system prune -af      # removes unused images and build cache
```

---

## What this costs you in resources

Measured on the running stack:

```
~575 MiB RAM total across all containers
~0% CPU when idle
~2 GB disk for images and data
```

A 4 GB VPS is comfortable. The bottleneck is never the server — it is Telegram's
rate limits (~30 messages/second), so a larger machine does not make forwarding
faster.
