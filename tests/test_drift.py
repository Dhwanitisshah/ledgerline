"""Phase 6: reconciliation across time -- does our record still match theirs?

The Phase 5a sweep asks a narrow question about payments that are *visibly*
unfinished, and then acts. This job asks the wide one about payments that look
finished on both sides, and then **reports**.

Every test here injects a discrepancy directly into one side's tables, because
that is the only way to produce drift: by construction, none of these states is
reachable through the API. That is the point. Drift is what is left after every
flow worked -- a crash between two commits, a hand-edit in a processor dashboard,
a bug that has since been fixed and left residue. If a test could produce one by
calling endpoints, it would be a bug in the endpoints rather than drift.

The last test is the one that matters most: **the job writes nothing.** It is the
only background job in the project that does not move money, and that restraint is
a design decision rather than an unimplemented feature -- see app/drift.py.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from app.db import async_session, engine
from app.deps import processor_books
from app.drift import DriftKind, detect_once
from app.processor import FakeProcessor
from tests.conftest import (
    count_rows,
    create_account,
    create_charge,
    refund_charge,
    scalar,
)

CHARGE = 250000


async def detect(*, grace_seconds: int = 0):
    """One drift pass over everything settled, however recently."""
    return await detect_once(
        async_session,
        FakeProcessor(books=processor_books),
        processor_books,
        grace_seconds=grace_seconds,
    )


async def charged(client: AsyncClient, amount: int = CHARGE) -> tuple[str, str]:
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, amount)
    return account, charge["id"]


async def execute(sql: str, params: dict | None = None) -> None:
    """Write straight to the database, bypassing every flow. How drift is injected."""
    async with engine.begin() as conn:
        await conn.execute(text(sql), params or {})


# --- The quiet case ----------------------------------------------------------------


async def test_a_healthy_system_reports_no_drift(client: AsyncClient) -> None:
    """Charges and refunds that went through the flows agree with the processor.

    The overwhelmingly common result, and worth pinning: a job that reported drift
    on correct data would be worse than no job, because the findings that matter
    would be buried in noise nobody reads.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000)

    report = await detect()

    assert report.examined == 1
    assert report.clean, report.findings


async def test_the_grace_period_ignores_freshly_settled_payments(
    client: AsyncClient,
) -> None:
    """A payment settled a second ago is work in progress, not drift.

    The processor's books and ours are written on different connections with no
    ordering between them, so a moment of disagreement is normal. With the default
    threshold this payment is not even examined.
    """
    await charged(client)

    report = await detect(grace_seconds=60)

    assert report.examined == 0
    assert report.clean


# --- Drift on the charge -------------------------------------------------------------


async def test_a_charge_the_processor_has_no_record_of_is_flagged(
    client: AsyncClient,
) -> None:
    """THE INJECTED DISCREPANCY: we posted money they have never heard of.

    Produced by deleting the processor's row, which stands in for their records
    being incomplete or for a charge that was recorded against the wrong reference.
    Ledgerline has credited an account and cannot point at anything on their side
    that justifies it.
    """
    account, payment_id = await charged(client)
    await execute(
        "DELETE FROM processor_charges WHERE attempt_ref = :id",
        {"id": uuid.UUID(payment_id)},
    )

    report = await detect()

    assert report.examined == 1
    assert DriftKind.CHARGE_MISSING_AT_PROCESSOR in report.kinds()

    finding = report.for_payment(uuid.UUID(payment_id))[0]
    assert finding.theirs == "no record"
    assert "250000" in finding.ours
    # The finding carries enough for a human to act without a second query.
    assert "no record of" in finding.detail


async def test_a_charge_the_processor_says_was_declined_is_flagged(
    client: AsyncClient,
) -> None:
    """We say succeeded and posted the money; they say the card refused.

    The most alarming finding the job can produce, because it means the ledger is
    crediting an account for money that was never taken.
    """
    account, payment_id = await charged(client)
    await execute(
        "UPDATE processor_charges SET outcome = 'failure', "
        "failure_reason = 'injected: card_declined' WHERE attempt_ref = :id",
        {"id": uuid.UUID(payment_id)},
    )

    report = await detect()

    assert DriftKind.CHARGE_OUTCOME_MISMATCH in report.kinds()
    finding = report.for_payment(uuid.UUID(payment_id))[0]
    assert "injected: card_declined" in finding.theirs


# --- Drift on refunds -----------------------------------------------------------------


async def test_a_refund_the_processor_has_no_record_of_is_flagged(
    client: AsyncClient,
) -> None:
    """We reversed money on our books that they never reversed on theirs.

    The customer's ledger balance has fallen for money that, by the processor's
    records, never went back to their card.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000)
    await execute("DELETE FROM processor_refunds")

    report = await detect()

    assert DriftKind.REFUND_MISSING_AT_PROCESSOR in report.kinds()
    finding = next(
        f for f in report.findings if f.kind is DriftKind.REFUND_MISSING_AT_PROCESSOR
    )
    assert finding.refund_id is not None
    assert finding.theirs == "no matching reversal"


async def test_a_refund_the_processor_made_that_we_never_recorded_is_flagged(
    client: AsyncClient,
) -> None:
    """THE CRASH-SHAPED ONE, and the reason it is more than an accounting nicety.

    This is the state left by a refund transaction that died after the processor
    reversed the money -- the window app/routers/refunds.py names and does not
    close. Our refunded total is now *understating* itself, which means a further
    refund would be allowed that should not fit, and the customer's ledger balance
    is overstated by the amount that already went back.
    """
    account, payment_id = await charged(client)
    await execute(
        "INSERT INTO processor_refunds "
        "(attempt_ref, charge_ref, processor_ref, amount, currency, outcome) "
        "VALUES (:a, :c, 'fake_re_injected', 100000, 'INR', 'success')",
        {"a": uuid.uuid4(), "c": uuid.UUID(payment_id)},
    )

    report = await detect()

    assert DriftKind.REFUND_MISSING_LOCALLY in report.kinds()
    finding = next(
        f for f in report.findings if f.kind is DriftKind.REFUND_MISSING_LOCALLY
    )
    assert finding.ours == "no matching refund"
    assert "fake_re_injected" in finding.theirs
    assert "understated" in finding.detail


async def test_a_refund_both_sides_have_for_different_amounts_is_flagged(
    client: AsyncClient,
) -> None:
    """Matched by processor_ref, so the amounts can be compared rather than guessed.

    Matching by amount instead would make this finding unreachable -- and would
    collapse two legitimate partial refunds of the same size into one, inventing a
    discrepancy where there is none.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000)
    await execute("UPDATE processor_refunds SET amount = 60000")

    report = await detect()

    assert DriftKind.REFUND_AMOUNT_MISMATCH in report.kinds()
    finding = next(
        f for f in report.findings if f.kind is DriftKind.REFUND_AMOUNT_MISMATCH
    )
    assert finding.ours == "100000"
    assert finding.theirs == "60000"


async def test_a_declined_refund_is_not_reported_as_missing(client: AsyncClient) -> None:
    """A refund the processor refused is *expected* to have no successful reversal.

    Worth its own test because the naive comparison -- every local refund should
    have a processor counterpart -- would report every declined refund as drift, and
    a job that cries wolf on normal outcomes gets switched off.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, force_outcome="failure")

    report = await detect()

    assert report.clean, report.findings


# --- Ledgerline disagreeing with itself ------------------------------------------------


async def test_the_ledger_disagreeing_with_our_own_records_is_flagged(
    client: AsyncClient,
) -> None:
    """No third party can cause this one, which is exactly why it is checked.

    Injected by adding a refunds row that points at an *existing* posting rather
    than a new one, so our records claim 150000 has come back while the ledger
    contains reversals totalling only 100000. An invariant nobody verifies is one
    that stops holding quietly.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000)

    existing_posting = await scalar("SELECT ledger_transaction_id FROM refunds")
    await execute(
        "INSERT INTO refunds "
        "(payment_id, amount, currency, status, ledger_transaction_id) "
        "VALUES (:p, 50000, 'INR', 'succeeded', :t)",
        {"p": uuid.UUID(payment_id), "t": existing_posting},
    )

    report = await detect()

    assert DriftKind.LEDGER_DISAGREES_WITH_RECORDS in report.kinds()
    finding = next(
        f for f in report.findings if f.kind is DriftKind.LEDGER_DISAGREES_WITH_RECORDS
    )
    assert "100000" in finding.ours
    assert "150000" in finding.theirs


# --- The restraint ----------------------------------------------------------------------


async def test_the_job_repairs_nothing(client: AsyncClient) -> None:
    """THE DESIGN DECISION, asserted rather than promised in a docstring.

    Every other background job in this project changes money: the sweep settles
    payments and writes postings, the publisher marks events delivered. This one
    reads two sides and reports. A discrepancy is evidence, not a diagnosis -- the
    same finding is consistent with a crash, a replayed request, and a hand-edit in
    a dashboard, and those need different repairs that depend on facts in neither
    database.

    So: run it against a system with real drift, and assert that afterwards every
    table holds exactly what it held before, including the drift itself.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000)
    await execute("DELETE FROM processor_refunds")

    before = {
        table: await count_rows(table)
        for table in (
            "payments",
            "refunds",
            "ledger_transactions",
            "ledger_entries",
            "outbox_events",
            "processor_charges",
            "processor_refunds",
        )
    }
    before_status = await scalar("SELECT status::text FROM payments")

    report = await detect()
    assert not report.clean, "this test needs real drift to be meaningful"

    after = {table: await count_rows(table) for table in before}
    assert after == before
    assert await scalar("SELECT status::text FROM payments") == before_status

    # Running it again finds the same drift, unchanged. Nothing was consumed,
    # acknowledged, or quietly resolved by having been looked at.
    again = await detect()
    assert again.kinds() == report.kinds()
    assert len(again.findings) == len(report.findings)


async def test_the_report_serialises_for_the_cli(client: AsyncClient) -> None:
    """``--json`` has to produce something a script can read, so the shape is pinned."""
    account, payment_id = await charged(client)
    await execute("DELETE FROM processor_charges")

    report = await detect()
    payload = report.as_dict()

    assert payload["examined"] == 1
    assert len(payload["findings"]) == 1
    finding = payload["findings"][0]
    assert finding["kind"] == DriftKind.CHARGE_MISSING_AT_PROCESSOR.value
    assert finding["payment_id"] == payment_id
    assert finding["refund_id"] is None
    assert set(finding) == {"kind", "payment_id", "refund_id", "detail", "ours", "theirs"}

    # And the human-readable form names what it found rather than just counting.
    assert "charge_missing_at_processor=1" in str(report)


async def test_several_payments_are_examined_and_only_the_broken_one_is_flagged(
    client: AsyncClient,
) -> None:
    """A pass covers the batch, and reports per payment rather than per run."""
    account = await create_account(client, "Customer")
    good = await create_charge(client, account, 100000)
    bad = await create_charge(client, account, 200000)
    await create_charge(client, account, 300000)

    await execute(
        "DELETE FROM processor_charges WHERE attempt_ref = :id",
        {"id": uuid.UUID(bad["id"])},
    )

    report = await detect()

    assert report.examined == 3
    assert len(report.findings) == 1
    assert report.for_payment(uuid.UUID(bad["id"]))
    assert not report.for_payment(uuid.UUID(good["id"]))
