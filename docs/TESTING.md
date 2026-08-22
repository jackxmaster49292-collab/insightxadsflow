# Testing Strategy

**Status:** Implemented.
**Last updated:** 2026-08-22

---

## 1. Ground rule

**Automated tests never contact real Telegram servers.** `TELEGRAM_PROVIDER` defaults to `mock` and CI
sets it explicitly. A separate, opt-in `pytest -m live_integration` profile exists, is excluded from the
default run, requires `TELEGRAM_PROVIDER=live` plus real credentials, and is marked in the README as
**unsafe to run with production credentials**. A CI guard fails the build if `live` appears in any test
configuration.

## 2. `MockAdapter`

Implements the full `TelegramAdapter` protocol with scriptable behaviour: queue inbound messages,
control per-chat source/destination eligibility, and inject any error class on demand —
`FloodWait(seconds)`, `AuthRevoked`, `WriteForbidden`, `MessageDeleted`, `ProtectedContent`,
`NetworkTimeout`, `Unknown`. It records every call so tests can assert on retry counts, honoured wait
durations, and idempotency-key reuse.

## 3. Required unit and integration tests

| Area | Assertions |
|---|---|
| Login & session expiry | Valid/invalid login, lockout after 5 failures, expired cookie rejected, logout revokes server-side |
| Object-level authorization | For **every** route: user B gets `404` on user A's object. Table-driven over the route registry |
| Connection state transitions | `pending→awaiting_code→awaiting_2fa→active`, and every invalid transition rejected; duplicate simultaneous attempt blocked by the partial unique index |
| Session encryption | Round-trip encrypt/decrypt; wrong KEK version fails; AAD mismatch (moved ciphertext) fails |
| Log redaction | Bot token, session string, phone, login code, 2FA password fed through the logger appear nowhere in output |
| Chat sync & eligibility | New chats created, removed chats deactivated, eligibility flips recorded with reason codes |
| Rule validation | Ineligible source/destination rejected; cross-connection chat rejected; `delay_ms` bounds; empty destinations; copy-mode-on-protected refused |
| Keyword filters | Include/exclude, substring vs word mode, case-insensitivity, unicode, empty lists = no filtering |
| Media filters | Each type included/excluded correctly; unknown type skipped with reason |
| Duplicate idempotency keys | Second insert violates the unique constraint and is handled, not crashed |
| Duplicate events after reconnect | Replaying the same update creates **zero** new jobs |
| Queue retry & worker crash recovery | Expired lease reclaimed; job completes exactly once |
| Flood-wait classification | `retry_after`/`FloodWaitError` honoured **in full**; a test asserts the slept duration is never less than the provider value |
| Permanent content & permission failures | Not retried; skipped with the correct reason code |
| Pause/resume | Paused rule creates no new jobs; resume restores; both survive restart |
| Rule edits with queued jobs | Removed destination skipped, filtered-out message skipped, unchanged job delivers with the original key |
| Disconnect | Dependent rules → `disconnected`, pending jobs → `skipped`, `revoke:true` calls `auth.logOut` |
| Protected/unsupported messages | Skipped in **both** forward and copy mode with visible reasons |
| Safe API errors & rate limits | No provider strings or stack traces leak; limits return `429` with `Retry-After` |
| API never blocks | A test asserts no adapter call occurs within a request handler |
| Response-model secret scan | Reflection over every Pydantic response model asserts no denylisted field name |
| Frontend states | Loading, empty, error, and permission-denied render for every page |

## 4. End-to-end mocked scenario

One test, executed against the real stack with `MockAdapter`, covering the full required path:

1. Create a test user and connection.
2. Synchronize mock source and destination chats.
3. Create and activate a forwarding rule (1 source → 3 destinations).
4. Receive a mock source message.
5. Assert **three** destination jobs are created.
6. Destination 1 succeeds; destination 2 fails with `NetworkTimeout` (transient); destination 3 fails
   with `WriteForbidden` (permission).
7. Assert **only** destination 2 is retried — destination 3 is not, and destination 1 is not replayed.
8. Replay the identical source event; assert **no** new successful delivery to destination 1.
9. Inject repeated serious errors past `SAFETY_PAUSE_THRESHOLD`; assert the rule auto-pauses with a
   durable reason.
10. Assert every one of the above appears on the Rule Detail view as forwarding events.

## 5. Tooling and gates

`pytest` + `pytest-asyncio` + `testcontainers` (real Postgres and Redis, mocked Telegram) for the
backend; `vitest` + React Testing Library for the frontend; `ruff` (lint + format), `mypy --strict`, and
`tsc --noEmit` for static checks.

CI runs, in order: lint → type check → migrations up **and down** → unit → integration → e2e → build →
`pip-audit` / `npm audit` / Trivy / gitleaks. Coverage floor 80% overall, **100% on
`classify_error`, the idempotency-key builder, and the redaction filter** — the three functions where a
silent bug is most costly.
