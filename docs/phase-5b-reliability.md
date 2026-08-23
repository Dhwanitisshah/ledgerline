# Phase 5b: reliability

Phase 5a fixed what happens when *we* lose track of a charge. This phase is about
everyone else: telling downstream systems that a payment happened, and being told
by the processor that one did. Both directions have the same shape of problem, and
it is the shape Phase 5a already established — two systems, one of which cannot be
enrolled in the other's transaction.

**Before:** a successful charge commits, and then publishes an event. Two writes to
two systems, with a gap between them.

**After:** a successful charge commits, and the event is *part of that commit*. A
separate worker delivers it afterwards, as many times as it takes.

### The dual write, stated plainly

Here is the code nobody thinks twice about:

```python
await session.commit()              # the postings, the payment, the key
await broker.publish(event)         # tell everyone
```

There is no ordering of those two lines that is correct.

| Order | What a crash in between leaves |
| ----- | ------------------------------ |
| commit, then publish | Money moved and **nobody was told**. The event does not exist anywhere to be retried from. |
| publish, then commit | Everyone was told about a charge that **never happened**, and consumers have already acted on it. |
| both in a `try/except` | The first half of a distributed transaction, with the compensating half missing |
| two-phase commit | Across a message broker you do not own. Nobody deploys this. |

The reason there is no safe ordering is exactly the reason Phase 5a's crash could
not be fixed by arranging a transaction more carefully: **`COMMIT` is a guarantee
about one database, and the broker is not in it.** A transaction cannot span the
ledger and Kafka any more than it could span the ledger and the card network.

Note that the second row is strictly worse than the first, which is the trap. It
looks safer — publish first, so nothing is missed — and it produces the failure you
cannot undo, because a consumer that has emailed a customer or credited a partner
does not have a rollback.

### The outbox: stop doing the second write

The move is not to make the two writes atomic. It is to **delete the second write**
and replace it with a row in the same database:

```
BEGIN
  INSERT ledger_transaction + 2 entries
  UPDATE payment -> 'succeeded'
  INSERT outbox_events (...)          <- the event
COMMIT
```

One commit. The event is committed **if and only if** the money moved, because they
are the same commit — there is no window between them in which anything can go
wrong, since a window between them does not exist.

The hard part has not been solved so much as **moved somewhere ordinary retries
work**. Delivery is now a separate problem against a durable row, and the worst a
broken consumer can do is make the backlog longer. What it can no longer do is
disagree with the ledger. [app/outbox.py](../app/outbox.py) holds the argument in
full; the enforcement is one line in the charge route,
`await record_payment_succeeded(session, payment)`, written where the postings are.

This column tracks that column, in every row, and that is the guarantee rather than
a coincidence of layout:

| Outcome | ledger rows | outbox event |
| ------- | ----------- | ------------ |
| processor succeeded | committed, balanced | **written** |
| processor declined | never written | **none** |
| crash after the processor said yes | not written | none |
| ledger invariant violated (bug) | rolled back | rolled back |
| the sweep settles a stranded charge | written by the sweep | **written by the sweep** |
| the sweep settles it as failed | never written | none |

The event lives at the point of **settlement**, not in the charge route, because
there are three ways a payment reaches `succeeded` — the request, the sweep, and a
webhook — and only one of them is a request. All three emit the identical
`payment.succeeded` event, so a consumer cannot tell that a charge settled four
minutes late through a recovery path. It should not be able to: a charge that
succeeded late is still a charge that succeeded.

### The publisher, and the promise it can actually keep

[app/publisher.py](../app/publisher.py) drains the outbox:

```
BEGIN
  SELECT ... WHERE status='pending' ORDER BY created_at
    FOR UPDATE SKIP LOCKED LIMIT 1
  ── deliver to the sink ──          -- the sink commits on its own session
  UPDATE ... SET status='published'
COMMIT
```

One event, one transaction, one commit, in the same idiom as the reconciler. `SKIP
LOCKED` is what makes several workers safe to run at once: they divide the backlog
instead of contending over its head.

**The guarantee is at-least-once delivery, and it cannot be better than that.** One
publish is two writes to two systems — the delivery, and the local mark recording
it — so the same argument applies here as everywhere else in this phase. A worker
that dies between them leaves an event that was delivered and is still marked
pending, and the next pass delivers it again.

That is not a bug awaiting a fix. Every "exactly-once" message system is this
arrangement with the duplicate suppressed somewhere further along. The only real
question is which side loses when you die at the wrong moment:

```
deliver, then mark   ->  a crash DUPLICATES an event
mark, then deliver   ->  a crash LOSES an event
```

This publisher delivers first, deliberately. A duplicate is recoverable by an
idempotent consumer; a lost event is recoverable by nobody, because nothing
anywhere still says it should have been sent.

### Where "exactly once" actually comes from

The consumer, and nowhere else:

```sql
INSERT INTO event_deliveries (event_id, event_type, payload)
VALUES (:event_id, :event_type, :payload)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id
```

The primary key **is** the event id. A second delivery collides with a unique index
and does nothing. Note what that is *not*: not the worker being careful, not a
sequence number, not a distributed lock. It is a constraint in the receiving
database, which is the only place "this has already been handled" can be claimed
without a race.

`event_deliveries` is **not Ledgerline's data**, in the same sense as
`processor_charges` — it stands in for whatever a downstream service keeps, it is
written on its own session and its own transaction, and it has no foreign key into
the outbox because a consumer in another process could not declare one. A real
consumer would keep this table in its own database and nothing about the argument
would change.

Sending a **stable event id** is therefore the sender's entire obligation, and it is
why the outbox row's primary key is the id we transmit rather than something that
changes between attempts.

**The measurement**, from `test_a_worker_killed_mid_publish_delivers_exactly_once`:

| Step | outbox row | `event_deliveries` |
| ---- | ---------- | ------------------ |
| charge succeeds | 1, `pending` | 0 |
| worker delivers, then is killed before the mark | 1, **still `pending`** | **1** |
| worker restarts and delivers again | 1, `published` | **still 1** |

Two deliveries. One effect.

### Idempotent webhooks: the same problem, pointed inwards

Processors deliver webhooks **at least once**. That is not a caveat about flaky
networks — it is the strongest promise a sender can make, because a provider that
receives no response cannot distinguish "never arrived" from "handled, response
lost". So it sends again. Every provider does.

`POST /webhooks` therefore deduplicates on the provider's event id, with the same
mechanism used everywhere else in this project:

```sql
INSERT INTO webhook_events (event_id, ...) VALUES (...)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id
```

A row comes back exactly when this delivery is the first, and only that delivery
does any work. Written as `SELECT`-then-`INSERT` it would be the Phase 4
check-then-act mistake, and two simultaneous deliveries — which providers genuinely
produce, since a retry can overlap the delivery it is retrying — would both find
nothing and both act.

**There is no `in_progress` status here**, unlike `idempotency_keys`, and the
difference is worth understanding. The claim and the settlement it authorises are
*one transaction*: if the settlement does not commit, neither does the claim, so a
redelivery correctly re-processes an event whose handling was lost. That is exactly
the property Phase 3 had and Phase 5a had to trade away — and this table keeps it
because handling a webhook never calls a third party mid-transaction.

#### The webhook says *when*. The processor says *what*.

The handler does not read `type` and write the matching status. It calls
`reconcile_payment` — the identical function the Phase 5a sweep calls — which locks
the payment and asks the processor's books.

```
POST /webhooks  ──▶  reconcile_payment(payment_id)  ──▶  processor.lookup()
python -m app.reconcile ──▶  reconcile_payment(payment_id)  ──▶  processor.lookup()
```

So the push path is a **latency optimisation on the pull path**, not a second
differently-behaved implementation of it. The consequences are worth listing,
because the obvious implementation — read `type == "charge.succeeded"`, mark the
payment succeeded, write the posting — passes every deduplication test in the suite
and is still wrong:

- a replayed, reordered or delayed event cannot move money the processor has no
  record of;
- event **ordering** becomes a latency concern rather than a correctness one;
- anyone who can reach the endpoint cannot credit an account by asserting it;
- `test_the_processor_decides_the_outcome_not_the_payload` sends a
  `charge.succeeded` event for a card the processor **declined**, and the payment
  settles as `failed` with zero ledger entries.

#### Two independent layers

1. **The event id**, above. Defeated by a provider that issues a fresh id on retry —
   some do, for some event classes.
2. **The settlement itself.** `SELECT ... FOR UPDATE SKIP LOCKED` with `status =
   'processing'` inside the lock, and a state machine with no second move out of a
   terminal state. A second settlement of one payment is not prevented by a check;
   it is unrepresentable.

Layer 2 covers a duplicate *effect* even with layer 1 removed entirely. Layer 1
covers a duplicate *event* even for payments that are not settleable. They fail
differently, which is the only reason to have both.

#### Status codes are somebody else's retry loop

Providers read a 2xx as "handled, stop sending" and anything else as "try again".

| Situation | Response |
| --------- | -------- |
| First delivery, payment settled | 200 |
| Duplicate delivery | 200 — already handled, stop sending |
| Payment already settled by the sweep | 200 — nothing to do, and that is fine |
| **Unknown attempt reference** | **404 — please retry** |
| Unrecognised event type, blank id, malformed body | 422 |

The 404 is deliberate rather than a default. A webhook can legitimately arrive
*before* the charge's transaction A commits — the processor's books and ours are
written on different connections with no ordering between them. Recording such an
event as handled would swallow a real notification for a payment that is about to
exist. So the claim is rolled back with the request and the event id is left free,
which is Phase 3's "a failed request leaves its key unconsumed", reused exactly.

### How this closes the Phase 2 gap, and what closes it

Worth being precise, because two mechanisms are involved and only one of them is
new here.

The gap named in Phase 2 — *the process dies between the processor returning
success and `COMMIT`* — is closed by **reconciliation against the processor's
records**, which shipped in Phase 5a: the attempt is committed as `processing`
before the processor is called, and [app/reconcile.py](../app/reconcile.py) settles
whatever is left stranded by asking the processor what actually happened. The DB
record is reconciled against the processor's record. That is the closure, and it is
worth repeating that it is **jointly held** — it requires the processor to answer
"what happened to attempt X?", and against one that cannot, the window can only be
narrowed, never closed.

Phase 5b adds a second trigger for that same machinery, and does not change the
mechanism: `POST /webhooks` calls the same `reconcile_payment`. What it buys is
**latency**. The sweep resolves a stranded payment within
`RECONCILE_STUCK_AFTER_SECONDS` plus one interval; the webhook resolves it the
moment the processor knows.

Push and pull are meant to run together, so they will regularly reach the same
stranded payment at once. They settle it once, for the reason two sweeps do — the
row lock, and the `status = 'processing'` predicate inside it. There is a test that
races them.

### The guarantee, stated precisely

**Outbound:**

> **At-least-once delivery, exactly-once effect.** Every event describing money that
> moved will be delivered, possibly more than once; a consumer that deduplicates on
> the event id acts on it exactly once.

**Inbound:**

> **At-least-once delivery, exactly-once effect.** A processor may post the same
> event any number of times; the settlement it describes happens exactly once.

**And the invariant underneath both:** an outbox event exists if and only if the
ledger posting exists, because they are written by one transaction.

**What you do not get:**

- **Not exactly-once delivery.** Nobody offers this, and a system claiming to is
  doing what this one does with the duplicate hidden further along. A consumer that
  does not deduplicate *will* double-process, and no amount of care on the sending
  side prevents it.
- **Not ordering.** Events are published oldest-first for fairness, but `SKIP
  LOCKED` across several workers gives no global order, and a failed delivery
  reorders the rest. Consumers must tolerate any order.
- **Not synchronous propagation.** A consumer's view can lag by up to
  `OUTBOX_INTERVAL_SECONDS` plus a delivery. The event is durable the moment the
  charge commits; it is only the *delivery* that is behind.
- **Not liveness without a worker.** The outbox accumulates whether or not anything
  drains it. Same shape as Phase 5a: the design buys the ability to recover, and
  something still has to run.
- **Not protection from a consumer that deduplicates badly.** Half of the
  exactly-once effect is enforced in a database this service does not own.
- **No dead-letter queue.** An event that fails forever retries forever, with
  `attempts` climbing. Nothing alerts on it yet.

### The honest cost of this arrangement

The publisher holds the outbox row lock **across the delivery**, so a slow consumer
parks a Postgres backend — the exact pathology Phase 5a removed from the charge
path. It is tolerable here for reasons worth naming rather than glossing over: the
sink is local, the worker is a background process whose latency nobody is waiting
on, and the concurrency is one connection per worker rather than one per request.

Against a genuinely slow remote consumer this stops being right, and the fix is a
**lease** — claim the row in a short transaction with a `claimed_until`, commit,
deliver holding nothing, then mark. That trades a simple schema for a
reclaim-on-expiry path and needs a status this table deliberately does not have. It
is the next thing to build, not a defect being hidden.

Two smaller ones, recorded rather than smoothed over:

- **`attempts` undercounts.** It counts attempts that reached a `COMMIT`. An attempt
  lost to the process dying increments nothing, because its transaction is rolled
  back by the same death — which is the same property that makes redelivery correct.
  A failed *delivery* is recorded on a fresh transaction, so a consumer that is
  merely down does climb.
- **Polling, not `LISTEN`/`NOTIFY`.** A notification goes to whoever is connected at
  that moment, so a worker that is restarting never hears it. A poll finds
  everything pending regardless of who was awake. `NOTIFY` is a fine latency
  optimisation *on top of* a poll and a disastrous replacement for one.

### Endpoints and commands added

| Method | Path | Behaviour |
| ------ | ---- | --------- |
| `POST` | `/webhooks` | `{id, type, data:{attempt_ref}}` → 200; deduped by `id`; 404 if the attempt is unknown |
| `GET`  | `/webhooks/{id}` | What handling that event did; 404 if never received |

| Command | Behaviour |
| ------- | --------- |
| `python -m app.publisher` | drain the outbox on an interval until interrupted |
| `python -m app.publisher --once` | one pass and exit |
| `python -m app.publisher --status` | `{pending, published, delivered}` as JSON, publishing nothing |
| `python -m app.reconcile --list` | the stranded payment ids as JSON, settling nothing |

### Not in Phase 5b (on purpose)

No real broker — every property claimed here is a property of how a *receiver*
deduplicates, and that is identical whether the event arrived over Kafka, an HTTP
POST, or a function call. Adding one would add a dependency and demonstrate nothing
further.

No webhook signature verification, which a production receiver needs and which is
orthogonal to idempotency (and less critical here than usual, because the payload is
not the authority — a forged event can at worst cause a lookup). No dead-letter
queue or alerting on repeatedly-failing events. No archival or pruning of the outbox
— it grows forever. No event schema versioning. No refunds (Phase 6).

## Smoke acceptance criteria

- The outbox event is committed atomically with the money; a failed charge writes
  none, and a killed settlement takes the event down with the posting
- The worker delivers exactly once in effect across a mid-publish kill and restart
  — two deliveries, one row in `event_deliveries`
- A duplicate webhook is a no-op: same outcome, one posting, balance moved once
- A payment stranded in `processing` is detected and resolved — by the sweep, and
  now also by a webhook, both through the same `reconcile_payment`
- The payload is not the authority: a `charge.succeeded` event for a declined card
  settles as `failed` and moves no money
- `pytest` + `ruff` green locally and in CI


---

**← Previous:** [Phase 5a: durability](phase-5a-durability.md) · **[All phases](../README.md#the-phases)** · **Next:** [Phase 6: refunds and reconciliation](phase-6-refunds-reconciliation.md) →
