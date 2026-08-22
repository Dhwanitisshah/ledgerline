"""Phase 5a: the crash between the processor and COMMIT, reproduced and closed.

Phase 2 named this gap in a docstring and left it standing. Phases 3 and 4 did not
touch it. It is the only defect in the project that cannot be fixed by arranging a
single transaction more carefully, because the thing that must be made atomic --
"the processor charged the card" and "Ledgerline recorded it" -- spans two systems.

So the fix is not atomicity. It is durability plus reconciliation: commit the
attempt before the money can move, and settle whatever is left stranded against the
processor's own records afterwards.

## The shape of the file

``test_the_single_txn_charge_loses_a_charged_card`` is the **preserved
reproduction**, in the same idiom as Phase 4's race tests. It runs the Phases 2-4
flow, crashes it at the fatal instant, and asserts that the money is gone and
Ledgerline has no idea. It is not an xfail: if it ever stops reproducing, the
before/after in the README has become fiction and needs re-measuring.

Everything after it runs the shipped path and asserts the same crash is survivable.

## Why the crash is honest

``force_crash_after_processor`` raises where a dying process would stop. Postgres
cannot tell the difference -- both are a transaction that ends without COMMIT -- and
the processor's books are written on their own connection and their own
transaction, so they survive either way. That last part is the whole reproduction:
if the fake processor recorded charges on the request's session, the rollback would
take the evidence with it and there would be nothing to find.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.config import settings
from app.db import async_session, engine
from app.deps import processor_books
from app.models import PaymentStatus
from app.processor import FakeProcessor, ProcessorOutcome
from app.reconcile import ReconcileOutcome, find_stuck_payments, sweep_once
from app.strategies import ChargeDurability
from tests.conftest import count_rows, create_account, get_balance, post_charge, scalar

KEY = "phase5a-key"


def reconciler() -> FakeProcessor:
    """A processor used only for ``lookup``.

    Its configured outcome is irrelevant -- the sweep never calls ``charge`` -- and
    passing the shared books is the only thing that matters. This is the same
    object the ``python -m app.reconcile`` worker builds.
    """
    return FakeProcessor(books=processor_books)


async def sweep(*, stuck_after_seconds: int = 0):
    """Run one reconciliation pass over everything currently stuck."""
    return await sweep_once(
        async_session, reconciler(), stuck_after_seconds=stuck_after_seconds
    )


async def crash_a_charge(
    client: AsyncClient, account: str, amount: int, *, key: str = KEY, **overrides: object
) -> None:
    """Send a charge that dies immediately after the processor answers.

    Phase 7 changed what this looks like from outside. The crash used to propagate
    out of the ASGI transport and be caught here with ``pytest.raises``; now
    ``AccessLogMiddleware`` catches it, logs it with a request id, and returns a bare
    500 -- which is what a real client always saw and what production must do rather
    than leaking a traceback onto the wire. So this asserts the 500, which is a
    strictly better assertion: it is the observable behaviour rather than an artifact
    of the test transport.
    """
    response = await post_charge(
        client,
        account,
        amount,
        key=key,
        force_crash_after_processor=True,
        **overrides,
    )
    assert response.status_code == 500, response.text
    # The id ties this response to the log line carrying the traceback.
    assert "request_id" in response.json()


async def only_payment_id() -> str:
    return str(await scalar("SELECT id FROM payments"))


# --- The preserved reproduction -------------------------------------------------


@pytest.mark.race
async def test_the_single_txn_charge_loses_a_charged_card(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESERVED REPRODUCTION -- the Phase 2 gap, executed rather than described.

    One transaction around the processor call. The card is charged, the process
    dies before COMMIT, and everything Ledgerline was about to write is rolled back
    by the dying transaction: no payment, no posting, no idempotency key. The money
    left the customer's account at the card network and there is no row anywhere in
    this service that says so.

    Note what makes it silent rather than loud. Nothing errored *in the database*.
    No constraint was violated, no invariant broke, the ledger still sums to zero,
    and every Phase 1-4 test still passes. The system is perfectly consistent and
    perfectly wrong, and the only evidence lives in a third party's records that
    nothing here is looking at.

    A failure here means the reproduction stopped reproducing, not that the bug is
    fixed.
    """
    monkeypatch.setattr(settings, "CHARGE_DURABILITY", ChargeDurability.SINGLE_TXN)

    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    # The processor charged the card and committed that on its own connection.
    assert await count_rows("processor_charges") == 1
    charged = await scalar("SELECT outcome FROM processor_charges")
    assert charged == "success"

    # And Ledgerline has nothing. Not an unresolved payment -- nothing at all.
    assert await count_rows("payments") == 0
    assert await count_rows("ledger_transactions") == 0
    assert await count_rows("ledger_entries") == 0
    assert await count_rows("idempotency_keys") == 0
    assert await get_balance(client, account) == 0

    # There is nothing for the sweep to find, which is the point: recovery needs
    # something durable to recover *from*, and this flow committed nothing.
    report = await sweep()
    assert report.examined == 0


# --- The fix ---------------------------------------------------------------------


async def test_the_durable_intent_charge_survives_the_same_crash(
    client: AsyncClient,
) -> None:
    """The identical crash, on the shipped path, leaves evidence.

    Same request, same instant of death, same charged card. The difference is that
    the attempt was committed before the processor was called, so what remains is a
    payment in 'processing': a durable statement that Ledgerline asked for this
    charge and does not know how it ended.

    The money has still not moved in the ledger, and that is correct -- nothing here
    has confirmed the charge yet. The claim being made is only that the attempt is
    now *findable*.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    assert await count_rows("processor_charges") == 1
    assert await count_rows("payments") == 1

    payment_id = await only_payment_id()
    status = await scalar("SELECT status::text FROM payments WHERE id = :id", {"id": payment_id})
    assert status == PaymentStatus.PROCESSING

    # No posting yet: 'processing' is not a claim that anything succeeded.
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, account) == 0

    # The key was consumed by the request that died, and is waiting on this payment.
    assert await count_rows("idempotency_keys") == 1
    key_status = await scalar("SELECT status FROM idempotency_keys WHERE key = :k", {"k": KEY})
    bound = await scalar("SELECT payment_id FROM idempotency_keys WHERE key = :k", {"k": KEY})
    assert key_status == "in_progress"
    assert str(bound) == payment_id

    # And the sweep can see it.
    async with async_session() as session:
        stuck = await find_stuck_payments(session, stuck_after_seconds=0, batch_size=10)
    assert [str(pid) for pid in stuck] == [payment_id]


async def test_the_sweep_settles_an_abandoned_charge_against_the_processor(
    client: AsyncClient,
) -> None:
    """The headline property of Phase 5a.

    The card was charged and the request that charged it never came back. The sweep
    asks the processor what happened, is told the charge succeeded, and writes the
    posting now -- the same balanced two-legged posting the charge route would have
    written, minutes late and from the processor's record rather than from a Python
    variable that died with the request.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)
    payment_id = await only_payment_id()

    report = await sweep()

    assert report.outcomes == {ReconcileOutcome.SETTLED_SUCCEEDED: 1}
    assert report.settled == 1

    # The payment is settled, and the money is in the ledger exactly once.
    status = await scalar("SELECT status::text FROM payments WHERE id = :id", {"id": payment_id})
    assert status == PaymentStatus.SUCCEEDED
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await get_balance(client, account) == 250000

    # The posting is a real double-entry posting, house account and all, so the
    # ledger still sums to zero across the reconciled charge.
    house = await scalar(
        "SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount ELSE -amount END), 0)"
        "::bigint FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
        "WHERE a.name LIKE 'house:%'"
    )
    assert house == -250000

    # The processor reference recorded on the payment is the processor's own, read
    # back from its books rather than invented during reconciliation.
    payment_ref = await scalar(
        "SELECT processor_ref FROM payments WHERE id = :id", {"id": payment_id}
    )
    processor_ref = await scalar("SELECT processor_ref FROM processor_charges")
    assert payment_ref == processor_ref

    # It is marked as reconciled rather than passed off as an ordinary charge.
    description = await scalar("SELECT description FROM ledger_transactions")
    assert "reconciled" in description


async def test_the_sweep_frees_the_key_so_a_retry_gets_its_answer(
    client: AsyncClient,
) -> None:
    """The customer must not be locked out for 24 hours by our crash.

    The dead request consumed the idempotency key, so until the sweep runs a retry
    gets a 409 -- correct, but only bearable because it is brief. Once the payment
    is settled, the key is finalised with the reconciled outcome and the very next
    retry replays it, exactly as though the original request had lived long enough
    to answer.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    # Before the sweep: the key is held by a charge nobody is running.
    blocked = await post_charge(client, account, 250000, key=KEY)
    assert blocked.status_code == 409, blocked.text

    await sweep()

    replayed = await post_charge(client, account, 250000, key=KEY)
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["status"] == PaymentStatus.SUCCEEDED
    assert replayed.json()["ledger_transaction_id"] is not None

    # And the retry replayed rather than charging again.
    assert await count_rows("payments") == 1
    assert await count_rows("ledger_transactions") == 1
    assert await get_balance(client, account) == 250000

    key_status = await scalar("SELECT status FROM idempotency_keys WHERE key = :k", {"k": KEY})
    assert key_status == "completed"


async def test_the_sweep_settles_a_declined_charge_as_failed(client: AsyncClient) -> None:
    """A crash after a decline reconciles to 'failed', and moves no money.

    Worth its own test because the tempting shortcut -- treat every stuck payment as
    succeeded and post it, since we know we called the processor -- would credit an
    account for a card that was refused. The processor is asked, not assumed.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000, force_outcome="failure")
    payment_id = await only_payment_id()

    report = await sweep()

    assert report.outcomes == {ReconcileOutcome.SETTLED_FAILED: 1}
    status = await scalar("SELECT status::text FROM payments WHERE id = :id", {"id": payment_id})
    assert status == PaymentStatus.FAILED

    # Phase 2's guarantee, held through a crash and a reconciliation.
    assert await count_rows("ledger_transactions") == 0
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, account) == 0

    reason = await scalar(
        "SELECT failure_reason FROM payments WHERE id = :id", {"id": payment_id}
    )
    assert "card_declined" in reason


async def test_a_stuck_payment_the_processor_never_saw_is_settled_as_failed(
    client: AsyncClient,
) -> None:
    """The third answer: the processor has no record, so no money moved.

    This is the crash that happens *before* the call lands rather than after it.
    Written straight into the database because there is no way to produce it through
    the API -- which is the point, since the sweep must handle states nothing in the
    request path can create.

    Settling as failed is safe on the processor's authority. Leaving it in
    'processing' forever would be the worse answer: it is a state nobody can act on.
    """
    account = await create_account(client, "Customer")
    payment_id = uuid.uuid4()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (id, account_id, amount, currency, status) "
                "VALUES (:id, :account_id, 250000, 'INR', 'processing')"
            ),
            {"id": payment_id, "account_id": uuid.UUID(account)},
        )

    assert await count_rows("processor_charges") == 0

    report = await sweep()

    assert report.outcomes == {ReconcileOutcome.NO_PROCESSOR_RECORD: 1}
    status = await scalar("SELECT status::text FROM payments WHERE id = :id", {"id": payment_id})
    assert status == PaymentStatus.FAILED
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, account) == 0

    reason = await scalar(
        "SELECT failure_reason FROM payments WHERE id = :id", {"id": payment_id}
    )
    assert "no record of this attempt" in reason


# --- The sweep must be safe to run any number of times ---------------------------


async def test_running_the_sweep_twice_settles_the_payment_once(
    client: AsyncClient,
) -> None:
    """At-least-once resolution, exactly-once effect.

    The reconciler is a recovery process, so it will be run repeatedly, on a timer,
    by two workers at once, and by a nervous engineer during an incident. A second
    pass must find nothing to do -- not because it is careful, but because the
    payment is no longer 'processing' and the locked query therefore does not return
    it.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    first = await sweep()
    second = await sweep()

    assert first.settled == 1
    assert second.examined == 0

    # One posting, one balance movement. Not two.
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await get_balance(client, account) == 250000


async def test_two_sweeps_running_at_once_settle_the_payment_once(
    client: AsyncClient,
) -> None:
    """Two reconcilers, one backlog. SKIP LOCKED divides it rather than duplicating.

    Both passes find the same candidate, because finding is an unlocked read. Only
    one can claim it: the other's ``FOR UPDATE SKIP LOCKED`` returns no row and it
    moves on instead of waiting. If the lock were plain ``FOR UPDATE`` the loser
    would block, wake up after the winner committed, and find the payment no longer
    'processing' -- also correct, but having paid for it with a parked connection.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    left, right = await asyncio.gather(sweep(), sweep())

    assert left.settled + right.settled == 1
    assert await count_rows("ledger_transactions") == 1
    assert await get_balance(client, account) == 250000


async def test_the_sweep_leaves_a_charge_that_is_still_running_alone(
    client: AsyncClient,
) -> None:
    """The threshold is what separates 'in flight' from 'abandoned'.

    A payment that has been in 'processing' for a moment is almost certainly a
    charge still talking to the processor. Reconciling it would be racing the
    request that owns it. With the default threshold the sweep does not consider it
    at all.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    # Fresh payment, realistic threshold: not a candidate.
    async with async_session() as session:
        candidates = await find_stuck_payments(
            session, stuck_after_seconds=settings.RECONCILE_STUCK_AFTER_SECONDS, batch_size=10
        )
    assert candidates == []

    report = await sweep_once(
        async_session, reconciler(), stuck_after_seconds=settings.RECONCILE_STUCK_AFTER_SECONDS
    )
    assert report.examined == 0

    # Still unresolved, and still waiting for a sweep that is willing to take it.
    assert await count_rows("ledger_entries") == 0


async def test_a_healthy_charge_is_never_touched_by_the_sweep(client: AsyncClient) -> None:
    """The overwhelmingly common case: nothing to reconcile, and nothing reconciled.

    A charge that completes normally leaves no payment in 'processing', so the sweep
    has no candidates even with the threshold at zero.
    """
    account = await create_account(client, "Customer")
    response = await post_charge(client, account, 250000, key=KEY)
    assert response.status_code == 201

    report = await sweep()

    assert report.examined == 0
    assert await count_rows("ledger_transactions") == 1
    assert await get_balance(client, account) == 250000


# --- The processor's side of the contract ----------------------------------------


async def test_the_processor_books_survive_the_charge_rolling_back(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism the whole phase rests on, asserted directly.

    If the fake processor wrote its records on the request's session, they would
    roll back with the crash and the reproduction above would silently stop being a
    reproduction -- it would pass for the wrong reason, because there would be no
    charged card to lose. This test pins the independence itself rather than its
    consequence.
    """
    monkeypatch.setattr(settings, "CHARGE_DURABILITY", ChargeDurability.SINGLE_TXN)

    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, 250000)

    # Ledgerline's transaction rolled back completely.
    assert await count_rows("payments") == 0
    # The processor's did not.
    assert await count_rows("processor_charges") == 1

    amount = await scalar("SELECT amount FROM processor_charges")
    assert amount == 250000


async def test_charging_one_attempt_reference_twice_charges_once(
    client: AsyncClient,
) -> None:
    """The processor is idempotent on the attempt reference, as a real one is.

    This is what makes the sweep's lookup trustworthy, and it is also the reason the
    attempt reference is the payment id rather than a fresh uuid per call: a
    reference that changed on retry would let the same payment be charged twice.

    Takes the ``client`` fixture purely for its truncation, not to send requests --
    this is a statement about the adapter, and going through the API would only put
    a charge flow between the assertion and the thing being asserted.
    """
    attempt = uuid.uuid4()
    processor = FakeProcessor(books=processor_books)

    first = await processor.charge(attempt_ref=attempt, amount=1000, currency="INR")
    # A second call that *wants* a different answer still gets the first one.
    declining = FakeProcessor(outcome=ProcessorOutcome.FAILURE, books=processor_books)
    second = await declining.charge(attempt_ref=attempt, amount=1000, currency="INR")

    assert first.succeeded
    assert second.succeeded
    assert first.processor_ref == second.processor_ref
    assert await count_rows("processor_charges") == 1

    found = await processor.lookup(attempt)
    assert found is not None
    assert found.processor_ref == first.processor_ref

    # An attempt it never saw is None, not a guess. The sweep depends on that.
    assert await processor.lookup(uuid.uuid4()) is None


# --- The second thing the split bought -------------------------------------------


PROCESSOR_LATENCY_MS = 800
SAMPLE_INTERVAL_S = 0.04

_IDLE_IN_TRANSACTION_SQL = text(
    """
    SELECT count(*)
    FROM pg_stat_activity
    WHERE datname = current_database()
      AND state = 'idle in transaction'
      AND pid <> pg_backend_pid()
    """
)


async def _samples_holding_a_transaction(during) -> tuple[int, int]:
    """Poll pg_stat_activity while ``during`` runs. Returns (hits, samples).

    A backend in ``idle in transaction`` has a transaction open and is doing
    nothing with it -- precisely what a connection looks like while it waits on a
    third party. The observer excludes its own pid, since it is inside a
    transaction itself while asking.

    Counting *samples* rather than taking a single peak is what makes this stable.
    Any charge flow is briefly idle between the statements of its own transactions,
    so a one-off sighting proves nothing either way. What separates the two
    arrangements is duration: a flow that holds a transaction across an 800ms
    processor call is idle for most of the window, and one that does not is idle for
    a couple of milliseconds at a transaction boundary.
    """
    hits = 0
    samples = 0
    task = asyncio.create_task(during)

    async with engine.connect() as conn:
        while not task.done():
            result = await conn.execute(_IDLE_IN_TRANSACTION_SQL)
            hits += 1 if int(result.scalar_one()) > 0 else 0
            samples += 1
            await conn.rollback()
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    await task
    return hits, samples


async def test_the_durable_intent_path_holds_no_transaction_across_the_processor_call(
    client: AsyncClient,
) -> None:
    """Phase 4 fixed the symptom of the open transaction; Phase 5a removes it.

    Under the old single-transaction flow, the whole processor call happened with a
    write transaction open, so the backend sat in ``idle in transaction`` for the
    length of a card authorisation -- holding its locks and its connection. Phase 4
    stopped duplicates queueing behind that, but the transaction was still open.

    Splitting the flow means there is nothing to be idle *in*: the intent is
    committed before the call and the settlement transaction does not begin until
    after it. Measured rather than claimed, because "we removed a class of
    connection pressure" is exactly the sort of statement that quietly stops being
    true.
    """
    account = await create_account(client, "Customer")

    hits, samples = await _samples_holding_a_transaction(
        post_charge(client, account, 1000, key=KEY, force_latency_ms=PROCESSOR_LATENCY_MS)
    )

    # A quarter of the window, against the preserved flow's *half or more* below.
    # The two are separated by a factor of four, which is the honest shape of the
    # claim: this is a statement about DURATION, not about an exact count.
    #
    # This was `hits <= 1` until Phase 7. The middleware stack added per-request
    # task switching -- four BaseHTTPMiddleware layers, each an anyio task group --
    # and under a full-suite run that is enough extra scheduling for a sample to
    # land inside a transaction boundary two or three times instead of never. The
    # test failed about half of full-suite runs while passing every time in
    # isolation, which is the signature of a threshold set to the noise floor rather
    # than of a broken guarantee. Loosened to where it measures the thing it is
    # about; the paired reproduction below still has to clear ten of twenty-one for
    # the before/after to hold, so nothing has been quietly made unfalsifiable.
    assert hits <= samples // 4, (
        f"a transaction was open for {hits} of {samples} samples across a "
        f"{PROCESSOR_LATENCY_MS}ms processor call; the durable_intent split is "
        "supposed to leave none open across it for any sustained period"
    )
    assert await get_balance(client, account) == 1000


@pytest.mark.race
async def test_the_single_txn_path_parks_a_connection_for_the_whole_call(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESERVED REPRODUCTION -- the other half of the measurement above.

    The same request on the Phases 2-4 flow does hold a transaction open across the
    processor call. Without this, the assertion that the split removed something
    would be unfalsifiable: a peak of zero proves nothing unless the arrangement it
    replaced produces a peak above zero on the same machine, in the same run.
    """
    monkeypatch.setattr(settings, "CHARGE_DURABILITY", ChargeDurability.SINGLE_TXN)

    account = await create_account(client, "Customer")

    hits, samples = await _samples_holding_a_transaction(
        post_charge(client, account, 1000, key=KEY, force_latency_ms=PROCESSOR_LATENCY_MS)
    )

    # The transaction is open for essentially the whole call. Asserted as "most of
    # the window" rather than "every sample" so that a slow or noisy runner losing a
    # sample or two does not fail a claim that is about duration, not precision.
    assert hits >= samples // 2, (
        f"expected the single-transaction flow to hold a connection open across the "
        f"{PROCESSOR_LATENCY_MS}ms processor call, saw it in only {hits} of "
        f"{samples} samples -- the before/after needs re-measuring"
    )
