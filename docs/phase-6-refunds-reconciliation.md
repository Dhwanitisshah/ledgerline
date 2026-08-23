# Phase 6: refunds and reconciliation

Phase 6 is where every earlier phase gets used at once, and the most useful thing
it says is how little new machinery it needed. A refund is:

- Phase 1's ledger invariants, via `write_posting` — a balanced two-legged posting
  with the charge's legs reversed;
- Phase 2's state machine, via `transition` — `succeeded → refunded`;
- Phase 3's idempotency, via `claim_key` / `finalize_key` — unchanged, different
  fingerprint;
- Phase 4's row lock, via `lock_payment_for_refund` — because an over-refund *is*
  the overdraw race;
- Phase 5b's outbox, via `record_payment_refunded` — beside the posting, one commit.

If any of those had been built as a special case for charges rather than as a
mechanism, [app/routers/refunds.py](../app/routers/refunds.py) is where it would have
shown. It did not.

### `refunded` finally becomes reachable

Phase 2 put `refunded` in the enum and left nothing transitioning into it, so the
Postgres type would never need widening. **That bet paid off**: the refund flow
landed without touching the enum. It paid off *because* of the next decision.

### Partial refunds are an amount, not a state

**A partial refund leaves the payment `succeeded`.** Only the refund that returns
the last of the charge moves it to `refunded`.

```
charge 2500.00        payment: succeeded
  refund   500.00     payment: succeeded    (still partly live)
  refund 1000.00      payment: succeeded
  refund 1000.00      payment: refunded     <- the total now equals the charge
```

The alternative — a `partially_refunded` status — would encode an **amount** in a
state machine, and the amount already has a home. So there is no
`payments.refunded_amount` column and nowhere to put one:

```sql
SELECT COALESCE(SUM(amount), 0)::bigint
FROM refunds
WHERE payment_id = :payment_id AND status = 'succeeded'
```

This is the same call as having no `balance` column, made a third time. A stored
total is a cached number that has to be kept in step with the rows that justify it,
and the two drift the moment anything goes wrong.

The practical effect: a partial refund does not call `transition` at all. It writes
a refund row and a posting and leaves the status exactly as it found it.

### A refund reverses. It does not edit.

The charge's posting is never touched — it *cannot* be, the append-only triggers
from migration 0002 reject `UPDATE` and `DELETE`. A refund writes a **new** posting
with the legs the other way round:

```
charge:  DEBIT   house:card_settlement:INR   250000
         CREDIT  customer                    250000

refund:  DEBIT   customer                    100000
         CREDIT  house:card_settlement:INR   100000
```

So a full refund returns the balance to its pre-charge value **by arithmetic**.
Nothing sets it back to zero; the SUM changes because there is a second posting to
add up. Money integrity is untouched: every posting still balances, the ledger
still sums to zero, and the audit trail says what happened in the order it
happened.

Corrections are made by posting, not by rewriting — which is what the Phase 1
section promised, delivered five phases later without amending the promise.

**One deliberate asymmetry:** a refund is **not** subject to the balance floor. A
withdrawal refuses to take an account below zero; a refund will, if the customer
already withdrew the money. A withdrawal is a request Ledgerline may decline; a
refund is a reversal of something that already happened, and the money is going
back to the card either way. Refusing would not keep it — it would only mean the
processor reversed a charge Ledgerline still shows as fully live. A negative
balance there is an accurate statement that the customer owes money.

### The over-refund invariant, in the database

> **The succeeded refunds for one payment never total more than it was charged.**

Enforced in three places, which is not belt-and-braces — they fail differently:

| Where | What it buys | What defeats it |
| ----- | ------------ | --------------- |
| `app/refunds.py`, inside the txn | a clean 422 saying how much room is left | a caller not using the API |
| `lock_payment_for_refund`, before the read | two concurrent refunds cannot both see a total that omits the other | a future endpoint that forgets it |
| **the migration 0007 trigger** | holds against psql, a data-fix script, anything | nothing |

A `CHECK` constraint cannot express this, for exactly the reason the Phase 4
section gives for `CHECK (balance >= 0)`: a CHECK sees only the row being written,
and this rule is a SUM over *other* rows. So it is a trigger.

But a trigger that merely sums is **also** not enough, and this is the subtle part.
Under `READ COMMITTED` two concurrent inserts each read a sum that omits the other,
both conclude there is room, and both commit — the Phase 4 overdraw race,
reproduced inside a trigger that looks like it prevents exactly that. So the
trigger takes the lock itself:

```sql
SELECT amount INTO charged FROM payments WHERE id = NEW.payment_id FOR UPDATE;
SELECT COALESCE(SUM(amount), 0) INTO already FROM refunds
  WHERE payment_id = NEW.payment_id AND status = 'succeeded' AND id <> NEW.id;
IF already + NEW.amount > charged THEN RAISE EXCEPTION ...
```

Try it with the application bypassed entirely:

```powershell
docker compose exec postgres psql -U postgres -d ledgerline -c "INSERT INTO refunds (payment_id, amount, currency, status, ledger_transaction_id) VALUES ('<paid-in-full-id>', 100000, 'INR', 'succeeded', '<some-posting>');"
# ERROR: ledgerline: refunds for payment ... would total 350000 minor units, which exceeds the 250000 charged
```

`test_an_over_refund_is_unstorable_even_from_raw_sql` is that, in the suite.

### The constraint Phase 2 knew it would have to widen

Migration 0003 wrote this, with a comment saying Phase 6 would have to revisit it:

```sql
CHECK ((status = 'succeeded') = (ledger_transaction_id IS NOT NULL))
```

A refunded payment **keeps the posting from its original charge**, so that
constraint made `refunded` literally unstorable. Migration 0007 replaces it:

```sql
CHECK ((status IN ('succeeded', 'refunded')) = (ledger_transaction_id IS NOT NULL))
```

Read it as: *exactly the payments that moved money have a posting.* It got wider by
one status and not one inch more — a `failed` payment still may not point at one,
and there is a test asserting that.

### Refund idempotency: the third use of Phase 3

`POST /charges/{id}/refund` **requires** an `Idempotency-Key`, with the same
semantics as a charge: same key + same body replays byte for byte, same key +
different amount is a 422, no key is a 400, and a rejected refund leaves its key
free so the caller can correct the amount and retry.

The only thing that differs is the fingerprint — `(payment_id, amount)` rather than
`(account_id, amount, currency)` — so `refund_fingerprint` sits beside
`request_fingerprint` and both delegate to one canonical hash.

**Why refunds do not need Phase 5a's two-transaction split.** The processor call
happens *inside* the transaction here, which looks like a regression until you see
where the attempt reference comes from:

```python
refund_attempt_ref(payment_id, idempotency_key)  # uuid5 — derived, not generated
```

A charge needed the split because its attempt reference existed only in a Python
variable until the payment row committed, so a crash made the attempt
unaskable-about. **A refund's reference is recomputable from the retry itself.** The
client resends the same key, the same uuid falls out, the processor recognises a
reversal it has already made, and returns the original outcome instead of sending
the money twice. The reference survives a crash without ever having been stored.

The honest costs, both real and both documented in the route: the processor call
holds a transaction and a row lock open (a smaller version of the `idle in
transaction` problem Phase 5a removed from the charge path), and between the
processor reversing and this transaction committing the two sides disagree. Nothing
closes that window. The next section is what *notices* it.

### Reconciliation across time: detect, do not repair

[app/drift.py](../app/drift.py) asks the question the Phase 5a sweep does not:

> For everything we consider **settled**, do the two sides still tell the same
> story?

Nothing is stuck, nothing timed out, no request failed. Both sides look finished
and simply disagree, and the only way to find out is to go and compare.

| Finding | Meaning |
| ------- | ------- |
| `charge_missing_at_processor` | we say succeeded; their books have no such charge |
| `charge_outcome_mismatch` | we posted money for a charge they say was declined |
| `refund_missing_at_processor` | we reversed money on our books that they never reversed |
| `refund_missing_locally` | **they reversed money we never recorded** |
| `refund_amount_mismatch` | both have the reversal, for different amounts |
| `refund_total_exceeds_charge` | our own refunds sum past the charge |
| `ledger_disagrees_with_records` | the postings do not net to charge − refunds |

The last two are different in kind, and that is why they are there. Every other row
is a disagreement with a third party, which can happen without anyone being at
fault. Those two are **Ledgerline disagreeing with itself** — the over-refund
trigger was bypassed, or a posting went missing — and they should be impossible. A
job that only checked the other side would never notice its own invariants failing.

`refund_missing_locally` is the crash-shaped one, and it is worse than an accounting
nicety: our refunded total would be *understating* itself, so a further refund would
be allowed that should not fit.

**It writes nothing.** Every other background job in this project moves money — the
sweep settles payments and writes postings, the publisher marks events delivered.
This one reads both sides, prints what disagrees, and stops. That is a decision, not
an unfinished feature:

- **A discrepancy is evidence, not a diagnosis.** "The processor has a refund we do
  not" is equally consistent with a crash between two commits, a bug that refunded
  the wrong payment, a replayed request, and someone reversing a charge by hand in a
  dashboard. The right repair differs in each case and depends on facts in neither
  database.
- **An auto-fixer is a money-moving robot triggered by disagreement.** It fires
  precisely when one of its two inputs is known to be wrong, which is how a small
  discrepancy becomes a large one.
- **The safe repair already exists elsewhere.** The mechanical case — a payment
  stranded in `processing` — has a job that resolves it, because there the
  processor's answer is unambiguous and the action uniquely determined. What is left
  here needs a person.

`test_the_job_repairs_nothing` asserts exactly that: run it against real drift, and
afterwards every table holds what it held before, including the drift.

A **grace period** (`DRIFT_GRACE_SECONDS`, default 60) keeps normal in-flight work
out of the report. Our books and the processor's are written on different
connections with no ordering between them, so a moment of disagreement is expected;
reporting it would bury the real findings under noise, which is how a report becomes
something nobody reads.

```powershell
python -m app.drift --once                    # human-readable
python -m app.drift --json                    # for scripts
python -m app.drift --once --grace-seconds 0  # compare everything; idle systems only
```

It **exits 0 even when it finds drift**. Drift is a thing to read, not a thing that
failed, and a non-zero exit would make a CI step red for a condition CI cannot fix —
which is how a report ends up disabled.

### The guarantee, stated precisely

**What you get:**

- **A refund never exceeds its charge.** Enforced by a trigger that takes its own
  lock, so it holds against concurrent requests, against psql, and against a future
  code path that forgets the application-level check.
- **A refund is a reversing posting.** No ledger row is ever updated or deleted, the
  ledger still sums to zero, and a full refund returns the balance to its pre-charge
  value by arithmetic.
- **A refund happens once per key.** Phase 3's replay semantics, plus a derived
  processor reference that makes a post-crash retry a replay on the processor's side
  rather than a second reversal.
- **Disagreement between the two sides is discoverable**, per payment, with both
  figures recorded.

**What you do not get:**

- **No prevention of the refund crash window.** If the transaction dies after the
  processor reversed the money, the two sides disagree until someone reads a drift
  report. Detection, not prevention.
- **No automatic repair.** By design, above.
- **No refund of a refund.** `refunded` is terminal; reversing a reversal is a new
  charge, not a state change.
- **No drift history.** The job reports a run; it does not store findings, because a
  drift table needs a resolution lifecycle (when is a finding closed? by whom?) that
  nothing here would yet honour.
- **No protection from a processor that is wrong.** Reconciling against a corrupted
  source of truth reproduces the corruption faithfully — unchanged from Phase 5a.

### Endpoints and commands added

| Method | Path | Behaviour |
| ------ | ---- | --------- |
| `POST` | `/charges/{id}/refund` | `{amount?}` → 201; omit `amount` to refund whatever is left. 409 if not refundable, 422 if it exceeds the remainder |
| `GET`  | `/charges/{id}/refunds` | Every refund attempt against the charge, declined ones included |

| Command | Behaviour |
| ------- | --------- |
| `python -m app.drift --once` | one drift pass, reported to the log |
| `python -m app.drift --json` | the same report as JSON on stdout |

### Not in Phase 6 (on purpose)

No refund of a refund and no `refunded → anything` transition. No partial-refund
*cancellation*. No drift table, alerting, or auto-repair. No refund sweep — a refund
has no `pending` state to strand, which is exactly what the derived attempt
reference buys. No withdrawal idempotency (still Phase 4's documented gap), no
webhook signature verification, and no dead-letter queue. Hardening, structured
logging, rate limiting, `/metrics` and deploy are Phase 7.

## Smoke acceptance criteria

- Full refund → `refunded`, a reversing posting, balance back to pre-charge, and the
  ledger still sums to zero
- Partial refund moves the balance by the partial amount and leaves the payment
  `succeeded`; a second partial that would exceed the charge is rejected and writes
  zero rows
- An over-refund is **unstorable** — the migration 0007 trigger rejects it even from
  psql, and two simultaneous refunds cannot together exceed the charge
- Refunds are idempotent: one key, one refund, a byte-identical replay
- Refunding a failed, unknown, still-processing or already-fully-refunded payment is
  a 4xx
- The drift job flags an injected discrepancy on either side, and repairs nothing
- `pytest` + `ruff` green locally and in CI


---

**← Previous:** [Phase 5b: reliability](phase-5b-reliability.md) · **[All phases](../README.md#the-phases)** · **Next:** [Phase 7: hardening, observability, deploy](phase-7-hardening-deploy.md) →
