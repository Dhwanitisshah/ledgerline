"""The transactional outbox: writing an event in the transaction that earns it.

## The problem, stated without an outbox in it

A charge succeeds. Two things now have to be true: the ledger holds a balanced
posting, and whatever cares about payments downstream -- an emails service, an
analytics pipeline, a partner's reconciliation feed -- has been told. The obvious
implementation is two writes::

    COMMIT                       -- the postings
    broker.publish(event)        -- tell everyone

That is a **dual write**, and it has no correct ordering:

* publish after the commit, and a crash in between moves money nobody hears about;
* publish before the commit, and a rollback leaves an event describing a charge
  that never happened -- worse, because a consumer has already acted on it and
  there is nothing to un-act;
* wrap both in a try/except and you have written the first half of a distributed
  transaction, badly, with the compensation missing;
* two-phase commit across a message broker you do not own is the thing nobody
  actually deploys.

The reason there is no ordering that works is the same reason Phase 5a's crash
could not be fixed by arranging a transaction more carefully: ``COMMIT`` is a
guarantee about one database, and the broker is not in it.

## The move

Stop performing the second write. Replace it with a row **in the same database**,
inserted in the **same transaction** as the money::

    BEGIN
      INSERT ledger_transaction + 2 entries
      UPDATE payment -> 'succeeded'
      INSERT outbox_events (...)          <- the event
    COMMIT

One commit. The event is committed if and only if the money moved, because they
are the same commit -- there is no window between them in which anything can
happen, since a window between them does not exist.

Publishing then becomes a separate, retryable, crash-safe problem, owned by
``app/publisher.py``: read pending rows, deliver them, mark them published. The
hard part has not been solved so much as **moved to a place where ordinary
retries work**, which is the whole trick. Delivery can fail arbitrarily often and
the only cost is delay; what it can no longer do is disagree with the ledger.

## What this module does and does not own

It owns the table: writing an event into a caller's transaction, claiming the
oldest pending one under a lock, and marking one published. It does **not** own
publishing, the worker loop, or the sink -- those are ``app/publisher.py``, kept
separate because "what an event is" and "how an event gets delivered" have
genuinely different reasons to change.

Every function here that writes takes a caller's session and does **not** commit,
following the same convention as ``write_posting`` and ``reconcile_payment``: this
module arranges rows, and the decision to make them durable belongs to whoever
knows the whole unit of work. In the case of :func:`record_event` that is not a
convention but the entire guarantee, so it is worth saying plainly: **if this
function ever grew a commit, the outbox would become a dual write again.**
"""

import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, Refund

#: The only event type Phase 5b emits. Named for the fact rather than for the
#: caller -- ``payment.succeeded`` is true whether the charge route, the sweep or
#: a webhook was the thing that noticed, and a consumer must not have to care
#: which. All three emit this identical event through
#: :func:`record_payment_succeeded`.
PAYMENT_SUCCEEDED = "payment.succeeded"

#: Emitted when money goes back, in the transaction that sends it (Phase 6).
#: Emitted for a PARTIAL refund as well as a full one, and the payload carries the
#: numbers a consumer needs to tell them apart -- ``amount`` for this reversal,
#: ``total_refunded`` and ``remaining_refundable`` for where the payment now
#: stands. A consumer that only cares about "is this charge fully reversed?" reads
#: ``payment_status``; one running a refund ledger of its own reads ``amount``.
#: Splitting this into two event types would have made every consumer branch on
#: something Ledgerline already knows.
PAYMENT_REFUNDED = "payment.refunded"

_INSERT_EVENT_SQL = text(
    """
    INSERT INTO outbox_events (event_type, payload)
    VALUES (:event_type, CAST(:payload AS jsonb))
    RETURNING id
    """
)

# The oldest pending event, claimed rather than merely read.
#
# SKIP LOCKED is what makes several publishers safe to run at once: a row another
# worker is already delivering is not returned, so the fleet divides the backlog
# instead of contending over its head. Plain FOR UPDATE would make every extra
# worker a queue behind the first one -- which is not incorrect, just pointless.
#
# The `status = 'pending'` predicate is *inside* the locked statement on purpose.
# Checking it first and locking second would be the same check-then-act shape
# Phase 4 is about: the answer would be a fact about the past by the time the lock
# was taken.
#
# `skip_ids` excludes events this *pass* has already tried and failed. Without it a
# permanently undeliverable event is re-claimed the instant its own transaction
# rolls back, and the pass spins on it until the batch limit -- a live-lock that
# also starves every event behind it. Note that it is per-pass and in memory
# rather than a column: the exclusion should last exactly as long as the knowledge
# that produced it, and the next pass genuinely should try again.
_CLAIM_NEXT_SQL = text(
    """
    SELECT id, event_type, payload, attempts
    FROM outbox_events
    WHERE status = 'pending'
      AND NOT (id = ANY(:skip_ids))
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
    """
).bindparams(sa.bindparam("skip_ids", type_=ARRAY(sa.Uuid)))

# `AND status = 'pending'` is belt and braces -- the claim above already holds the
# row lock, so nobody else can have moved it. Kept because it costs nothing and
# makes the statement correct read on its own, without the reader having to know
# what the caller did first.
_MARK_PUBLISHED_SQL = text(
    """
    UPDATE outbox_events
    SET status       = 'published',
        published_at = now(),
        attempts     = attempts + 1
    WHERE id = :id AND status = 'pending'
    """
)

_RECORD_ATTEMPT_SQL = text("UPDATE outbox_events SET attempts = attempts + 1 WHERE id = :id")

_COUNTS_SQL = text(
    """
    SELECT
        count(*) FILTER (WHERE status = 'pending')   AS pending,
        count(*) FILTER (WHERE status = 'published') AS published
    FROM outbox_events
    """
)


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """One claimed event, ready to hand to a sink."""

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    attempts: int


async def record_event(
    session: AsyncSession, event_type: str, payload: dict[str, Any]
) -> uuid.UUID:
    """Write one event into the caller's open transaction. Does not commit.

    The absence of a commit here is the guarantee, not an oversight. This row must
    become durable at the same instant as the rows that justify it, which means the
    caller's ``COMMIT`` and no other. A commit inside this function would restore
    the dual write it exists to remove -- with the failure mode inverted and harder
    to see, since the event would then be the write that survives a crash the money
    did not.
    """
    result = await session.execute(
        _INSERT_EVENT_SQL,
        {
            # Serialised here rather than passed as a dict, matching
            # app/idempotency.py: the bind is a text parameter cast to jsonb, and
            # the encoding decision stays in one place.
            "event_type": event_type,
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        },
    )
    return result.scalar_one()


async def record_payment_succeeded(session: AsyncSession, payment: Payment) -> uuid.UUID:
    """Emit ``payment.succeeded`` for a payment whose posting is in this transaction.

    Called from all three places money can start moving -- the charge route, the
    reconciliation sweep, and the webhook receiver -- and called from inside each
    one's settlement transaction. A consumer therefore cannot tell which path
    produced an event, which is the correct amount of information to give it: a
    charge that succeeded four minutes late because its request died is still just
    a charge that succeeded.

    Every field is a plain JSON scalar. Consumers do not share this service's Python
    types, its enums, or its migrations, and an event that requires them to is an
    event that breaks the day either side is deployed alone.
    """
    return await record_event(
        session,
        PAYMENT_SUCCEEDED,
        {
            "payment_id": str(payment.id),
            "account_id": str(payment.account_id),
            # Minor units, as everywhere else. An event is not the place to start
            # being creative about money's type.
            "amount": payment.amount,
            "currency": payment.currency,
            "status": str(payment.status),
            "processor_ref": payment.processor_ref,
            "ledger_transaction_id": (
                None
                if payment.ledger_transaction_id is None
                else str(payment.ledger_transaction_id)
            ),
        },
    )


async def record_payment_refunded(
    session: AsyncSession, payment: Payment, refund: Refund, *, total_refunded: int
) -> uuid.UUID:
    """Emit ``payment.refunded`` for a reversal whose posting is in this transaction.

    Same contract as :func:`record_payment_succeeded` and for the same reason: the
    row is written beside the reversing posting, in the settlement transaction, so
    the event exists if and only if the money actually went back. A refund that
    rolls back takes its announcement with it.

    ``total_refunded`` is passed in rather than re-queried because the caller has
    just computed it under the payment lock, and reading it again here would be a
    second answer to a question that already has one -- with a window between them
    in which they could differ.
    """
    return await record_event(
        session,
        PAYMENT_REFUNDED,
        {
            "payment_id": str(payment.id),
            "refund_id": str(refund.id),
            "account_id": str(payment.account_id),
            # This reversal, in minor units. NOT the running total -- see below.
            "amount": refund.amount,
            "currency": refund.currency,
            "charged": payment.amount,
            # Where the payment stands after this reversal. Both are derived from
            # the refunds table rather than stored anywhere, which is why they are
            # computed once, under the lock, and carried here.
            "total_refunded": total_refunded,
            "remaining_refundable": payment.amount - total_refunded,
            # 'succeeded' while partly refundable, 'refunded' once nothing is left.
            "payment_status": str(payment.status),
            "processor_ref": refund.processor_ref,
            "ledger_transaction_id": (
                None
                if refund.ledger_transaction_id is None
                else str(refund.ledger_transaction_id)
            ),
        },
    )


async def claim_next_pending(
    session: AsyncSession, *, skip: Collection[uuid.UUID] = ()
) -> PendingEvent | None:
    """Lock the oldest unpublished event, or return None if there are none.

    The lock is held until the caller's transaction ends, which is what makes the
    publisher's "deliver, then mark published" pair atomic against every other
    publisher. None means the backlog is empty, *or* every event in it is already
    being delivered by somebody else, *or* the rest have been excluded by ``skip``
    -- three situations that warrant the same response, which is why they are not
    distinguished.

    ``skip`` is how a caller says "not these, I have already tried them this pass".
    """
    row = (await session.execute(_CLAIM_NEXT_SQL, {"skip_ids": list(skip)})).first()
    if row is None:
        return None
    return PendingEvent(
        id=row.id, event_type=row.event_type, payload=row.payload, attempts=row.attempts
    )


async def mark_published(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Record that ``event_id`` has been delivered. Does not commit.

    Deliberately in the caller's transaction, and deliberately *after* the delivery
    rather than before it. The gap between the delivery and this commit is the
    window in which a dying worker produces a duplicate -- which is allowed, because
    the sink is idempotent. Marking first would produce the other kind of window,
    in which a dying worker produces a *lost event*, and no amount of idempotency
    downstream recovers one of those.
    """
    await session.execute(_MARK_PUBLISHED_SQL, {"id": event_id})


async def record_failed_attempt(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Bump ``attempts`` for a delivery that raised. Does not commit.

    Called on its own fresh session by the publisher, precisely because the
    transaction that tried to deliver has been rolled back and this must not be.
    An event that fails repeatedly should visibly climb rather than sit at zero
    looking untouched.
    """
    await session.execute(_RECORD_ATTEMPT_SQL, {"id": event_id})


async def counts(session: AsyncSession) -> tuple[int, int]:
    """``(pending, published)``. For the status CLI and the smoke script."""
    row = (await session.execute(_COUNTS_SQL)).one()
    return int(row.pending), int(row.published)
