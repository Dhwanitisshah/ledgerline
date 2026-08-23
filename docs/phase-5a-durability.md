# Phase 5a: durability

Phase 2 named a gap in a docstring and left it standing. Phases 3 and 4 did not
touch it:

> If the process dies between the processor returning success and `COMMIT`, the
> card was charged and Ledgerline has no record of it.

This phase closes it. It is the only defect in the project that cannot be fixed by
arranging a transaction more carefully, and understanding *why* is most of the work.

### Why no single transaction can fix this

Through Phase 4 the charge flow was one transaction wrapped around the processor
call:

```
BEGIN
  claim the key
  insert payment, move it to 'processing'
  ── call the processor ──        <- a third party, outside the transaction
  write the posting, move to 'succeeded'
  finalise the key
COMMIT
```

Find the moment the process can die. It is the arrow. The processor has taken the
money and every row that would record it is sitting uncommitted in a transaction a
dying backend is about to roll back. No error handler helps — the handler dies too.
No retry helps — nothing was written to retry *from*.

This is not carelessness in the code. A database transaction is a guarantee about
one database, and the processor is not in it. `COMMIT` cannot be made to mean "and
also the card network agrees", so **no arrangement of one transaction makes those
two facts atomic**. The instinct to reach for a bigger transaction is the instinct
to reach for two-phase commit across a company you do not own.

The only move left is to stop trying, and instead make the attempt **durable before
the money can move**, so a crash leaves evidence.

### The measurement

Both flows are in the tree, selected by `CHARGE_DURABILITY`, in the same idiom as
Phase 4's preserved races. Numbers below are from a real run against Postgres 16 —
one crashed charge for ₹2,500.00, then the customer retrying with the same
idempotency key.

```powershell
# Run the old flow yourself
$env:CHARGE_DURABILITY = "single_txn"
```

**`single_txn` — the preserved "before":**

| Step | Card charged | payments | keys | Ledger balance |
| ---- | ------------ | -------- | ---- | -------------- |
| crash after the processor said yes | **250,000** | 0 | 0 | 0 |
| customer retries with the same key → **201** | **500,000** | 1 | 1 | 250,000 |

Read the second row carefully, because it is worse than the docstring predicted.
The retry was not protected by the idempotency key — the key was in the transaction
that rolled back, so it never existed, so Phase 3's entire mechanism was absent
exactly when it was needed. **The card was charged 500,000 and the ledger says
250,000.** Nothing errored, no constraint fired, the ledger still sums to zero, and
every Phase 1–4 test still passes. The system is perfectly consistent and perfectly
wrong.

**`durable_intent` — the shipped path:**

| Step | Card charged | payments | keys | Ledger balance |
| ---- | ------------ | -------- | ---- | -------------- |
| crash after the processor said yes | 250,000 | 1 (`processing`) | 1 (`in_progress`) | 0 |
| customer retries with the same key → **409** | 250,000 | 1 | 1 | 0 |
| `python -m app.reconcile --once` | 250,000 | 1 (`succeeded`) | 1 (`completed`) | **250,000** |
| customer retries again → **201**, replayed | 250,000 | 1 | 1 | 250,000 |

Charged once, recorded once, and the customer gets their answer.

### Two transactions, and a gap on purpose

```
BEGIN                                    -- A: the intent
  claim the key
  look up the account, check the currency
  insert payment, move it to 'processing'
  bind the key to the payment
COMMIT                                   <- the attempt is now durable

── call the processor ──                 <- NO transaction open, NO locks held

BEGIN                                    -- B: the settlement
  success: posting + 'succeeded'   |   failure: 'failed'
  finalise the key with the response to replay
COMMIT
```

A crash at the arrow now leaves a committed payment in `processing` — a durable
statement that Ledgerline *asked* for this charge and does not know how it ended.
Transaction B is still all-or-nothing, unchanged from Phase 2.

Note what `processing` now means. Before this phase it meant "a charge is running".
Now it means "a charge is running **or** its request died", and the row cannot tell
you which. That ambiguity is not a flaw in the design, it is the honest content of
what we know, and it is why the sweep resolves it by asking rather than by inferring
from a timestamp.

### The processor has to keep books, or none of this works

The reconciler asks the processor what happened. For that question to have an
answer, `FakeProcessor` stops being amnesiac: it writes every charge to
`processor_charges`, keyed by the attempt reference.

Two properties of *how* it writes carry the whole phase, and both are in
[app/processor.py](../app/processor.py):

1. **Its own session, its own transaction, its own commit.** Not the request's.
   When Ledgerline's transaction rolls back, the processor's row stays. That
   asymmetry is not a quirk of the fake — it is the actual shape of the problem.
   The card issuer does not roll back because your web process segfaulted. If the
   fake wrote on the caller's session, the rollback would take the evidence with it
   and the reproduction would silently stop reproducing;
   `test_the_processor_books_survive_the_charge_rolling_back` pins exactly that.
2. **Idempotent on the attempt reference.** Charging the same reference twice
   returns the first outcome instead of taking the money again — which is what a
   real processor does with an idempotency key, and is why the attempt reference is
   the payment id rather than a fresh UUID per call.

`processor_charges` is **not Ledgerline's data**. It has no foreign keys into the
money model, nothing joins against it, and the only sanctioned reader is
`ProcessorAdapter.lookup`. It lives in this database because the project has one
database, not because it belongs to this service.

### The sweep

[app/reconcile.py](../app/reconcile.py) finds payments in `processing` older than
`RECONCILE_STUCK_AFTER_SECONDS` and settles each one against the processor's
records. **The processor is the authority** — Ledgerline does not guess, and does
not decide a payment failed because the row looks old:

| The processor says         | What it means             | Settled as                       |
| -------------------------- | ------------------------- | -------------------------------- |
| success                    | the card was charged      | `succeeded`, posting written now |
| failure                    | the card declined         | `failed`                         |
| nothing — no such attempt  | the call never reached it | `failed`                         |

The third row is safe for a specific reason: if the processor has no record, no
money moved, so recording a failure cannot be wrong about anyone's balance.

The first row is where the phase pays for itself. The card was charged, the request
that charged it never came back, and the posting is constructed minutes later from
the processor's own record — the same balanced two-legged posting the charge route
would have written, marked `(reconciled)` in its description rather than passed off
as ordinary.

One payment, one transaction, one commit. `SELECT … FOR UPDATE SKIP LOCKED` is what
makes several reconcilers safe to run at once: they divide the backlog instead of
contending over it, and a payment another worker holds is skipped rather than waited
for.

It runs as a **separate process**, deliberately:

```powershell
python -m app.reconcile              # loop
python -m app.reconcile --once       # single pass, for CI and smoke
```

Recovery that lives inside the process being recovered from is unavailable exactly
when it is needed. If the web process is dead or crash-looping, the payments it
abandoned are the ones waiting.

### What it costs: the key is now consumable

The split inverts something Phase 3 was proud of. Then, a request that died left no
trace, so the key was never consumed and a retry was free. Now a crash **after**
transaction A does consume the key, and retries get a 409 until it is resolved.

Left alone that would be a 24-hour lockout caused entirely by our own crash, so the
sweep finalises the key alongside the payment, in the same transaction — which is
what the `idempotency_keys.payment_id` column added in migration 0005 is for. The
customer's next retry replays the reconciled outcome, exactly as the original
request would have answered had it lived.

Requests that fail *before* transaction A commits — unknown account, currency
mismatch, malformed body — still roll their claim back and leave the key free.
Those never reach the processor.

### A second thing the split bought

The processor call no longer happens inside an open write transaction. Phase 4
measured the cost of that arrangement and fixed the *symptom* with a non-blocking
advisory lock; Phase 5a removes the cause. Measured with `pg_stat_activity` while an
800 ms charge runs: under `single_txn` a backend sits in `idle in transaction` for
most of the call, and under `durable_intent` for none of it. The advisory lock is
still there — its job shrank to covering the window *inside* transaction A, where
the claim exists but is not yet visible.

### The guarantee, stated precisely

**What you get:** no charge accepted by the processor stays invisible. If the
processor took the money, the sweep finds the payment, reads that fact from the
processor's records, and writes the posting. **Eventually, not instantly** — and
"at-least-once resolution, exactly-once effect": running the sweep twice, ten times,
or concurrently with the original request finishing settles the payment once. Three
independent things enforce that — the row lock, the `status = 'processing'`
predicate inside it, and the state machine refusing a second move out of a terminal
state.

**What you do not get:**

- **A closed window, only a bounded one.** A payment can be unresolved for up to
  `RECONCILE_STUCK_AFTER_SECONDS` plus one sweep interval. During it the truth
  exists and is discoverable; it has simply not been discovered.
- **Recovery without a recoverer.** Durability buys the *ability* to recover.
  Something still has to run the sweep.
- **Independence from the processor.** All of it rests on being able to ask "what
  happened to attempt X?". Against a processor with no idempotency-keyed lookup,
  this module cannot be written and the gap can only be narrowed, never closed. The
  guarantee is jointly held, and that is worth saying out loud rather than
  presenting reconciliation as something a service does by itself.
- **Protection from a processor that is wrong.** Reconciling against a corrupted
  source of truth reproduces the corruption faithfully.

### Endpoints and commands added

| Command                                | Behaviour                                          |
| -------------------------------------- | -------------------------------------------------- |
| `python -m app.reconcile`              | sweep on an interval until interrupted              |
| `python -m app.reconcile --once`       | one pass and exit                                   |

No new HTTP endpoints. `POST /charges` gains one test knob,
`force_crash_after_processor`, which abandons the request at the fatal instant.

### Not in Phase 5a (on purpose)

The transactional outbox, `event_deliveries`, and the webhook receiver are [**Phase
5b**](phase-5b-reliability.md) — they answer a different question (how to tell a downstream consumer
that a payment happened, without a dual write) and they deserved their own commit
rather than being bolted onto this one.

Also absent: no sweep for *expired* idempotency keys (they are still reclaimed
lazily), no alerting on payments the sweep repeatedly fails to settle, no retry of
the processor call itself, and no refunds (Phase 6).

## Smoke acceptance criteria

- Crash after the processor says yes → the payment survives as `processing`
- The sweep settles it against the processor: `succeeded` + posting, or `failed`
- A processor with no record of the attempt → `failed`, and no money moved
- The sweep is idempotent: running it twice, or twice at once, settles once
- The abandoned idempotency key is finalised, so the retry replays instead of 409ing
- The preserved `single_txn` flow still loses a charged card, and is asserted to
- `pytest` + `ruff` green locally and in CI


---

**← Previous:** [Phase 4: concurrency](phase-4-concurrency.md) · **[All phases](../README.md#the-phases)** · **Next:** [Phase 5b: reliability](phase-5b-reliability.md) →
