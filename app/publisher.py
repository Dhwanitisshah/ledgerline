"""The worker that drains the outbox, and the sink it drains into.

``app/outbox.py`` guarantees that an event exists if and only if the money moved.
This module is what then gets that event *out*, and the guarantee it can offer is
strictly weaker, on purpose:

    **At-least-once delivery. Exactly-once effect.**

Both halves are load-bearing and the second is not a restatement of the first.

## Why delivery cannot be exactly-once

One publish is two writes to two systems: the delivery itself, and the local
``status = 'published'`` that records it. Those cannot be made atomic for exactly
the reason the outbox exists in the first place -- ``COMMIT`` is a guarantee about
one database and the consumer is not in it. So a process that dies between them
leaves an event that was delivered and is still marked pending, and the next pass
delivers it again.

That is not a bug to be engineered away. It is the shape of the problem, and every
"exactly-once" message system is this arrangement with the duplicate suppressed
somewhere further along. The only real question is **which side of the wire loses**
if you die at the wrong moment::

    deliver, then mark        -> a crash duplicates an event
    mark, then deliver        -> a crash loses an event

This module delivers first, and that ordering is a deliberate choice rather than
an accident of how the code reads. A duplicate is recoverable by an idempotent
consumer; a lost event is recoverable by nobody, because there is no longer
anything anywhere that says it should have been sent. Given a choice between a
failure mode the other side can fix and one nobody can, take the first every time.

## Where exactly-once actually comes from

The consumer, and nowhere else. :class:`DeliveryLog` inserts into
``event_deliveries`` with the event id as the primary key, ``ON CONFLICT DO
NOTHING``. A second delivery of one event collides with a unique index and does
nothing at all. Note what that is *not*: it is not this worker being careful, not a
sequence number, and not a distributed lock. It is a constraint in the receiving
database, which is the only place a claim about "this has already been handled" can
be made without a race.

This is the same argument as Phase 5a's processor books, pointed the other way.
There, a third party's idempotency on our attempt reference is what made our retry
safe. Here, our event id is what makes a downstream consumer's retry safe. Sending
a stable id is the entire obligation the sender has, and it is why the outbox row's
primary key is the event id rather than an autoincrementing sequence that would
change between attempts.

## One event, one transaction

::

    BEGIN
      SELECT ... WHERE status='pending' ORDER BY created_at
        FOR UPDATE SKIP LOCKED LIMIT 1      -- claim, with the lock held below
      ── deliver to the sink ──             -- the sink commits on its own session
      UPDATE ... SET status='published'
    COMMIT

Per event rather than per batch, in the same idiom as ``app/reconcile.py``: a batch
transaction means one undeliverable event rolls back the publication of every event
around it, and holds a row lock on all of them meanwhile. Queue workers commit per
item.

**The honest cost of this arrangement**: the row lock is held across the delivery,
so a slow consumer parks a Postgres backend for the length of a delivery -- exactly
the pathology Phase 5a removed from the charge path. It is tolerable here for
reasons worth naming rather than glossing: the sink is local and fast, the worker
is a background process whose latency nobody is waiting on, and the concurrency is
one connection per worker rather than one per request. Against a genuinely slow
remote consumer this arrangement stops being right, and the fix is a **lease** --
claim the row in a short transaction with a ``claimed_until`` timestamp, commit,
deliver with nothing held, then mark. That trades a simpler schema for a
reclaim-on-expiry path, needs a status this table deliberately does not have, and
is not needed at this scale. It is the next thing to build, not a defect being
hidden.

## Running it

Deliberately a separate process (``python -m app.publisher``), like the reconciler
and for a related reason: the outbox is the thing that still works when the API is
down. Events accumulate in a table that needs nothing running to accept them, and a
worker drains them whenever it comes back. Coupling the drain to the web process
would mean an outage stops the delivery of exactly the events the outage produced.
"""

import argparse
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.observability import configure_logging
from app.outbox import (
    PendingEvent,
    claim_next_pending,
    counts,
    mark_published,
    record_failed_attempt,
)

logger = logging.getLogger("ledgerline.publisher")


class SimulatedWorkerCrash(BaseException):
    """The worker dying mid-publish: after the delivery, before the COMMIT.

    A :class:`BaseException` rather than an ``Exception``, and that is the whole
    point of the class. :func:`publish_once` catches ``Exception`` around the
    delivery so that a failing sink is recorded and retried rather than killing the
    loop -- and a simulated crash must not be caught by that, because a real
    ``kill -9`` is not caught by anything. Inheriting from ``BaseException`` makes
    it structurally impossible for the handler to swallow it, instead of relying on
    an ``except`` clause staying in the right order forever.

    To Postgres this is indistinguishable from the backend dying: a transaction
    that ends without ``COMMIT``. What it does not simulate is the process failing
    to come back, which is why the test restarts the publisher explicitly.
    """


class EventSink(Protocol):
    """Where a published event goes.

    One method, returning whether *this* delivery was the first one. Real sinks
    (an HTTP endpoint, a broker) rarely tell you that, and a sink that cannot is
    perfectly usable here -- it would return ``True`` always and the exactly-once
    effect would still hold, because the effect is enforced by the consumer's
    constraint rather than by this return value. It exists so the report can
    distinguish "delivered" from "delivered again and correctly ignored", which is
    the distinction the whole phase is about and would otherwise be invisible.
    """

    async def deliver(self, event: PendingEvent) -> bool: ...


_INSERT_DELIVERY_SQL = text(
    """
    INSERT INTO event_deliveries (event_id, event_type, payload)
    VALUES (:event_id, :event_type, CAST(:payload AS jsonb))
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
    """
)

_COUNT_DELIVERIES_SQL = text("SELECT count(*) FROM event_deliveries")


class DeliveryLog:
    """The consumer's side of the wire, standing in for a downstream service.

    There is no broker in this project and there does not need to be one: every
    property Phase 5b claims is a property of *how a receiver deduplicates*, and
    that is identical whether the event arrived over Kafka, an HTTP POST, or a
    function call. Putting a broker in would add a dependency and demonstrate
    nothing further.

    Two things about this class are not simplifications and must survive any real
    implementation:

    1. **It writes on its own session, in its own transaction.** Not the
       publisher's. When the publisher's transaction rolls back -- because the
       process died between the delivery and the mark -- this row stays. That
       asymmetry is what makes the duplicate reachable, and therefore what makes
       the dedupe worth having. It is the same argument as ``ProcessorBooks`` in
       app/processor.py, and if this ever took the caller's session the whole
       demonstration would quietly evaporate.
    2. **It deduplicates with a primary key, not a lookup.** No
       ``SELECT ... if not exists ... INSERT``, which is the check-then-act shape
       Phase 4 is entirely about and which two concurrent publishers would walk
       straight through.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        crash_after_delivery: bool = False,
    ) -> None:
        self._session_factory = session_factory
        # A test affordance, in the same spirit as `force_crash_after_processor` on
        # POST /charges and named just as unmistakably. It kills the worker at the
        # one instant that produces a duplicate: the delivery is committed, the
        # publisher's `status = 'published'` is not. Nothing in production sets it.
        self.crash_after_delivery = crash_after_delivery

    async def deliver(self, event: PendingEvent) -> bool:
        """Record the event downstream. True if this was the first delivery.

        ``RETURNING event_id`` is what makes the answer trustworthy: with
        ``ON CONFLICT DO NOTHING`` a colliding insert affects no rows and returns
        nothing, so an empty result *is* the duplicate signal, read from the
        database rather than inferred.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _INSERT_DELIVERY_SQL,
                    {
                        "event_id": event.id,
                        "event_type": event.event_type,
                        "payload": json.dumps(
                            event.payload, separators=(",", ":"), sort_keys=True
                        ),
                    },
                )
            ).first()
            # Committed here, independently, before this returns. Everything after
            # this point on the publisher's side can fail without un-delivering it.
            await session.commit()

        if self.crash_after_delivery:
            raise SimulatedWorkerCrash(
                f"simulated worker crash after delivering event {event.id}"
            )

        return row is not None

    async def count(self) -> int:
        async with self._session_factory() as session:
            return int((await session.execute(_COUNT_DELIVERIES_SQL)).scalar_one())


class PublishOutcome(StrEnum):
    """What one publish attempt did."""

    #: The consumer had not seen this event. It acted on it.
    DELIVERED = "delivered"

    #: The consumer had seen this event already -- an earlier attempt got through
    #: and died before it could be marked. The delivery was suppressed by the
    #: consumer's primary key, which is the exactly-once effect being observed
    #: rather than assumed.
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"

    #: The sink raised. The event stays pending, `attempts` is incremented, and the
    #: next pass tries again.
    FAILED = "failed"


@dataclass
class PublishReport:
    """Counts from one pass, so a publish can be asserted on rather than trusted."""

    outcomes: dict[PublishOutcome, int] = field(default_factory=dict)

    def record(self, outcome: PublishOutcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    @property
    def examined(self) -> int:
        return sum(self.outcomes.values())

    @property
    def published(self) -> int:
        """Events that left 'pending' because of this pass.

        A suppressed duplicate counts: the event *is* published now, and the row is
        marked as such. What was suppressed was a second effect, not the publication.
        """
        return sum(
            count
            for outcome, count in self.outcomes.items()
            if outcome
            in {PublishOutcome.DELIVERED, PublishOutcome.DUPLICATE_SUPPRESSED}
        )

    def __str__(self) -> str:
        if not self.outcomes:
            return "nothing to publish"
        return ", ".join(
            f"{outcome.value}={count}" for outcome, count in sorted(self.outcomes.items())
        )


async def publish_once(
    session_factory: async_sessionmaker[AsyncSession],
    sink: EventSink,
    *,
    batch_size: int | None = None,
) -> PublishReport:
    """Drain up to ``batch_size`` pending events, and report what happened.

    One event, one transaction, one commit, so a pass that dies halfway keeps every
    publication it has already made. The worker is itself crash-safe, which it had
    better be, being the thing that makes crashes survivable.

    ``batch_size`` bounds the pass rather than the backlog: a pass stops when it has
    handled that many events, and the next pass picks up where it left off. A pass
    that ran until the table was empty would never terminate under sustained load,
    which is a problem for ``--once`` and for the operator who wants to stop it.
    """
    limit = settings.OUTBOX_BATCH_SIZE if batch_size is None else batch_size
    report = PublishReport()
    # Events this pass has already tried and failed. Excluded from further claims
    # here, because a failed delivery rolls its transaction back and leaves the
    # event immediately claimable again -- without this the pass would re-claim the
    # same undeliverable event until it hit the batch limit, spinning on it and
    # starving everything behind it. The next pass starts with an empty set and
    # tries again, which is exactly right: the reason to skip it expires with the
    # attempt that produced it.
    failed: list[uuid.UUID] = []

    while report.examined < limit:
        async with session_factory() as session:
            event = await claim_next_pending(session, skip=failed)
            if event is None:
                # Empty, or every pending event is held by another worker. Either
                # way there is nothing for this pass to do.
                break

            try:
                first_delivery = await sink.deliver(event)
            except Exception:
                # The sink refused. The transaction is abandoned, so the event stays
                # pending and will be tried again -- which is the correct response to
                # essentially every delivery failure, and is available only because
                # the event is a durable row rather than a message in flight.
                #
                # SimulatedWorkerCrash is a BaseException and is deliberately not
                # caught here; it propagates out of this function exactly as a real
                # process death would leave it uncaught.
                logger.exception("delivery of event %s failed", event.id)
                await session.rollback()
                report.record(PublishOutcome.FAILED)
                failed.append(event.id)
                await _record_attempt(session_factory, event.id)
                continue

            await mark_published(session, event.id)
            await session.commit()

        outcome = (
            PublishOutcome.DELIVERED
            if first_delivery
            else PublishOutcome.DUPLICATE_SUPPRESSED
        )
        report.record(outcome)
        logger.info("event %s -> %s", event.id, outcome.value)

    return report


async def _record_attempt(
    session_factory: async_sessionmaker[AsyncSession], event_id: uuid.UUID
) -> None:
    """Increment ``attempts`` on a fresh session, and commit it.

    Fresh because the transaction that tried to deliver has just been rolled back,
    and the record of having tried must not go down with it. Best-effort: if even
    this fails, the event is still pending and still correct, just with an
    understated attempt count. A failure to write a diagnostic must never become a
    failure to publish.
    """
    try:
        async with session_factory() as session:
            await record_failed_attempt(session, event_id)
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("could not record a failed attempt for event %s", event_id)


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    sink: EventSink,
    *,
    interval_seconds: int | None = None,
) -> None:  # pragma: no cover - exercised by the smoke script, not the suite
    """Publish on an interval until interrupted.

    Polling rather than ``LISTEN``/``NOTIFY``, and the reason is durability again: a
    notification is delivered to whoever is connected at that moment, so a worker
    that is restarting when an event lands never hears about it. A poll finds
    everything pending regardless of who was awake when it was written. ``NOTIFY``
    is a fine *latency* optimisation on top of a poll, and a disastrous replacement
    for one.
    """
    interval = settings.OUTBOX_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    logger.info("publisher started: draining the outbox every %ss", interval)

    while True:
        try:
            report = await publish_once(session_factory, sink)
            if report.examined:
                logger.info("pass: %s", report)
        except Exception:
            # A pass that dies must not take the loop with it. The next pass sees
            # the same backlog and tries again; the events are durable, which is the
            # entire premise.
            logger.exception("publish pass failed, continuing")

        await asyncio.sleep(interval)


async def _print_status(
    session_factory: async_sessionmaker[AsyncSession], sink: DeliveryLog
) -> None:
    """Print outbox and delivery counts as JSON on stdout.

    JSON because the caller is ``scripts/smoke_phase5.ps1``, and a PowerShell 5.1
    script parsing a human-readable line is a script that breaks the day somebody
    improves the wording.
    """
    async with session_factory() as session:
        pending, published = await counts(session)

    print(
        json.dumps(
            {"pending": pending, "published": published, "delivered": await sink.count()}
        )
    )


async def _main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        prog="python -m app.publisher",
        description="Drain the transactional outbox into the downstream sink.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain one batch and exit, instead of looping. Used by CI and the smoke script.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print {pending, published, delivered} as JSON and exit, publishing nothing.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=None, help="most events one pass will publish."
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=None, help="seconds between passes when looping."
    )
    args = parser.parse_args()

    # Imported here rather than at module scope so that importing this module for
    # its functions -- which the tests do -- does not build an engine as a side
    # effect.
    from app.db import async_session, engine

    sink = DeliveryLog(async_session)

    try:
        if args.status:
            # No logging configured on this path: stdout must carry the JSON and
            # nothing else, or the smoke script's ConvertFrom-Json chokes on a
            # startup banner.
            await _print_status(async_session, sink)
            return

        # Structured, same as the web process. A worker whose logs are shaped
        # differently from the API's is a worker whose logs get dropped by the ingester
        # -- and these three are exactly the processes nobody is watching when they
        # matter, so their output has to survive the pipeline unassisted.
        configure_logging(log_format=settings.LOG_FORMAT, level=settings.LOG_LEVEL)

        if args.once:
            report = await publish_once(async_session, sink, batch_size=args.batch_size)
            logger.info("pass: %s", report)
        else:
            await run_forever(async_session, sink, interval_seconds=args.interval_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover - CLI
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
