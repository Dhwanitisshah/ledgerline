# Ledgerline

**A payments backend where every phase closes one specific failure the previous
phase left standing — and every fix is a runnable demonstration, not a paragraph.**

[![CI](https://github.com/Dhwanitisshah/ledgerline/actions/workflows/ci.yml/badge.svg)](https://github.com/Dhwanitisshah/ledgerline/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB.svg)](https://www.python.org/)
[![Postgres 16](https://img.shields.io/badge/postgres-16-336791.svg)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-210%20against%20real%20Postgres-brightgreen.svg)](docs/testing.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

The broken implementations are still in the tree, behind flags, with tests asserting
they are *still broken*, because a fix nobody can watch fail is a fix nobody can
check.

**The short version of what it does:** money is integer minor units everywhere,
balances are never stored (always a SQL `SUM`), the ledger is append-only enforced
by Postgres triggers, a retried charge happens once, a crashed charge is recovered
against the processor's own books, downstream systems are told if and only if the
money moved, and a refund is a reversing posting rather than an edit.

**Status: all seven phases complete.** Start with
[the two stories](#the-two-stories-this-project-is-really-about) below, or jump to
[the phases](#the-phases).

## Architecture

```
                       Idempotency-Key: <uuid>
   client ───────────────────────┐
                                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  FastAPI                                                    │
   │  ┌───────────────────────────────────────────────────────┐  │
   │  │  LedgerlineMiddleware  (one pure-ASGI layer)          │  │
   │  │  request id · rate limit (2 tiers) · metrics · 500s   │  │
   │  └───────────────────────────────────────────────────────┘  │
   │                                                             │
   │  POST /charges          POST /charges/{id}/refund           │
   │  POST /withdrawals      POST /webhooks                      │
   │  GET  /health  /ready  /metrics  /docs                      │
   └───────────┬─────────────────────────────────┬───────────────┘
               │                                 │
               │  txn A: durable intent          │  NO txn open
               │  txn B: settle + outbox         │  across this call
               ▼                                 ▼
   ┌───────────────────────────┐     ┌──────────────────────────┐
   │  Postgres 16              │     │  FakeProcessor           │
   │                           │     │                          │
   │  accounts    (no balance) │     │  writes on its OWN       │
   │  ledger_transactions      │     │  session + txn, so it    │
   │  ledger_entries  (append- │     │  SURVIVES our rollback   │
   │                   only)   │     │                          │
   │  payments · refunds       │◀────│  processor_charges       │
   │  idempotency_keys         │     │  processor_refunds       │
   │  outbox_events            │     └──────────────────────────┘
   │  webhook_events           │      "the card issuer does not
   │  event_deliveries         │       roll back because your
   └───────────┬───────────────┘       web process segfaulted"
               │
     ┌─────────┴──────────┬─────────────────────┐
     ▼                    ▼                     ▼
  publisher           reconciler              drift
  drains the outbox   settles payments        compares our ledger
  deliver-then-mark   stranded in             against the processor
  at-least-once,      'processing' against    and REPORTS —
  exactly-once        the processor's books   never repairs
  effect
```

Three background processes, **one image**, so a worker can never run different code
than the API that writes the rows it consumes.
## The two stories this project is really about

### 1. The bug that wasn't there — Phase 4

The assumed bug was a double-charge under concurrent same-key requests. It did not
exist: Phase 3's `INSERT ... ON CONFLICT` claim was already correct, because
Postgres makes the loser wait on the winner's uncommitted row. **The real defect
was liveness**, and it was invisible until measured.

| 8 concurrent requests, one key, 1200 ms processor call | latency |
| --- | --- |
| winner (`201`) | 1237 ms |
| losers (`409`) | median **12.2 ms**, max 15.1 ms |

Before the fix the losers *blocked* for the full authorisation, each holding a
Postgres connection. Fifty retries of one charge become fifty backends asleep behind
one slow third party — and connection exhaustion arrives long before correctness
ever fails. [Full write-up →](docs/phase-4-concurrency.md)

The *preserved naive* path still double-charges, and there is a test that proves it:
50 concurrent requests → **50 payments, ₹1,25,000 charged for a ₹2,500 purchase**.

### 2. The gap no transaction can close — Phase 5a

If the process dies between the processor returning success and `COMMIT`, the card
was charged and the service has no record. **No arrangement of one database
transaction fixes this**, because `COMMIT` is a guarantee about one database and the
card network is not in it.

| | card charged | payments | keys | ledger balance |
| --- | --- | --- | --- | --- |
| **`single_txn`** — crash after the processor said yes | 250,000 | 0 | 0 | 0 |
| … customer retries with the same key → `201` | **500,000** | 1 | 1 | 250,000 |
| **`durable_intent`** — same crash | 250,000 | 1 (`processing`) | 1 | 0 |
| … retry → `409`, then `python -m app.reconcile --once` | 250,000 | 1 (`succeeded`) | 1 | **250,000** |

Read the second row twice. The retry was *not* protected by the idempotency key —
the key was in the transaction that rolled back, so Phase 3's entire mechanism was
absent exactly when it was needed. The card was charged twice and the ledger says
once. Nothing errored, no constraint fired, and every earlier test still passed.

The fix is not atomicity but **durability plus reconciliation**: commit the intent
before the money can move, then settle whatever is stranded against the processor's
own records. [Full write-up →](docs/phase-5a-durability.md)

## The phases

Each phase is a self-contained write-up: the failure it found, the fix, the test
that proves it, and an explicit list of what it deliberately left undone.

| Phase | What it closes | Read |
| --- | --- | --- |
| **1** | Money model — a double-entry ledger, append-only, balances derived | [The money model](docs/phase-1-ledger.md) |
| **2** | Payment lifecycle — a state machine, a fake processor, an atomic charge | [The payment lifecycle](docs/phase-2-payment-lifecycle.md) |
| **3** | Idempotency — a retried charge happens once | [Idempotency](docs/phase-3-idempotency.md) |
| **4** | Concurrency — two races, reproduced then fixed | [Concurrency](docs/phase-4-concurrency.md) |
| **5a** | Durability — a charge that outlives the process that made it | [Durability](docs/phase-5a-durability.md) |
| **5b** | Reliability — a transactional outbox and idempotent webhooks | [Reliability](docs/phase-5b-reliability.md) |
| **6** | Refunds & reconciliation — reversing postings, drift detection | [Refunds & reconciliation](docs/phase-6-refunds-reconciliation.md) |
| **7** | Hardening, observability & deploy — safe to expose, legible from outside | [Hardening & deploy](docs/phase-7-hardening-deploy.md) |

## Load test

`scripts/loadtest.py` — 300 iterations of **charge → replay → refund** at
concurrency 20, no new dependencies (async `httpx` + `statistics`):

```
throughput   21.8 flows/s  (65.4 req/s)

charge   n=300  p50= 387.4ms  p95= 489.2ms  p99= 588.2ms
replay   n=300  p50= 115.4ms  p95= 152.6ms  p99= 197.5ms
refund   n=300  p50= 350.0ms  p95= 921.0ms  p99=1180.2ms
```

**The replay is 3.4× faster than the charge**, which is the number worth looking at:
it shows the idempotent retry genuinely does less work rather than repeating it. A
charge runs two transactions, a processor call, a balanced posting and an outbox
write; a replay is one indexed lookup and a stored response served back.

Reproduce it:

```powershell
$env:RATE_LIMIT_ENABLED="false"; uvicorn app.main:app     # terminal 1
python scripts\loadtest.py --iterations 300 --concurrency 20
```

Honest caveats: this is one laptop talking to a Postgres container on the same
laptop, with the fake processor at zero latency. It is a floor for the
application's own overhead, not a capacity estimate — a real processor adds a
network round trip to every charge. The numbers are for *relative* comparison.

## How to break it

Every "before" in this README is executable. The deliberately broken
implementations are still in the tree behind flags, and two tests exist purely to
assert they are **still broken** — because a reproduction that quietly stops
reproducing is worse than none.

```powershell
# Double-charge under concurrency (Phase 4). 50 requests, one key -> 50 payments.
$env:IDEMPOTENCY_CLAIM_STRATEGY="naive" ; $env:NAIVE_RACE_WINDOW_MS="300"
pytest tests/test_concurrency.py -k naive -s

# Overdraw an account (Phase 4). 20 concurrent withdrawals, 10 affordable -> all 20 honoured.
$env:WITHDRAWAL_GUARD="naive"
pytest tests/test_concurrency.py -k overdraw -s

# Lose a charged card (Phase 5a). The card is charged; zero payments exist.
$env:CHARGE_DURABILITY="single_txn"
pytest tests/test_durability.py -k single_txn -s

# Over-refund from psql, with the application bypassed entirely (Phase 6).
docker compose exec postgres psql -U postgres -d ledgerline -c "INSERT INTO refunds (payment_id, amount, currency, status, ledger_transaction_id) VALUES ('<paid-in-full-id>', 100000, 'INR', 'succeeded', '<posting-id>');"
# ERROR: ledgerline: refunds for payment ... would exceed the 250000 charged

# Edit history (Phase 1). The ledger refuses.
docker compose exec postgres psql -U postgres -d ledgerline -c "UPDATE ledger_entries SET amount = 1;"
# ERROR: ledgerline: UPDATE on ledger_entries is not permitted -- the ledger is append-only
```

The broken paths **cannot be deployed**: `APP_ENV=production` makes `Settings`
refuse to construct if any of them is selected, so a misconfigured deployment fails
at startup rather than coming up healthy and being wrong under load.

## The guarantees, precisely

| # | Guarantee | Enforced by |
| --- | --- | --- |
| 1 | Money is an integer in minor units, everywhere | `BIGINT` columns, `StrictInt` at the boundary |
| 2 | A balance is never stored | No balance column exists; `SUM` over `ledger_entries` |
| 3 | The ledger is append-only | Postgres `FOR EACH STATEMENT` triggers |
| 4 | Every posting sums to zero and is single-currency | Checked in-txn against rows read back, before `COMMIT` |
| 5 | A failed charge moves **no** money | No ledger row is written until the processor says yes |
| 6 | Exactly the payments that moved money have a posting | `CHECK ((status IN ('succeeded','refunded')) = (ledger_transaction_id IS NOT NULL))` |
| 7 | A retried charge happens once | `INSERT ... ON CONFLICT` claim + byte-identical replay |
| 8 | A duplicate in flight is refused, not parked | `pg_try_advisory_xact_lock` (non-blocking) |
| 9 | Concurrent withdrawals cannot overdraw | `SELECT ... FOR UPDATE` **before** the balance is read |
| 10 | A charge the processor accepted is never silently lost | Durable intent + reconciliation against its books |
| 11 | Downstream is told iff the money moved | Outbox row in the same transaction as the postings |
| 12 | At-least-once delivery, **exactly-once effect** | The consumer's primary key, not the publisher |
| 13 | A duplicate webhook is a no-op | `ON CONFLICT` on the provider's event id, in the settlement txn |
| 14 | Refunds never exceed the charge | A trigger that takes the payment's row lock **itself** |
| 15 | Disagreement with the processor is discoverable | `python -m app.drift --once` — reports, never repairs |

**What is deliberately absent:** authentication (the largest single gap, stated
plainly), webhook signature verification, a dead-letter queue, distributed rate
limiting, and tested backups. Each is named in its phase's "what you do not get".
## Stack

- Python 3.13 — the local venv and the CI runner are pinned to the same version,
  in `.github/workflows/ci.yml` and ruff's `target-version` in `pyproject.toml`
- FastAPI + uvicorn
- Postgres 16 (Docker Compose)
- SQLAlchemy 2.0 async (asyncpg)
- Alembic (async-configured)
- pytest + pytest-asyncio + httpx
- ruff
- pydantic-settings (config from `.env`)


## Getting started

| | |
| --- | --- |
| **Run it locally** | [docs/running-locally.md](docs/running-locally.md) — Postgres, migrations, the API, and all three background processes |
| **Run the tests** | [docs/testing.md](docs/testing.md) — why they need a real Postgres, and what the `race` marker means |
| **Break it on purpose** | [How to break it](#how-to-break-it) above — every preserved bug, with the command that triggers it |
| **Deploy it** | [Phase 7](docs/phase-7-hardening-deploy.md#choosing-the-platform-honestly) — the platform comparison and the deploy steps |

## Notes

- `/health` is dependency-free (no DB call) so it works without Postgres running.
- `.env` is gitignored; copy `.env.example` to `.env` to configure locally.
- Compose publishes Postgres on **5433** locally; CI uses **5432**. The difference
  lives in `DATABASE_URL` and nowhere else.
