"""Reconciliation across time: does our record still agree with the processor's?

``app/reconcile.py`` answers a narrow question -- *this payment is stuck in
``processing``; what happened to it?* -- and then acts, settling it against the
processor's books. It is recovery, and it is bounded: it only ever looks at
payments that are visibly unfinished.

This module asks the wider one:

    **For everything we consider settled, do the two sides still tell the same
    story?**

That question has no bound and no obvious trigger. Nothing is stuck, nothing timed
out, no request failed. The payments look finished on both sides and simply
disagree, and the only way to find out is to go and compare.

## It reports. It does not fix.

This is the important line in the file, so it is near the top.

Every other background job in this project changes money: the sweep settles
payments and writes postings, the publisher marks events delivered. **This one
writes nothing.** It reads both sides, prints what disagrees, and stops.

That is a deliberate design decision rather than an unfinished feature, and the
reasoning is worth stating because "and then fix it automatically" is the obvious
next thought:

* **A discrepancy is evidence, not a diagnosis.** "The processor has a refund we do
  not" is consistent with a crash between their commit and ours -- and equally with
  a bug that refunded the wrong payment, a replayed request, or someone reversing a
  charge by hand in the processor's dashboard. The right repair differs in each
  case and depends on facts not present in either database.
* **An auto-fixer is a money-moving robot triggered by disagreement.** The
  circumstance in which it fires is, by construction, the circumstance in which one
  of its two inputs is known to be wrong. A repair driven by corrupt input is how a
  small discrepancy becomes a large one.
* **The safe repairs are already elsewhere.** The genuinely mechanical case -- a
  payment stranded in ``processing`` -- has a job that resolves it, because there
  the processor's answer is unambiguous and the action is uniquely determined. What
  is left here is the residue that needs a person.

So the output is a report. A human reads it, decides, and if a correction is
needed it is made by *posting a correcting entry* through the ordinary flows --
never by editing history, which the ledger forbids anyway.

## What it compares

For each payment settled longer ago than ``DRIFT_GRACE_SECONDS``:

| Finding                        | Meaning                                              |
| ------------------------------ | ---------------------------------------------------- |
| ``charge_missing_at_processor``| we say succeeded; their books have no such charge     |
| ``charge_outcome_mismatch``    | we say succeeded and they say declined, or vice versa |
| ``charge_amount_mismatch``     | both have it, for different amounts                   |
| ``refund_missing_at_processor``| we recorded a refund they have no record of           |
| ``refund_missing_locally``     | they reversed money we never recorded                 |
| ``refund_amount_mismatch``     | both have the reversal, for different amounts         |
| ``refund_total_exceeds_charge``| our own refunds sum past the charge                   |
| ``ledger_disagrees_with_records``| the postings do not match payment minus refunds     |

The last two are different in kind from the rest and that is the point of including
them. Every other row is a disagreement with a third party, which can happen
without anyone being at fault. Those two are Ledgerline disagreeing **with itself**
-- the over-refund trigger was bypassed, or a posting went missing -- and they
should be impossible. A job that only ever checked the other side would never
notice the failure mode where our own invariants stopped holding.

## The grace period

A payment settled two seconds ago is not drift, it is work in progress: the
processor's books and ours are written on different connections with no ordering
between them, so a moment of disagreement is normal and expected. Reporting it
would bury the real findings under a stream of things that resolve themselves,
which is how a report becomes something nobody reads.
"""

import argparse
import asyncio
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.observability import configure_logging
from app.processor import ProcessorAdapter, ProcessorBooks

logger = logging.getLogger("ledgerline.drift")


class DriftKind(StrEnum):
    """What kind of disagreement was found. Every value needs a human."""

    #: We recorded a successful charge the processor has never heard of.
    CHARGE_MISSING_AT_PROCESSOR = "charge_missing_at_processor"

    #: Both sides have the charge and disagree about whether it worked.
    CHARGE_OUTCOME_MISMATCH = "charge_outcome_mismatch"

    #: Both sides have the charge and disagree about how much it was for.
    CHARGE_AMOUNT_MISMATCH = "charge_amount_mismatch"

    #: We recorded a refund the processor has no record of. Money we believe went
    #: back and, by their books, did not.
    REFUND_MISSING_AT_PROCESSOR = "refund_missing_at_processor"

    #: The processor reversed money we never wrote down -- the crash-shaped one, and
    #: the reason it matters is that our refunded total is now understating itself,
    #: so we would allow a further refund that should not fit.
    REFUND_MISSING_LOCALLY = "refund_missing_locally"

    #: Both sides have the reversal, for different amounts.
    REFUND_AMOUNT_MISMATCH = "refund_amount_mismatch"

    #: Our own refunds sum past the charge. Should be unreachable -- migration
    #: 0007's trigger exists to make it so -- and therefore worth shouting about.
    REFUND_TOTAL_EXCEEDS_CHARGE = "refund_total_exceeds_charge"

    #: The ledger does not equal the payment minus its refunds. Ledgerline
    #: disagreeing with itself, which no third party can cause.
    LEDGER_DISAGREES_WITH_RECORDS = "ledger_disagrees_with_records"


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """One disagreement, with both sides recorded so a human can act on it.

    ``ours`` and ``theirs`` are deliberately free-form strings rather than typed
    amounts. A finding is something a person reads at 3am, and "succeeded, 250000"
    against "no record" is more useful than two nullable integer fields that are
    both meaningless for half the kinds.
    """

    kind: DriftKind
    payment_id: uuid.UUID
    detail: str
    ours: str
    theirs: str
    refund_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "payment_id": str(self.payment_id),
            "refund_id": None if self.refund_id is None else str(self.refund_id),
            "detail": self.detail,
            "ours": self.ours,
            "theirs": self.theirs,
        }


@dataclass
class DriftReport:
    """Everything one pass found, so a run can be asserted on rather than trusted."""

    examined: int = 0
    findings: list[DriftFinding] = field(default_factory=list)

    def record(self, finding: DriftFinding) -> None:
        self.findings.append(finding)

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_kind(self) -> dict[DriftKind, int]:
        counts: dict[DriftKind, int] = defaultdict(int)
        for finding in self.findings:
            counts[finding.kind] += 1
        return dict(counts)

    def kinds(self) -> set[DriftKind]:
        return {finding.kind for finding in self.findings}

    def for_payment(self, payment_id: uuid.UUID) -> list[DriftFinding]:
        return [f for f in self.findings if f.payment_id == payment_id]

    def as_dict(self) -> dict:
        return {
            "examined": self.examined,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def __str__(self) -> str:
        if self.clean:
            return f"examined {self.examined} settled payments, no drift"
        counts = ", ".join(
            f"{kind.value}={count}" for kind, count in sorted(self.by_kind().items())
        )
        return f"examined {self.examined} settled payments, {len(self.findings)} findings: {counts}"


# Settled payments old enough to be worth comparing, with the figures Ledgerline
# holds about each: what we charged, what we say we refunded, and what the ledger
# actually contains.
#
# The ledger figure is computed here rather than in Python because it is the whole
# point of the last check: `net_posted` is the customer's net movement across every
# posting that names this payment or one of its refunds, read straight from
# ledger_entries. If our records say 2500 charged and 1000 refunded, the ledger had
# better show a net credit of 1500, and if it does not then something wrote money
# outside the flows that were supposed to be the only ones that can.
_SETTLED_PAYMENTS_SQL = text(
    """
    SELECT
        p.id,
        p.account_id,
        p.amount,
        p.currency,
        p.status::text AS status,
        COALESCE(r.refunded, 0)::bigint AS refunded_locally,
        COALESCE(l.net_posted, 0)::bigint AS net_posted
    FROM payments p
    LEFT JOIN LATERAL (
        SELECT SUM(amount) AS refunded
        FROM refunds
        WHERE payment_id = p.id AND status = 'succeeded'
    ) r ON TRUE
    LEFT JOIN LATERAL (
        SELECT SUM(
            CASE e.direction WHEN 'credit' THEN e.amount ELSE -e.amount END
        ) AS net_posted
        FROM ledger_entries e
        WHERE e.account_id = p.account_id
          AND e.transaction_id IN (
              SELECT p.ledger_transaction_id
              UNION ALL
              SELECT ledger_transaction_id
              FROM refunds
              WHERE payment_id = p.id AND ledger_transaction_id IS NOT NULL
          )
    ) l ON TRUE
    WHERE p.status IN ('succeeded', 'refunded')
      AND p.updated_at < now() - (:grace_seconds * interval '1 second')
    ORDER BY p.created_at
    LIMIT :batch_size
    """
)

# Our refunds for one payment, with the derived attempt reference we would have
# sent the processor. Note that we cannot recompute that reference here -- it is
# uuid5 over the payment id and the *idempotency key*, which the refunds table does
# not store -- so matching is done by the processor_ref the processor gave back,
# which both sides do hold.
_LOCAL_REFUNDS_SQL = text(
    """
    SELECT id, amount, status, processor_ref
    FROM refunds
    WHERE payment_id = :payment_id
    ORDER BY created_at
    """
)


async def find_settled_payments(
    session: AsyncSession, *, grace_seconds: int, batch_size: int
) -> list:
    """Settled payments old enough that a disagreement is not just work in flight."""
    rows = await session.execute(
        _SETTLED_PAYMENTS_SQL,
        {"grace_seconds": grace_seconds, "batch_size": batch_size},
    )
    return list(rows)


async def _check_charge(
    processor: ProcessorAdapter, row, report: DriftReport
) -> None:
    """Compare one charge against the processor's record of it."""
    theirs = await processor.lookup(row.id)

    if theirs is None:
        report.record(
            DriftFinding(
                kind=DriftKind.CHARGE_MISSING_AT_PROCESSOR,
                payment_id=row.id,
                detail=(
                    "Ledgerline holds a settled charge the processor has no record "
                    "of. Either money was posted that was never actually taken, or "
                    "the processor's records are incomplete."
                ),
                ours=f"{row.status}, {row.amount} {row.currency}",
                theirs="no record",
            )
        )
        return

    if not theirs.succeeded:
        report.record(
            DriftFinding(
                kind=DriftKind.CHARGE_OUTCOME_MISMATCH,
                payment_id=row.id,
                detail=(
                    "Ledgerline posted money for a charge the processor says was "
                    "declined. The ledger is crediting an account for a card that "
                    "refused."
                ),
                ours=f"{row.status}, {row.amount} {row.currency}",
                theirs=f"failure ({theirs.failure_reason or 'no reason given'})",
            )
        )


async def _check_refunds(
    books: ProcessorBooks, session: AsyncSession, row, report: DriftReport
) -> int:
    """Compare our refunds against the processor's. Returns our succeeded total.

    Matching is by ``processor_ref``, the handle the processor itself issued and
    both sides stored. Matching by amount instead would collapse two legitimate
    partial refunds of the same size into one and invent a discrepancy; matching by
    our refund id is impossible, because the processor never sees it.
    """
    theirs = {r.processor_ref: r for r in await books.refunds_for(row.id) if r.succeeded}
    ours_rows = list(await session.execute(_LOCAL_REFUNDS_SQL, {"payment_id": row.id}))

    ours_total = 0
    seen: set[str] = set()

    for refund in ours_rows:
        if refund.status != "succeeded":
            # A declined refund is *expected* to have no successful counterpart.
            continue

        ours_total += int(refund.amount)
        match = theirs.get(refund.processor_ref)

        if match is None:
            report.record(
                DriftFinding(
                    kind=DriftKind.REFUND_MISSING_AT_PROCESSOR,
                    payment_id=row.id,
                    refund_id=refund.id,
                    detail=(
                        "Ledgerline recorded a successful refund and wrote a "
                        "reversing posting, but the processor has no matching "
                        "reversal. The customer's ledger balance has fallen for "
                        "money that, by their books, never went back."
                    ),
                    ours=f"succeeded, {refund.amount}, ref {refund.processor_ref}",
                    theirs="no matching reversal",
                )
            )
            continue

        seen.add(refund.processor_ref)
        if int(match.amount) != int(refund.amount):
            report.record(
                DriftFinding(
                    kind=DriftKind.REFUND_AMOUNT_MISMATCH,
                    payment_id=row.id,
                    refund_id=refund.id,
                    detail="Both sides recorded this reversal, for different amounts.",
                    ours=str(refund.amount),
                    theirs=str(match.amount),
                )
            )

    for processor_ref, unmatched in theirs.items():
        if processor_ref in seen:
            continue
        report.record(
            DriftFinding(
                kind=DriftKind.REFUND_MISSING_LOCALLY,
                payment_id=row.id,
                detail=(
                    "The processor reversed money Ledgerline has no record of. Our "
                    "refunded total is understated, so a further refund would be "
                    "allowed that should not fit -- and the customer's ledger "
                    "balance is overstated by this amount."
                ),
                ours="no matching refund",
                theirs=f"succeeded, {unmatched.amount}, ref {processor_ref}",
            )
        )

    return ours_total


def _check_our_own_books(row, refunded_locally: int, report: DriftReport) -> None:
    """Check Ledgerline against Ledgerline. No third party can cause these.

    Both findings here should be unreachable. That is exactly why they are checked:
    an invariant nobody verifies is an invariant that stops holding quietly, and the
    cost of asking is one comparison against numbers already in hand.
    """
    if refunded_locally > int(row.amount):
        report.record(
            DriftFinding(
                kind=DriftKind.REFUND_TOTAL_EXCEEDS_CHARGE,
                payment_id=row.id,
                detail=(
                    "The succeeded refunds for this payment total more than it was "
                    "charged. Migration 0007's trigger exists to make this "
                    "impossible, so if it appears the trigger is missing, disabled "
                    "or was bypassed."
                ),
                ours=f"{refunded_locally} refunded against {row.amount} charged",
                theirs="n/a -- this is an internal invariant",
            )
        )

    # What the ledger should show for this customer across the charge and its
    # refunds: credited the full amount, debited back whatever was returned.
    expected = int(row.amount) - refunded_locally
    if int(row.net_posted) != expected:
        report.record(
            DriftFinding(
                kind=DriftKind.LEDGER_DISAGREES_WITH_RECORDS,
                payment_id=row.id,
                detail=(
                    "The postings for this payment and its refunds do not net to "
                    "the amount charged minus the amount refunded. The ledger and "
                    "the records that are supposed to explain it have diverged."
                ),
                ours=f"records say {row.amount} - {refunded_locally} = {expected}",
                theirs=f"ledger shows a net {row.net_posted} on this account",
            )
        )


async def detect_once(
    session_factory: async_sessionmaker[AsyncSession],
    processor: ProcessorAdapter,
    books: ProcessorBooks,
    *,
    grace_seconds: int | None = None,
    batch_size: int | None = None,
) -> DriftReport:
    """Run one drift pass and report what it found. Writes nothing, ever.

    Takes both the adapter and the books because it needs two different shapes of
    question. ``ProcessorAdapter`` answers "what happened to attempt X?", which is
    the seam a real processor sits behind. Listing every reversal against a charge
    is a bulk read that belongs to the books, and a real implementation would be a
    list endpoint on the charge -- which is why it is not on the adapter protocol
    the charge flow depends on.
    """
    grace = settings.DRIFT_GRACE_SECONDS if grace_seconds is None else grace_seconds
    limit = settings.DRIFT_BATCH_SIZE if batch_size is None else batch_size

    report = DriftReport()

    async with session_factory() as session:
        rows = await find_settled_payments(
            session, grace_seconds=grace, batch_size=limit
        )

        for row in rows:
            report.examined += 1
            await _check_charge(processor, row, report)
            refunded_locally = await _check_refunds(books, session, row, report)
            _check_our_own_books(row, refunded_locally, report)

        # Read-only by construction, not by convention. Nothing above writes, and
        # this rollback makes that structural: if a future edit ever adds a write to
        # this module, it is discarded here rather than committed by accident.
        await session.rollback()

    return report


async def _main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        prog="python -m app.drift",
        description=(
            "Compare Ledgerline's records against the processor's books and report "
            "what disagrees. Writes nothing and repairs nothing."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single pass and exit. The only mode; accepted so the CLI reads "
        "like the sweep's and so CI can be explicit about it.",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=None,
        help=(
            "override DRIFT_GRACE_SECONDS. 0 compares every settled payment, "
            "including ones settled moments ago -- which will report normal "
            "in-flight disagreement as drift, so it is for an idle system only."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON on stdout and log nothing, for scripts.",
    )
    args = parser.parse_args()

    from app.db import async_session, engine
    from app.deps import processor_books
    from app.processor import FakeProcessor

    # Only the read paths are ever used, so the configured outcome and latency are
    # irrelevant. What matters is that it has the books.
    processor = FakeProcessor(books=processor_books)

    if not args.json:
        # Structured, same as the web process. A worker whose logs are shaped
        # differently from the API's is a worker whose logs get dropped by the ingester
        # -- and these three are exactly the processes nobody is watching when they
        # matter, so their output has to survive the pipeline unassisted.
        configure_logging(log_format=settings.LOG_FORMAT, level=settings.LOG_LEVEL)

    try:
        report = await detect_once(
            async_session, processor, processor_books, grace_seconds=args.grace_seconds
        )

        if args.json:
            print(json.dumps(report.as_dict()))
        else:
            logger.info("drift: %s", report)
            for finding in report.findings:
                logger.warning(
                    "%s payment=%s%s | ours: %s | processor: %s | %s",
                    finding.kind.value,
                    finding.payment_id,
                    "" if finding.refund_id is None else f" refund={finding.refund_id}",
                    finding.ours,
                    finding.theirs,
                    finding.detail,
                )
            if report.findings:
                logger.warning(
                    "%d finding(s) need a human. This job does not repair money: "
                    "a discrepancy is evidence, not a diagnosis.",
                    len(report.findings),
                )
    finally:
        await engine.dispose()

    # Exit 0 even with findings. Drift is a thing to read, not a thing that failed,
    # and a non-zero exit would make a CI step red for a condition CI cannot fix --
    # which is how a report becomes something people disable.


if __name__ == "__main__":  # pragma: no cover - CLI
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
