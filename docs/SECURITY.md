# Security Model

**Status:** Implemented.
**Last updated:** 2026-08-22

---

## 1. Assets

1. Telegram **bot tokens** — full control of a bot.
2. Telegram **MTProto session strings** — full control of a *personal account*. Highest-value asset.
3. Per-account **`access_hash`** values — enable peer interaction.
4. Application credentials and session cookies.
5. Customer message content in transit through the forwarder.
6. Rule configuration (reveals business relationships).

## 2. Threat model

| # | Threat | Impact | Mitigation |
|---|---|---|---|
| T1 | Account takeover of the app login | Full access to connections | Argon2id, login rate limit + lockout, server-side revocable sessions, re-auth for sensitive ops |
| T2 | Theft of bot tokens / MTProto sessions | Telegram account compromise | Envelope encryption (§4), KEK outside the DB, no plaintext at rest or in logs |
| T3 | Cross-user object access (IDOR) | Data breach across customers | Every query scoped by `user_id` at the repository layer (§5) |
| T4 | Unauthorized rule creation / destination modification | Content sent to attacker-chosen chats | Ownership check + eligibility revalidation + audit event on every mutation |
| T5 | Malicious media or links flowing through | Harm to destination members | Forward-by-reference (no re-hosting), no auto-download, size caps, no link rewriting |
| T6 | Secrets leaking into logs | Credential disclosure | Structural redaction filter (§6), tested |
| T7 | Replay of activate/pause/resume/retry | Duplicate/conflicting side effects | `Idempotency-Key` required on all state-changing POSTs (§7) |
| T8 | Queue message tampering | Forged deliveries | Jobs live in Postgres, not in the queue payload; Redis carries only job ids; Redis is never exposed |
| T9 | Abuse of the forwarding service (spam relay) | Platform harm, bans | Authorization revalidation, protected-content refusal, safety pause, audit trail, ToS acceptance |
| T10 | SSRF | Internal network access | No user-supplied URL is ever fetched server-side |
| T11 | Injected markup in a chat title | A screen Telegram refuses to render, or misleading formatting | Every interpolated value is MarkdownV2-escaped; a checker renders every screen in the test suite and fails on an unescaped special character |
| T12 | CSRF | Forced state change | `SameSite=Strict` cookies + double-submit token on mutations |
| T13 | SQL injection | Data breach | SQLAlchemy parameterized queries only; no string-built SQL |
| T14 | Insecure file handling | RCE / traversal | No media staging in MVP; if added, private storage + signed expiring URLs + content-type allowlist |
| T15 | Dependency / container / secret vulnerabilities | Supply chain | `pip-audit`, `npm audit`, Trivy, gitleaks in CI (§10) |
| T16 | 2FA password capture | Telegram account compromise | Used once in memory, never stored/hashed/logged; not accepted over any GET |
| T17 | Session file exfiltration from disk | Account compromise | Telethon `StringSession` in the encrypted DB column — **no session files on disk** |

## 3. Authentication and authorization

- Argon2id password hashing with tuned parameters; minimum length enforced; breach-list check optional.
- Sessions are server-side rows (`app_sessions`) keyed by a SHA-256 hash of the cookie value. Logout,
  "revoke all sessions", and password change delete rows — revocation is immediate and real.
- Cookies: `HttpOnly`, `Secure`, `SameSite=Strict`, host-only, sliding expiry with absolute cap.
- **Every** protected endpoint enforces authentication server-side. There is no client-side-only gate.
- **Object-level authorization** is structural: repository methods require a `user_id` argument and
  there is no method that fetches a connection, chat, rule, job, or event without one. A test walks
  every route and asserts a second user receives `404` (not `403`, which would confirm existence).
- Strict user/workspace isolation is implemented now, even though release 1 is one workspace per user.

## 4. Secret storage — envelope encryption

```
KEK (AES-256)  ← env var / secrets manager, never in the database, versioned
   └─ wraps ─► DEK (per connection, random 256-bit)
                  └─ AES-256-GCM ─► session string / bot token / access_hash
```

- Each connection gets its own DEK; the wrapped DEK is stored beside the ciphertext with a
  `key_version`. KEK rotation rewraps DEKs without touching or decrypting message data.
- GCM AAD binds ciphertext to `(connection_id, field_name)`, so a ciphertext cannot be moved between
  rows or columns.
- Decryption happens only inside the adapter layer, only in worker/listener processes, and the
  plaintext never leaves that scope or enters a log, an exception message, or an API response.
- MTProto sessions use Telethon's `StringSession` so nothing is written to a session file on disk.
- The raw 2FA password and login code are never persisted in any form.

## 5. Data isolation

Repository layer signature discipline:

```python
async def get_rule(self, *, user_id: UUID, rule_id: UUID) -> ForwardingRule | None: ...
```

No `get_rule(rule_id)` overload exists. Chats and jobs are reached only through their connection, which
is reached only through `user_id`. Postgres row-level security is a documented V1 defence-in-depth
addition, not a substitute for this.

## 6. Log redaction

Structured JSON logs with a redaction processor applied **before** any sink:

- Denylist by key: `token`, `bot_token`, `session`, `session_string`, `access_hash`, `password`,
  `code`, `phone`, `authorization`, `cookie`, `2fa`, `dek`, `kek`.
- Pattern scrub: bot-token shape (`\d{8,10}:[A-Za-z0-9_-]{35}`), E.164 phone numbers, long base64 blobs.
- **Message content is not logged.** Events store a length, media type, and reason code — never body
  text. `forwarding_events.detail_safe` is redacted at write time and is the only field the UI shows.
- Exception handlers log an error class and correlation id, never the raw provider exception string
  (Telethon exceptions can embed request parameters).
- A unit test feeds a known token, session string, phone number, and 2FA password through the logger
  and asserts none appear in the output.

## 7. Request integrity

- All state-changing `POST`/`PATCH`/`DELETE` accept an `Idempotency-Key` header; activate, pause,
  resume, retry-failed, and connection-create **require** one. Replays return the stored response;
  a same-key different-body request returns `409`.
- Every response carries an `X-Correlation-Id`, propagated into jobs and events for tracing.

## 8. Rate limiting

| Surface | Limit (default, configurable) |
|---|---|
| Login | 5 / 15 min per IP **and** per account, then lockout |
| Registration | 3 / hour per IP |
| Connection create / verify | 5 / hour per user |
| Chat sync | 4 / hour per connection |
| Rule create | 30 / hour per user |
| Control commands | 60 / min per user |
| All other authenticated | 600 / min per user |

These protect *the application*. They are separate from Telegram pacing (ARCHITECTURE §6.1) and are
neither monetization limits nor customer-facing quotas.

## 9. Transport and headers

TLS 1.2+ terminated at nginx, HSTS with preload. `Content-Security-Policy` with no `unsafe-inline` and
no `unsafe-eval`; `X-Content-Type-Options: nosniff`; `Referrer-Policy: no-referrer`;
`X-Frame-Options: DENY`; `Permissions-Policy` denying camera/microphone/geolocation. CORS restricted to
the configured panel origin with credentials — no wildcard.

## 10. Supply chain and CI

`pip-audit` and `npm audit --audit-level=high` on every PR; Trivy image scan; `gitleaks` secret scan;
lockfiles committed (`uv.lock`/`requirements.txt` hashes, `package-lock.json`); Dependabot weekly;
pinned base images by digest.

## 11. Audit and customer data rights

Audit events for connection create/verify/disconnect/revoke, sync, rule create/edit/activate/pause/
resume/delete, retry-failed, export, and deletion. Each records actor, object, correlation id, hashed
IP, and redacted metadata.

Customer-facing: **disconnect** (stop and clear the local session), **revoke** (also call
`auth.logOut` so Telegram invalidates the session server-side), **export** (JSON of account, chats,
rules, and recent events — never secrets), **delete** (removes user data and revokes all Telegram
sessions).

## 12. Abuse posture

The system refuses to relay from protected/`noforwards` chats in *both* forward and copy mode; refuses
destinations it cannot confirm authorization for; auto-pauses on repeated authorization or restriction
errors; keeps a durable audit trail; and never presents "unlimited" capacity. Operators are expected to
accept the Telegram API Terms; the README states plainly that misuse can get a bot or a personal
account restricted by Telegram, and that the software will not help evade such enforcement.
