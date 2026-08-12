"""The sweep that finishes charges whose requests did not.

This module is the second half of Phase 5a. The first half -- committing the
payment as ``processing`` before the processor is called, in
``app/routers/charges.py`` -- makes a crashed charge *detectable*. This is what
makes it *resolved*.

## What it is for

A payment sitting in ``processing`` means one of two things, and the row cannot
tell you which:

1. a charge is in flight right now and will settle itself in a moment;
2. the request that created it died, and nobody is coming back for it.

Waiting distinguishes them. A healthy charge leaves ``processing`` in the time one
authorisation takes; one that is still there minutes later has been abandoned.
``RECONCILE_STUCK_AFTER_SECONDS`` is where that line is drawn, and it is a
threshold on *our* crash recovery rather than a timeout on the processor -- setting
it too low does not break correctness, it just means the sweep sometimes contends
with a request that was going to finish anyway, and the lock and the status
re-check handle that safely.

## How it resolves one

**The processor is the authority.** Ledgerline does not guess, does not assume, and
does not decide a stuck payment failed because it looks old. It asks::

    result = await processor.lookup(payment.id)

and there are exactly three answers, each with one correct action:

| The processor says         | What it means             | Settled as                       |
| -------------------------- | ------------------------- | -------------------------------- |
| success                    | the card was charged      | `succeeded`, posting written now |
| failure                    | the card declined         | `failed`                         |
| nothing -- no such attempt | the call never reached it | `failed`                         |

The third row is the subtle one and it is safe for a specific reason: if the
processor has no record of the attempt, no money moved, so recording a failure
cannot be wrong about anyone's balance. It relies on the processor's records being
authoritative and complete, which is the same thing every reconciliation in every
payments system relies on.

The first row is where the phase pays for itself. The card was charged, the
original request died before it could write anything, and the posting is
constructed *here*, minutes later, from the processor's own record. The money
appears in the ledger exactly once, with the same two-legged balanced posting the
charge route would have written, and the customer's retry gets the recorded answer.

## Why each payment gets its own transaction

One payment, one transaction, one commit. A batch-wide transaction would mean a
single unsettleable payment rolls back the settlement of every other payment in the
batch, and would hold row locks across every processor lookup in it. The sweep is a
queue worker, and queue workers commit per item.

``SELECT ... FOR UPDATE SKIP LOCKED`` is what makes several of them safe to run at
once: they divide the backlog instead of contending over it. See app/locking.py.

## What this guarantees, precisely

* **No charge accepted by the processor stays invisible.** If the processor took
  the money, the sweep will find the payment, read that fact from the processor's
  records, and write the posting. Eventually, not instantly.
* **Bounded, not eliminated.** A payment can be unresolved for up to
  ``RECONCILE_STUCK_AFTER_SECONDS`` plus one sweep interval. During that window the
  truth exists and is discoverable; it has simply not been discovered.
* **At-least-once resolution with an exactly-once effect.** Running the sweep
  twice, or ten times, or concurrently with the original request finishing, settles
  the payment once. Three independent things enforce that: the row lock, the
  ``status = 'processing'`` predicate inside it, and the state machine, which
  refuses a second move out of a terminal state.

And what it does not give:

* **It is not a replacement for the processor having a lookup API.** Everything
  here rests on being able to ask about an attempt by reference. Against a
  processor with no such API this module cannot be written, and the gap can only be
  narrowed by shrinking the window, never closed.
* **It does not run itself.** Durability buys the ability to recover; something
  still has to perform the recovery. Run ``python -m app.reconcile``.
* **It does not detect a processor that is wrong**, only one that disagrees with
  us. Reconciliation against a corrupted source of truth reproduces the corruption.
"""

import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.idempotency import finalize_key, unfinished_key_for_payment
from app.ledger import LedgerInvariantError, settlement_account_id, write_posting
from app.locking import try_lock_payment_for_settlement
from app.models import Payment, PaymentStatus
from app.payments import transition
from app.processor import ProcessorAdapter
from app.schemas import ChargeOut

logger = logging.getLogger("ledgerline.reconcile")

# Ordered oldest first: the payment that has been unresolved longest is the one
# whose customer has been waiting longest.
#
# The age test is on `created_at` rather than `updated_at`, and that is a choice
# worth one line. A payment is inserted and moved to 'processing' inside the same
# transaction, microseconds apart, so the two are interchangeable as a measure of
# "how long has this been unsettled". `created_at` wins because it is immutable:
# `updated_at` moves whenever anything touches the row, so a payment that is
# repeatedly poked would keep resetting its own age and could evade the sweep
# forever.
_FIND_STUCK_SQL = text(
    """
    SELECT id
    FROM payments
    WHERE status = 'processing'
      AND created_at < now() - (:stuck_after_seconds * interval '1 second')
    ORDER BY created_at
    LIMIT :batch_size
    """
)


class ReconcileOutcome(StrEnum):
    """What one reconciliation attempt did. Every value is a normal day."""

    #: The processor had charged the card. The posting was written here.
    SETTLED_SUCCEEDED = "settled_succeeded"

    #: The processor had declined. No money moved, then or now.
    SETTLED_FAILED = "settled_failed"

    #: The processor never saw the attempt, so nothing was charged. Settled as
    #: failed on the processor's authority rather than on a guess.
    NO_PROCESSOR_RECORD = "no_processor_record"

    #: Someone else got there first -- another sweep, or the original request
    #: finishing after all. The row was locked or no longer 'processing'.
    SKIPPED = "skipped"

    #: The payment could not be settled and is left in 'processing' for the next
    #: pass. The only value here that should ever prompt a look at the logs.
    FAILED_TO_SETTLE = "failed_to_settle"


@dataclass
class SweepReport:
    """Counts from one pass, so a sweep can be asserted on rather than trusted."""

    outcomes: dict[ReconcileOutcome, int] = field(default_factory=dict)

    def record(self, outcome: ReconcileOutcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    @property
    def examined(self) -> int:
        return sum(self.outcomes.values())

    @property
    def settled(self) -> int:
        """Payments that left 'processing' because of this pass."""
        return sum(
            count
            for outcome, count in self.outcomes.items()
            if outcome
            in {
                ReconcileOutcome.SETTLED_SUCCEEDED,
                ReconcileOutcome.SETTLED_FAILED,
                ReconcileOutcome.NO_PROCESSOR_RECORD,
            }
        )

    def __str__(self) -> str:
        if not self.outcomes:
            return "nothing to reconcile"
        return ", ".join(
            f"{outcome.value}={count}" for outcome, count in sorted(self.outcomes.items())
        )


async def find_stuck_payments(
    session: AsyncSession, *, stuck_after_seconds: int, batch_size: int
) -> list[uuid.UUID]:
    """Payment ids that have been in 'processing' longer than the threshold.

    Deliberately takes no locks. This is a candidate list, not a work claim -- by
    the time any of these is acted on it may already have been settled by someone
    else, and :func:`reconcile_payment` re-checks under a lock rather than trusting
    what this returned. Locking here instead would hold every row in the batch for
    the length of every processor lookup in it.
    """
    rows = await session.execute(
        _FIND_STUCK_SQL,
        {"stuck_after_seconds": stuck_after_seconds, "batch_size": batch_size},
    )
    return [row.id for row in rows]


async def _finalize_abandoned_key(session: AsyncSession, payment: Payment) -> None:
    """Give the crashed request's idempotency key the answer it never got.

    Without this the money is fixed and the customer is not: their key was consumed
    by the request that died, so every retry gets a 409 until the 24-hour TTL
    expires -- a day-long lockout caused entirely by a crash on our side. Finalising
    it here means the next retry replays the reconciled outcome, which is exactly
    what the original request would have returned had it lived.

    Silent when there is no such key, which is the common case rather than an edge
    one: the key may have completed already, or expired, or never existed at all on
    the naive claim path.
    """
    key = await unfinished_key_for_payment(session, payment.id)
    if key is None:
        return

    await session.refresh(payment)
    body = ChargeOut.model_validate(payment).model_dump(mode="json")
    # 201, matching what POST /charges returns for both outcomes. A replay should
    # be indistinguishable from the response the original request would have sent.
    await finalize_key(session, key, body, 201)


async def reconcile_payment(
    session: AsyncSession, processor: ProcessorAdapter, payment_id: uuid.UUID
) -> ReconcileOutcome:
    """Settle one abandoned payment against the processor's records.

    The caller owns the transaction and must commit it. That follows the same
    convention as ``write_posting`` and the charge route: this function arranges the
    rows, and the decision to make them durable belongs to whoever knows the whole
    unit of work.
    """
    # The lock and the 'is it still processing?' test are one statement, so no
    # other settlement can slip between them. Everything below runs under it.
    if not await try_lock_payment_for_settlement(session, payment_id):
        return ReconcileOutcome.SKIPPED

    payment = await session.get(Payment, payment_id)
    if payment is None:  # pragma: no cover - the lock just returned this row
        return ReconcileOutcome.SKIPPED

    # Ask. Do not assume, do not infer from the age of the row.
    result = await processor.lookup(payment_id)

    if result is None:
        # The processor has never heard of this attempt, so the call died before
        # reaching it and no money moved. Settling as failed is safe on the
        # processor's authority -- and leaving it in 'processing' forever would be
        # the worse answer, since it is a state nobody can act on.
        payment.failure_reason = (
            "reconciled: the processor has no record of this attempt, so no money "
            "moved. The charge did not reach the processor before this request was "
            "lost."
        )
        transition(payment, PaymentStatus.FAILED)
        outcome = ReconcileOutcome.NO_PROCESSOR_RECORD

    elif result.succeeded:
        # The card was charged and the original request never got to say so. Write
        # the posting now, from the processor's record, exactly as the charge route
        # would have written it.
        try:
            house_id = await settlement_account_id(session, payment.currency)
            ledger_transaction = await write_posting(
                session,
                description=f"charge {payment.id} (reconciled)",
                debit_account_id=house_id,
                credit_account_id=payment.account_id,
                amount=payment.amount,
            )
        except LedgerInvariantError:  # pragma: no cover - defensive
            # Leave it in 'processing'. A payment we cannot post correctly stays
            # visibly unresolved rather than being marked failed for a card that
            # was genuinely charged, which would be a lie that balances.
            logger.exception("payment %s could not be posted during reconciliation", payment_id)
            await session.rollback()
            return ReconcileOutcome.FAILED_TO_SETTLE

        payment.processor_ref = result.processor_ref
        payment.ledger_transaction_id = ledger_transaction.id
        transition(payment, PaymentStatus.SUCCEEDED)
        outcome = ReconcileOutcome.SETTLED_SUCCEEDED

    else:
        payment.processor_ref = result.processor_ref
        payment.failure_reason = result.failure_reason
        transition(payment, PaymentStatus.FAILED)
        outcome = ReconcileOutcome.SETTLED_FAILED

    await session.flush()
    await _finalize_abandoned_key(session, payment)
    return outcome


async def sweep_once(
    session_factory: async_sessionmaker[AsyncSession],
    processor: ProcessorAdapter,
    *,
    stuck_after_seconds: int | None = None,
    batch_size: int | None = None,
) -> SweepReport:
    """Run one reconciliation pass and report what it did.

    One transaction per payment, committed as it goes, so a pass that dies halfway
    keeps everything it has already settled -- the sweep is itself crash-safe, which
    it had better be, being the crash recovery.
    """
    stuck_after = (
        settings.RECONCILE_STUCK_AFTER_SECONDS
        if stuck_after_seconds is None
        else stuck_after_seconds
    )
    limit = settings.RECONCILE_BATCH_SIZE if batch_size is None else batch_size

    async with session_factory() as session:
        candidates = await find_stuck_payments(
            session, stuck_after_seconds=stuck_after, batch_size=limit
        )

    report = SweepReport()
    for payment_id in candidates:
        # A fresh session per payment: its own connection, its own transaction, its
        # own commit. One payment's failure cannot take another's settlement with it.
        async with session_factory() as session:
            try:
                outcome = await reconcile_payment(session, processor, payment_id)
                await session.commit()
            except Exception:
                logger.exception("reconciliation of payment %s failed", payment_id)
                await session.rollback()
                outcome = ReconcileOutcome.FAILED_TO_SETTLE

        report.record(outcome)
        if outcome is not ReconcileOutcome.SKIPPED:
            logger.info("payment %s -> %s", payment_id, outcome.value)

    return report


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    processor: ProcessorAdapter,
    *,
    interval_seconds: int | None = None,
) -> None:  # pragma: no cover - exercised by the smoke script, not the suite
    """Sweep on an interval until interrupted.

    Deliberately a separate process (``python -m app.reconcile``) rather than a task
    inside the API. Recovery that runs in the process being recovered from is
    recovery that is unavailable exactly when it is needed -- if the web process is
    dead or crash-looping, the payments it abandoned are the ones waiting.
    """
    interval = (
        settings.RECONCILE_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    )
    logger.info(
        "reconciler started: every %ss, settling payments stuck in 'processing' for %ss",
        interval,
        settings.RECONCILE_STUCK_AFTER_SECONDS,
    )

    while True:
        try:
            report = await sweep_once(session_factory, processor)
            if report.examined:
                logger.info("sweep: %s", report)
        except Exception:
            # A sweep that dies must not take the loop with it. The next pass sees
            # the same backlog and tries again; the payments are durable, which is
            # the entire premise.
            logger.exception("sweep failed, continuing")

        await asyncio.sleep(interval)


async def _main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        prog="python -m app.reconcile",
        description="Settle payments abandoned in 'processing' against the processor.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single pass and exit, instead of looping. Used by CI and the smoke script.",
    )
    parser.add_argument(
        "--stuck-after-seconds",
        type=int,
        default=None,
        help=(
            "override RECONCILE_STUCK_AFTER_SECONDS for this run. 0 reconciles every "
            "payment currently in 'processing', including ones still legitimately in "
            "flight -- safe, because the lock and the status check settle who wins, "
            "but only sensible against an idle system."
        ),
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=None, help="seconds between passes when looping."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    # Imported here rather than at module scope so that importing this module for
    # its functions -- which the tests do -- does not build an engine as a side
    # effect.
    from app.db import async_session, engine
    from app.deps import processor_books
    from app.processor import FakeProcessor

    # Only `lookup` is ever called on this, so the configured outcome and latency
    # are irrelevant. What matters is that it has the books.
    processor = FakeProcessor(books=processor_books)

    try:
        if args.once:
            report = await sweep_once(
                async_session, processor, stuck_after_seconds=args.stuck_after_seconds
            )
            logger.info("sweep: %s", report)
        else:
            await run_forever(
                async_session, processor, interval_seconds=args.interval_seconds
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover - CLI
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
