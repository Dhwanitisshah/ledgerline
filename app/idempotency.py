"""Claiming an idempotency key, and replaying what it owes.

The problem this solves is mundane and expensive: a customer double-clicks, a
mobile client retries a request whose response was lost to a dropped connection,
a load balancer replays a timed-out POST. Phase 2's charge flow treats each of
those as a new charge, because it has no way to tell them apart from a genuine
second purchase. Only the caller knows, so only the caller can say -- which is
what the ``Idempotency-Key`` header is for.

## The claim

Ownership of a key is decided by the database, not by application logic::

    INSERT INTO idempotency_keys (...) VALUES (...)
    ON CONFLICT (key) DO UPDATE SET ... WHERE idempotency_keys.expires_at <= now()
    RETURNING key

A row comes back exactly when the caller now owns the key: either it was free, or
it held an expired claim that has just been reset in place. No row means a live
claim already exists, and this request is a retry rather than a new charge.

Writing that as SELECT-then-INSERT would be the same shape of mistake this whole
project is about: a gap between deciding and acting, in which a second request can
slip through and charge the card twice.

## What the claim shares with the charge

Through Phase 4 the claim ran **inside the charge's single transaction**, and that
one decision settled what happened when a request died partway through: if the
charge did not commit, neither did the claim, so the key was never consumed and a
retry was free to try again. No cleanup path, no reaper, no customer locked out by
a key a crash left behind. The cost was that ``status = 'in_progress'`` was never
observable in committed data -- a key became visible only once it was already
'completed'.

**Phase 5a splits that transaction in two** (see app/routers/charges.py), and the
claim now commits with the payment, before the processor is called. Both halves of
the old trade-off invert:

* ``in_progress`` becomes real. A concurrent duplicate is now turned away by
  committed data rather than by the advisory lock, which is what Phase 3's
  docstring predicted and Phase 4 got to by a different route.
* A crash after that first commit **does** consume the key. The lockout this
  module used to make impossible is now possible, and it is bounded by the sweep
  rather than by the 24-hour TTL: ``app/reconcile.py`` settles the abandoned
  payment and finalises its key in the same transaction, so the customer's next
  retry replays the reconciled outcome instead of waiting a day. That is what the
  ``payment_id`` column exists for.

What did not change: a request that fails *before* that first commit -- unknown
account, currency mismatch, bad body -- still takes its claim down with it and
leaves the key free. Those paths never reach the processor, so there is nothing to
reconcile and no reason to consume anything.
"""

import hashlib
import json
import uuid
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 24 hours, matching the window Stripe gives a key. Long enough that a client
# retrying after an outage still gets its recorded answer; short enough that the
# table is a working set rather than an archive.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

# Longest key accepted. Arbitrary but stated: an unbounded primary key is an
# unbounded index.
MAX_KEY_LENGTH = 255


class StoredResponse(NamedTuple):
    """A completed key's recorded answer, ready to be replayed verbatim."""

    body: dict[str, Any]
    status_code: int


class ExistingClaim(NamedTuple):
    """A live claim that this request did not win."""

    request_hash: str
    status: str
    response_snapshot: dict[str, Any] | None
    response_status: int | None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"


def request_fingerprint(
    *, account_id: uuid.UUID, amount: int, currency: str | None
) -> str:
    """Hash the fields that make one charge distinct from another.

    Covers ``account_id``, ``amount`` and ``currency`` -- who is being charged, how
    much, and in what units. If any of those differ, it is a different charge and
    reusing a key for it is a client bug worth reporting rather than papering over.

    **Deliberately excluded: ``force_outcome`` and ``force_latency_ms``.** Those
    are the fake processor's test knobs (see app/processor.py); they change how the
    charge is *simulated*, not which charge it is. Including them would mean a
    retry that flipped ``force_outcome`` counted as a different request and charged
    again -- the exact double-charge this module exists to prevent, reintroduced by
    a field that will not exist once a real processor adapter lands. Excluding them
    also gives the honest semantics: **a key pins an outcome**, so replaying a key
    that recorded a decline returns that decline no matter what the retry asks for.

    ``currency`` is hashed as the client sent it, including ``null`` for "use the
    account's currency". Omitting it and stating it explicitly are therefore
    different fingerprints even when they resolve to the same currency. That is the
    conservative direction to be wrong in: the caller controls the key and is
    expected to resend the same body, and a false "different payload" rejection is
    a 4xx the client can see and fix, where a false match would be a silent replay
    of the wrong charge.
    """
    canonical = json.dumps(
        {"account_id": str(account_id), "amount": amount, "currency": currency},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Claim the key, or take over an expired one. The DO UPDATE branch fires only when
# the existing row has already expired, and it resets the row completely -- an
# expired key must behave exactly like one that was never used, including
# forgetting the response it used to replay.
_CLAIM_SQL = text(
    """
    INSERT INTO idempotency_keys (key, request_hash, status, expires_at)
    VALUES (
        :key,
        :request_hash,
        'in_progress',
        now() + (:ttl_seconds * interval '1 second')
    )
    ON CONFLICT (key) DO UPDATE
    SET request_hash      = EXCLUDED.request_hash,
        status            = 'in_progress',
        response_snapshot = NULL,
        response_status   = NULL,
        -- Reset with everything else. A reclaimed key is a new claim, and leaving
        -- it pointing at the previous charge's payment would give the sweep a
        -- key whose payment it has nothing to do with.
        payment_id        = NULL,
        created_at        = now(),
        expires_at        = EXCLUDED.expires_at
    WHERE idempotency_keys.expires_at <= now()
    RETURNING key
    """
)

_LOAD_SQL = text(
    """
    SELECT request_hash, status, response_snapshot, response_status
    FROM idempotency_keys
    WHERE key = :key
    """
)

_FINALIZE_SQL = text(
    """
    UPDATE idempotency_keys
    SET response_snapshot = CAST(:snapshot AS jsonb),
        response_status   = :status_code,
        status            = 'completed'
    WHERE key = :key
    """
)

_READ_BACK_SQL = text(
    """
    SELECT response_snapshot, response_status
    FROM idempotency_keys
    WHERE key = :key
    """
)

_BIND_PAYMENT_SQL = text(
    "UPDATE idempotency_keys SET payment_id = :payment_id WHERE key = :key"
)

# Only unfinished claims. A completed key already holds the response it owes, and
# the sweep overwriting that would replace a real recorded answer with a
# reconstructed one -- for a payment whose request, by definition, got far enough
# to record it.
_KEY_FOR_PAYMENT_SQL = text(
    """
    SELECT key
    FROM idempotency_keys
    WHERE payment_id = :payment_id AND status = 'in_progress'
    """
)


async def claim_key(session: AsyncSession, key: str, request_hash: str) -> bool:
    """Try to take ownership of ``key``. True when this request now owns it.

    False means a live claim already exists and the caller should load it with
    :func:`load_claim` to decide between replaying and rejecting.
    """
    result = await session.execute(
        _CLAIM_SQL,
        {
            "key": key,
            "request_hash": request_hash,
            "ttl_seconds": IDEMPOTENCY_TTL_SECONDS,
        },
    )
    return result.first() is not None


async def bind_payment(session: AsyncSession, key: str, payment_id: uuid.UUID) -> None:
    """Record which payment this key claimed (Phase 5a).

    Runs in the transaction that commits the payment, so the link and the thing it
    links to become durable together. A key pointing at a payment that does not
    exist is therefore not a state the database can be left in.

    The link is needed in exactly one direction and for exactly one reason: when
    the sweep settles a payment whose request died, it has to find that request's
    key and finalise it. Without this column the sweep can fix the money and still
    leave the customer's retries answered with 409 until the key expires -- a
    24-hour lockout caused entirely by our own crash.
    """
    await session.execute(_BIND_PAYMENT_SQL, {"key": key, "payment_id": payment_id})


async def unfinished_key_for_payment(
    session: AsyncSession, payment_id: uuid.UUID
) -> str | None:
    """The still-open key that claimed ``payment_id``, if there is one.

    Returns None in three cases that are all ordinary rather than exceptional: the
    key already completed (the request got its answer out before dying, or the
    sweep has already been here), the key expired and was reclaimed by someone
    else, or the charge ran on the naive claim path, which writes no key until the
    end. In all three there is nothing for the sweep to finalise.
    """
    row = (await session.execute(_KEY_FOR_PAYMENT_SQL, {"payment_id": payment_id})).first()
    return None if row is None else row.key


async def load_claim(session: AsyncSession, key: str) -> ExistingClaim | None:
    """Read the live claim on ``key``, if there is one."""
    row = (await session.execute(_LOAD_SQL, {"key": key})).first()
    if row is None:
        return None
    return ExistingClaim(
        request_hash=row.request_hash,
        status=row.status,
        response_snapshot=row.response_snapshot,
        response_status=row.response_status,
    )


async def finalize_key(
    session: AsyncSession, key: str, body: dict[str, Any], status_code: int
) -> StoredResponse:
    """Record the response this key will replay, and hand back what was stored.

    The return value is read *back out of the database* rather than being the dict
    that went in, and that is not ceremony. JSONB normalises key order on write, so
    a snapshot that round-trips through Postgres can serialise differently from the
    Python dict that produced it. Sourcing the original response from the stored
    row means the first response and every replay are generated from byte-identical
    input -- which is the property this phase promises, rather than one that merely
    holds until someone inspects it closely.
    """
    await session.execute(
        _FINALIZE_SQL,
        {
            # Serialised here rather than passed as a dict: the bind is a plain
            # text parameter cast to jsonb by the column type, and going through
            # json.dumps keeps the encoding decision in one place.
            "snapshot": json.dumps(body, separators=(",", ":")),
            "status_code": status_code,
            "key": key,
        },
    )
    row = (await session.execute(_READ_BACK_SQL, {"key": key})).one()
    return StoredResponse(body=row.response_snapshot, status_code=row.response_status)


# --- Phase 4: the naive claim, preserved so the fix stays falsifiable -----------
#
# What follows is deliberately broken code. It is the implementation almost
# everyone writes first, and it is kept -- runnable, behind
# IDEMPOTENCY_CLAIM_STRATEGY=naive -- because a fix nobody can watch fail is a fix
# nobody can check.
#
# The shape of the mistake:
#
#     1. SELECT the key. Absent? Then this is a new charge.
#     2. ... charge the card ...
#     3. INSERT the key, ON CONFLICT DO NOTHING.
#
# Step 1 and step 3 are separated by an entire processor call, and step 1's answer
# is a fact about the past by the time step 3 acts on it. Two simultaneous requests
# both read "absent" at step 1, so both charge.
#
# Step 3 is where it becomes silent rather than loud. Without ON CONFLICT the
# second INSERT would raise a duplicate-key error, the transaction would roll back,
# and only one payment would survive -- the primary key would have saved it by
# accident. ON CONFLICT DO NOTHING is the reasonable-looking line that removes that
# accident: it was added to stop duplicate-key errors filling the logs, and it
# works, and the errors stop, and now the system quietly writes two payments
# against one idempotency key. The logs are clean. The customer is charged twice.

_NAIVE_INSERT_SQL = text(
    """
    INSERT INTO idempotency_keys (
        key, request_hash, response_snapshot, response_status, status, expires_at
    )
    VALUES (
        :key,
        :request_hash,
        CAST(:snapshot AS jsonb),
        :status_code,
        'completed',
        now() + (:ttl_seconds * interval '1 second')
    )
    ON CONFLICT (key) DO NOTHING
    """
)


async def claim_key_naive(session: AsyncSession, key: str) -> ExistingClaim | None:
    """The broken claim: look for a live key, and believe the answer.

    Returns the live claim if one is already recorded (so an ordinary, serial
    retry still replays correctly -- which is precisely why this passes every test
    written before Phase 4), or ``None`` meaning "go ahead and charge".

    ``None`` is the lie. It means "no key existed a moment ago", and the caller
    reads it as "no charge for this key is happening", which is a different claim
    entirely and is false whenever another request is mid-flight.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT request_hash, status, response_snapshot, response_status
                FROM idempotency_keys
                WHERE key = :key AND expires_at > now()
                """
            ),
            {"key": key},
        )
    ).first()

    if row is None:
        return None

    return ExistingClaim(
        request_hash=row.request_hash,
        status=row.status,
        response_snapshot=row.response_snapshot,
        response_status=row.response_status,
    )


async def finalize_key_naive(
    session: AsyncSession,
    key: str,
    request_hash: str,
    body: dict[str, Any],
    status_code: int,
) -> StoredResponse:
    """Write the key after the fact, tolerating a key that is somehow already there.

    The tolerance is the bug. See the commentary above.
    """
    await session.execute(
        _NAIVE_INSERT_SQL,
        {
            "key": key,
            "request_hash": request_hash,
            "snapshot": json.dumps(body, separators=(",", ":")),
            "status_code": status_code,
            "ttl_seconds": IDEMPOTENCY_TTL_SECONDS,
        },
    )
    # Returns what this request produced rather than what is stored, because under
    # a lost race those differ -- and the caller genuinely did perform this charge,
    # so this is the honest thing to hand back. That two requests can both be
    # honest about having charged the same card is the whole problem.
    return StoredResponse(body=body, status_code=status_code)
