# Implementation Plan

**Status:** Implemented. All slices complete — see the final report and OPERATIONS.md §8.
**Last updated:** 2026-08-22

---

## Approach

Small vertical slices. The project stays runnable and green after every slice. Each slice ends with the
required final report (what changed, files, migrations, tests run with results, env vars, Telegram
decisions, security/reliability notes, known limitations, next step).

## Sequence

| # | Slice | Deliverable | Done when |
|---|---|---|---|
| **0** | Repository & runtime inspection | Complete — see [ARCHITECTURE.md](ARCHITECTURE.md) §1 | ✅ Done |
| **1** | Scaffold & toolchain | `git init`, Compose stack, `.env.example`, ruff/mypy/pytest/vitest/tsc, `Makefile`, CI skeleton | `make up`, `make test`, all checks pass on an empty suite |
| **2** | Schema & migrations | All tables from [DATABASE.md](DATABASE.md), Alembic up **and down**, invariant constraints | Migrations round-trip; invariant tests pass |
| **3** | AuthN/AuthZ | Register, login, logout, `/me`, server-side sessions, `user_id`-scoped repository layer | Cross-user isolation test passes for every existing route |
| **4** | Adapter interface + `MockAdapter` | `TelegramAdapter` protocol, domain types, `classify_error`, full mock | 100% coverage on `classify_error`; no Telethon/aiogram import outside `adapters/` |
| **5** | Secure connection flow | Bot token + MTProto phone/code/2FA, envelope encryption, redaction filter, disconnect/revoke | Encryption round-trip, redaction, and state-transition tests pass |
| **6** | Chat sync & eligibility | Sync job, `(peer_type, peer_id)` keying, per-connection `access_hash`, eligibility reason codes | Sync creates/deactivates chats; eligibility flips recorded |
| **7** | Rule CRUD & validation | Rules, sources, destinations, filters, preview string, all validation rules | Every rejection case in [API.md](API.md) §5 has a passing test |
| **8** | Event intake | MTProto updates with `catch_up`, bot long polling, durable cursors, Redis single-owner lock, album buffering | Replayed update creates zero new jobs; restart resumes from cursor |
| **9** | Queue & workers | Durable jobs, leases, bounded + per-connection concurrency, rule delay, token buckets, graceful shutdown | Three destinations produce three independent durable results |
| **10** | Reliability | Idempotency, retry classification, flood-wait obedience, ambiguous-timeout policy, pause/resume, lease reclaim, safety pause | Restart drill ([OPERATIONS.md](OPERATIONS.md) §6) passes; flood-wait duration test passes |
| **11** | Dashboard | Login, Home, Connections, Chats, Rules, Rule Detail, Settings | Loading/empty/error/permission-denied states on every page |
| **12** | Activity & errors | Event list, counts by outcome, safe error display, retry-failed action | Rule Detail shows every event from the e2e scenario |
| **13** | Security hardening | Rate limits, CSRF, CSP + headers, audit events, export/delete, idempotency replay | Response-model secret scan and audit coverage tests pass |
| **14** | E2E & operational docs | Full mocked e2e ([TESTING.md](TESTING.md) §4), OPERATIONS finalized, `openapi.yaml` committed | E2E green; docs match shipped behaviour |
| **15** | Acceptance review | Walk [OPERATIONS.md](OPERATIONS.md) §8 line by line with evidence | Every box ticked with a linked test or transcript |

Slices 1–4 have no Telegram I/O at all, so the riskiest surface is exercised only after the adapter
boundary and its mock exist.

## Ground rules

- **Never** report a feature complete because the UI renders. Prove backend behaviour, persistence,
  authorization, and failure handling with tests.
- Run the relevant tests and checks at every stage; report real results, including failures.
- No production code path may import Telethon or aiogram outside `adapters/`.
- No feature from the [PRODUCT_SPEC.md](PRODUCT_SPEC.md) §2 exclusion list gets added, even
  incidentally.
- Anything in the §3 safety boundary is refused and re-raised rather than quietly implemented.

## Proposed layout

```
insightadsflow/
├── docs/                       # this specification set
├── docker-compose.yml
├── Makefile
├── .env.example
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI
│   │   ├── listener.py         # intake process
│   │   ├── scheduler.py        # cron/lease reclaim
│   │   ├── worker.py           # claim/lease/deliver
│   │   ├── adapters/           # ONLY place Telethon/aiogram may be imported
│   │   │   ├── base.py  bot.py  user.py  mock.py  errors.py
│   │   ├── api/v1/             # routers
│   │   ├── domain/             # entities, filters, idempotency, preview
│   │   ├── repositories/       # all methods take user_id
│   │   ├── security/           # crypto, redaction, sessions, rate limits
│   │   └── db/                 # models, migrations
│   └── tests/  unit/ integration/ e2e/
└── frontend/
    └── src/  pages/ components/ api/ hooks/
```

## Assumptions

Stated for the record; each is cheap to revise now and expensive to revise after slice 8.

1. Single tenant per user (one workspace), with isolation built as if multi-tenant.
2. Email + password auth; no OAuth or SSO in MVP.
3. English-only UI in MVP; strings kept in a single module so i18n stays possible.
4. The operator supplies their own `TELEGRAM_API_ID`/`API_HASH` from my.telegram.org.
5. MVP does **not** propagate edits or deletions of already-forwarded source messages.
6. MVP does **not** forward message history — only messages arriving after a rule is activated.
7. Notifications are in-dashboard only; email/Telegram alerts are V1.
8. Media is forwarded **by reference**; nothing is downloaded or re-hosted.

## Open questions

None are blocking — I have a defensible default for each and will proceed on it unless told otherwise.

1. **Bot vs account guidance.** Should the UI actively steer customers toward a bot connection when
   their sources allow it? *Default: yes, with an advisory note, because it materially lowers their risk.*
2. **Event retention.** 90 days assumed. *Default: 90, configurable.*
3. **Edit propagation** (assumption 5). Genuinely a product call. *Default: out of MVP, flagged in ADR
   open tradeoffs.*
4. **Backfill on activation.** Some customers expect the last N messages to forward when a rule turns
   on. *Default: no backfill — forward only new messages, which is the safer and less surprising
   behaviour.*
