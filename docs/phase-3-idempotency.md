# Phase 3: idempotency

Phase 2's charge flow was correct and still dangerous. It had no way to tell a
genuine second purchase from the same purchase arriving twice — and the same
purchase arrives twice constantly: a double-clicked button, a mobile client
retrying a POST whose response was lost to a dead cell, a load balancer replaying a
request it decided had timed out.

**Before:** the customer clicks Pay twice and is charged twice. Both charges are
individually perfect — balanced postings, correct states, a clean audit trail
saying exactly how they were robbed.

**After:** the second request returns the first request's response, byte for byte.
One payment, one posting, one authorisation on the card.

Only the caller knows whether two requests mean one purchase, so only the caller
can say. That is what the `Idempotency-Key` header is for.

### The contract

`POST /charges` **requires** an `Idempotency-Key` header.

| Situation                          | Response                                          |
| ---------------------------------- | ------------------------------------------------- |
| New key                            | The charge runs; 201                              |
| Same key, same body                | The stored response, replayed byte for byte       |
| Same key, different body           | 422, and nothing is written                       |
| No key / blank / over 255 chars    | 400                                               |
| Key older than 24h                 | Treated as never used; the charge runs            |

Required rather than optional. An optional safety net is missing from exactly the
client that needed it, and a caller who has not thought about retries is the one
most likely to send one.

### The claim is a database decision

Ownership of a key is settled by Postgres, not by application logic:

```sql
INSERT INTO idempotency_keys (key, request_hash, status, expires_at)
VALUES (:key, :request_hash, 'in_progress', now() + (:ttl_seconds * interval '1 second'))
ON CONFLICT (key) DO UPDATE
SET request_hash = EXCLUDED.request_hash, status = 'in_progress',
    response_snapshot = NULL, response_status = NULL,
    created_at = now(), expires_at = EXCLUDED.expires_at
WHERE idempotency_keys.expires_at <= now()
RETURNING key
```

A row comes back exactly when this request now owns the key — either it was free,
or it held an expired claim just reset in place. No row means a live claim exists
and this is a retry. The `DO UPDATE ... WHERE expired` branch is what makes TTL
expiry work without a sweeper: an expired key is *reclaimed*, and reset completely,
because a key past its TTL must behave exactly like one that was never used.

Writing this as `SELECT` then `INSERT` would be the same shape of mistake this
project is about — a gap between deciding and acting, wide enough for a second
request to charge the card again.

### The key commits with the charge

The claim runs **inside the charge's transaction**. That one decision answers what
happens when a request dies partway through: if the charge does not commit, neither
does the claim, so the key was never consumed and the retry is free to try again.
There is no cleanup path and no reaper, because a key that did not commit does not
exist. A charge that 404s on an unknown account leaves no key behind — there is a
test for exactly that.

The visible cost: `status = 'in_progress'` is **never observed in committed data**
in Phase 3. A key becomes visible to anyone else only once it is already
`completed`. The column exists because Phase 4 needs it.

### What the fingerprint covers, and what it deliberately does not

`request_hash` is SHA-256 over a canonical JSON encoding of **`account_id`,
`amount`, `currency`** — who is being charged, how much, in what units.

It **excludes `force_outcome` and `force_latency_ms`**, the fake processor's test
knobs. Those change how a charge is *simulated*, not which charge it is. Including
them would mean a retry that flipped `force_outcome` counted as a different request
and charged again — reintroducing the exact double-charge this feature prevents,
via a field that will not exist once a real adapter lands. Excluding them also
gives the honest semantics: **a key pins an outcome.** Replay a key that recorded a
decline and you get that decline back, no matter what the retry asks for.

`currency` is hashed as the client sent it, `null` included. Omitting it and
stating the account's own currency therefore produce different fingerprints even
though they resolve to the same charge. That is the conservative direction to be
wrong in: a false "different payload" is a 422 the client can see and fix, where a
false match would silently replay the wrong charge.

### Byte-identical, and why that took work

`response_snapshot` is `JSONB`, and JSONB normalises key order on write. So a
snapshot that round-trips through Postgres serialises differently from the Python
dict that produced it — which would make the *first* response and its replays
differ, in exactly the way nobody notices until a client diffs them.

So the first response is not sent from the dict that was stored. It is read back
out of the database after the write and served from that. Both the original and
every replay are generated from identical bytes. The visible symptom of this
working is that charge responses come back in JSONB's key order rather than the
declaration order of `ChargeOut` — JSON objects are unordered, so no client should
care, but it is the fingerprint of the mechanism.

### The gap this leaves, stated plainly

Phase 3 handles a retry that arrives **after** the first request finished. Two
requests genuinely **in flight at once** on the same key are a different problem:
the first claim is invisible to everyone until it commits, and it does not commit
until the charge is done. Phase 4 owns that, and it is why `'in_progress'` exists
here but is never seen.

### Not in Phase 3 (on purpose)

No background expiry sweeper — expired keys are reclaimed lazily when the same key
is presented again, though `ix_idempotency_keys_expires_at` is there for when one
is written. No idempotency on any endpoint other than `POST /charges`. Simultaneous
same-key requests are [Phase 4](phase-4-concurrency.md).

## Smoke acceptance criteria

- Same key + same body → one charge, identical replayed response
- Same key + different body → 4xx, nothing written
- Missing key → 400. Expired key → fresh charge allowed
- Failure path replays identically
- `pytest` + `ruff` green locally and in CI


---

**← Previous:** [Phase 2: the payment lifecycle](phase-2-payment-lifecycle.md) · **[All phases](../README.md#the-phases)** · **Next:** [Phase 4: concurrency](phase-4-concurrency.md) →
