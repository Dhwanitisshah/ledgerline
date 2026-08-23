# Phase 4: concurrency

Every guarantee in Phases 1–3 was established against one request at a time. This
phase is what happens when two arrive together.

Both bugs below are **still in the tree**, runnable, behind a setting. A
before/after where the "before" is a paragraph of prose is a claim; one where the
"before" is code you can execute is a measurement. Every number in this section was
produced by [tests/test_concurrency.py](../tests/test_concurrency.py) against a real
Postgres, and two tests exist purely to assert the broken paths are *still broken*
— because a reproduction that quietly stops reproducing is the one thing worse than
no reproduction.

```powershell
# See either bug for yourself
$env:IDEMPOTENCY_CLAIM_STRATEGY="naive" ; $env:NAIVE_RACE_WINDOW_MS="300"
$env:WITHDRAWAL_GUARD="naive"
```

### Race 1 — one idempotency key, fifty simultaneous requests

Phase 3 made a *retry* safe. It said nothing about two requests in flight at once,
because the winner's claim row is invisible to everyone until it commits.

**The broken claim** ([app/idempotency.py](../app/idempotency.py)) is the version most
people write:

1. `SELECT` the key. Absent? Then this is a new charge.
2. …charge the card…
3. `INSERT` the key, `ON CONFLICT (key) DO NOTHING`.

Step 1's answer is a fact about the past by the time step 3 acts on it. Step 3 is
where it turns silent: without `ON CONFLICT`, the second insert would raise a
duplicate-key error and roll its charge back — the primary key would have saved you
*by accident*. `ON CONFLICT DO NOTHING` is the reasonable-looking line that removes
the accident. It was added to stop duplicate-key errors filling the logs. It works.
The logs go quiet. The system now writes two payments against one idempotency key.

**50 concurrent requests, one key, `amount = 250000`:**

| | payments | postings | ledger entries | keys | balance | statuses |
|---|---|---|---|---|---|---|
| **Before** | **50** | 50 | 100 | 1 | **12,500,000** | `201 × 50` |
| **After** | **1** | 1 | 2 | 1 | **250,000** | `201 × 1`, `409 × 49` |

The customer asked to be charged ₹2,500 and was charged ₹1,25,000. One idempotency
key was faithfully written, and prevented nothing.

**The fix** is a non-blocking advisory lock taken *before* the claim:

```sql
SELECT pg_try_advisory_xact_lock(:lock_id)
```

Worth being precise about what this fixes, because it is not correctness. The
Phase 3 `ON CONFLICT` claim was **already correct** under concurrency: Postgres
makes the loser wait on the winner's uncommitted row, and once it commits the loser
sees `completed` and replays. Measured before this phase, 20 concurrent requests
produced exactly 1 payment and 20 × `201`.

What it cost was *liveness*. Every loser sat blocked for the length of a card
authorisation, holding a connection. Fifty retries of one charge become fifty
Postgres backends asleep behind one slow payment processor — and connection
exhaustion arrives long before the correctness ever fails.

`pg_try_advisory_xact_lock` answers immediately instead:

| 8 requests, 1200 ms processor call | latency |
|---|---|
| winner (`201`) | 1237 ms |
| losers (`409`) | median **12.2 ms**, max 15.1 ms |

A duplicate in-flight request now costs a 409 and twelve milliseconds instead of a
connection and a second and a quarter. The client retries and gets the recorded
response.

**Why `409` rather than blocking until the winner finishes and replaying?** Both
are defensible, and Stripe returns 409 here too. Blocking gives the client a nicer
answer — the real response instead of "try again" — at the cost of pinning a
connection for an unbounded time that is controlled by a third party. Refusing
fast keeps the failure in the client's retry loop, where it is cheap and visible,
rather than in the connection pool, where it is expensive and looks like something
else entirely at 3am.

**Why transaction-scoped (`_xact_`)?** It is released by `COMMIT`, by `ROLLBACK`,
and by the backend dying. There is no path, including a hard crash mid-charge, that
leaks one. A session-scoped `pg_advisory_lock` needs an explicit unlock, and every
explicit unlock is a line some error path fails to reach.

**Why hash the key in Python rather than with `hashtext()`?** `hashtext` is an
internal Postgres function with no compatibility promise, and its output has changed
across major versions. A lock id that silently changes during an upgrade is a lock
that stops locking.

### Race 2 — one account, two withdrawals

Phase 4 adds `POST /withdrawals`, the mirror of a charge: it DEBITs the customer and
CREDITs the house account. It is the first operation in Ledgerline that has to
*refuse*, and refusing means checking, and checking is where this bug lives.

```
request A          request B
---------          ---------
reads 1000
                   reads 1000
1000-600 >= 0 OK
                   1000-600 >= 0 OK
writes -600
                   writes -600
COMMIT             COMMIT        -> balance is now -200
```

Neither request did anything wrong. Both read a true balance and applied a correct
rule to it. The balance simply was not true any more by the time either one acted.

**20 concurrent withdrawals of 10,000 against a balance of 100,000** (only 10 are
affordable):

| | honoured | rejected | final balance |
|---|---|---|---|
| **Before** | **20 of 20** | 0 | **−100,000** |
| **After** | 10 of 20 | 10 × `422` | **0** |

The account was overdrawn by exactly the amount it held. Note what is *not* broken:
every posting still balances, the ledger still sums to zero, every invariant from
Phase 1 holds perfectly. The bookkeeping is immaculate. Only the *rule* was broken —
which is precisely why this class of bug survives code review and audit alike.

**The fix** is one statement, in the right place:

```sql
SELECT id FROM accounts WHERE id = :account_id FOR UPDATE
```

taken **before** the balance is read. The order is the whole thing: a lock taken
after the read protects nothing, because the value it is meant to protect has
already been copied into a Python variable. From there to `COMMIT`, no other
withdrawal against that account can read its balance — they queue on this
statement, and each one in turn reads a balance that already includes every
withdrawal ahead of it.

Only the `id` is selected. The row is being used as a mutex, and reading columns
from it would imply the balance lives there. It does not, and never has.

#### Why a constraint cannot do this

The instinct is `CHECK (balance >= 0)`, and it cannot be written. A CHECK sees only
the row being written, and the balance is a `SUM` over *other* rows. A trigger can
compute that sum — and under `READ COMMITTED` both transactions see the same
pre-race snapshot, reach the same wrong answer, and both allow it. The check is not
in the wrong place. The problem is that the check and the write are two moments, and
something has to make them one.

#### Row lock vs `SERIALIZABLE` + retry

| | `SELECT … FOR UPDATE` (chosen) | `SERIALIZABLE` + retry |
|---|---|---|
| Style | Pessimistic — take the lock, then look | Optimistic — act, and be told you conflicted |
| Contention | Per account. Different accounts never contend | Whole-transaction; unrelated work can abort |
| Failure mode | Blocking, and deadlock if lock order is inconsistent | `40001` serialization failure, needs a retry loop |
| The catch | You must *remember* to take it — it is a convention, not an invariant | The database notices for you |

Chosen `FOR UPDATE` because the contention is naturally per-account and the lock
scope matches the problem exactly: two withdrawals on one account genuinely must
serialise, and withdrawals on different accounts genuinely need not. It also fails
predictably. `SERIALIZABLE` is the better answer when the conflict shape is not
known in advance; here it is known precisely.

The honest cost is in that last row. Nothing in the database forces a future
`POST /transfers` to take the lock, and if it forgets, the guard is silently gone
for that path. `FOR UPDATE` buys correctness with a rule people must keep, which is
strictly weaker than the DB-enforced invariants in Phases 1–3.

### Making the concurrency real

A sequential loop dressed as a concurrency test proves nothing, so the chain is
worth stating:

1. `asyncio.gather` schedules all N coroutines before any completes.
2. FastAPI's `get_session` builds a **separate `AsyncSession`** per request.
3. Each session takes a **separate asyncpg connection** — the pool is sized 25 + 35
   overflow in [app/db.py](../app/db.py) precisely so 50 requests do not queue on the
   client and quietly serialise.
4. Each connection is a **separate Postgres backend in a separate transaction**.

`test_the_requests_actually_overlap` asserts this instead of assuming it: 12
requests each holding a 300 ms processor call must finish in well under 3.6 s.

Determinism comes from `NAIVE_RACE_WINDOW_MS`, which widens the check-then-act
window on the naive paths only. It does not create either bug — both windows exist
at zero, since a `SELECT` and a later `INSERT` are two statements with a round trip
between them — it makes them reproduce on every run rather than on unlucky ones.

**One measurement trap worth recording.** The first version of the liveness test
fired all 50 requests and timed each one. The losers appeared to take ~740 ms
against a 300 ms processor call, which looks exactly like lock blocking and is not:
with 50 coroutines on one event loop, a request's wall time includes every slice the
loop spent on the other 49. The test now runs at 8-way concurrency, where the signal
is the lock rather than the scheduler.

### Endpoints added

| Method | Path            | Behaviour                                                        |
| ------ | --------------- | ---------------------------------------------------------------- |
| `POST` | `/withdrawals`  | `{account_id, amount, currency?}` → 201; 422 if it would breach the floor |

### Not in Phase 4 (on purpose)

Withdrawals take no `Idempotency-Key` — a retried withdrawal withdraws twice. Making
them idempotent is a mechanical repeat of Phase 3 against a second endpoint, and
this phase is about the races. The balance floor is global (`BALANCE_FLOOR_MINOR_UNITS`,
default 0), not per-account: no credit limits, no overdraft tiers. No webhooks or
outbox (Phase 5b), no refunds (Phase 6).

## Smoke acceptance criteria

- 50 concurrent same-key charges → exactly one payment, one posting
- Concurrent withdrawals → balance never goes below the floor
- Broken reproductions preserved, runnable, and asserted to still reproduce
- Real concurrency (separate sessions, connections and backends), not a loop
- `pytest` + `ruff` green locally and in CI


---

**← Previous:** [Phase 3: idempotency](phase-3-idempotency.md) · **[All phases](../README.md#the-phases)** · **Next:** [Phase 5a: durability](phase-5a-durability.md) →
