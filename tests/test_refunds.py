"""Phase 6: money going back, and the invariant that bounds it.

Three claims, and they fail in different ways:

1. **A refund is a reversing posting, not an edit.** The charge's posting is never
   touched -- it cannot be, the ledger rejects UPDATE -- so a full refund returns
   the balance to its pre-charge value by arithmetic rather than by assertion, and
   the ledger still sums to zero.
2. **Refunds never exceed the charge.** Checked in the route, guarded by a row
   lock, and enforced by a trigger that holds even against psql. The third one is
   the only one that survives a caller who never came through the API, so it gets
   its own test written in raw SQL.
3. **A refund happens once.** Phase 3's machinery, reused rather than reimplemented
   -- so the interesting assertion is that the *same* replay semantics apply.

The partial-versus-full decision runs through all of it: a partial refund leaves
the payment ``succeeded``, because a payment with some money still live has not
been refunded. Only the reversal that returns the last of it transitions.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.db import engine
from app.models import PaymentStatus
from app.outbox import PAYMENT_REFUNDED, PAYMENT_SUCCEEDED
from tests.conftest import (
    count_rows,
    create_account,
    create_charge,
    get_balance,
    post_charge,
    refund_charge,
    scalar,
)

CHARGE = 250000
KEY = "phase6-key"


async def charged(client: AsyncClient, amount: int = CHARGE) -> tuple[str, str]:
    """An account with one succeeded charge against it. Returns (account, payment)."""
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, amount)
    return account, charge["id"]


async def ledger_sums_to_zero() -> bool:
    """The Phase 1 invariant, over every entry in the database.

    Asserted after refunds specifically because a reversing posting is the first
    thing in the project that moves money *back*, and a sign error in it would
    balance within its own transaction while breaking the whole-ledger total.
    """
    total = await scalar(
        "SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount "
        "ELSE -amount END), 0)::bigint FROM ledger_entries"
    )
    return int(total) == 0


# --- Full refunds -----------------------------------------------------------------


async def test_a_full_refund_reverses_the_charge_completely(client: AsyncClient) -> None:
    """The headline: status, posting, balance, and the ledger still sums to zero.

    Note what the balance assertion is really saying. It is not "we set it back to
    zero" -- nothing writes a balance, there is nowhere to write one. It is that the
    SUM over a credit of 250000 and a debit of 250000 is 0, which is the same
    mechanism Phase 1 shipped, given a second posting to sum.
    """
    account, payment_id = await charged(client)
    assert await get_balance(client, account) == CHARGE

    response = await refund_charge(client, payment_id)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "succeeded"
    assert body["amount"] == CHARGE
    assert body["total_refunded"] == CHARGE
    assert body["remaining_refundable"] == 0
    assert body["payment_status"] == PaymentStatus.REFUNDED
    assert body["ledger_transaction_id"] is not None

    # 'refunded' is finally reachable -- five phases after the enum value was added.
    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.REFUNDED

    # Two postings now: the charge and its reversal. The charge's was NOT edited.
    assert await count_rows("ledger_transactions") == 2
    assert await count_rows("ledger_entries") == 4
    assert await get_balance(client, account) == 0
    assert await ledger_sums_to_zero()

    # The house account is square again too: it funded the charge and got it back.
    house = await scalar(
        "SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount ELSE -amount END), 0)"
        "::bigint FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
        "WHERE a.name LIKE 'house:%'"
    )
    assert house == 0


async def test_a_full_refund_does_not_touch_the_charges_posting(
    client: AsyncClient,
) -> None:
    """Corrections are made by posting, not by rewriting. Phase 1's promise, kept.

    The charge's ledger_transaction_id is unchanged and its entries are byte for
    byte what they were. A refund that "fixed" the original would be editing
    history -- and the append-only triggers would reject it, which is the point of
    having them.
    """
    account, payment_id = await charged(client)
    original_posting = await scalar(
        "SELECT ledger_transaction_id FROM payments WHERE id = :id", {"id": payment_id}
    )
    original_entries = await scalar(
        "SELECT count(*) FROM ledger_entries WHERE transaction_id = :t",
        {"t": original_posting},
    )

    await refund_charge(client, payment_id)

    assert (
        await scalar(
            "SELECT ledger_transaction_id FROM payments WHERE id = :id", {"id": payment_id}
        )
        == original_posting
    )
    assert (
        await scalar(
            "SELECT count(*) FROM ledger_entries WHERE transaction_id = :t",
            {"t": original_posting},
        )
        == original_entries
    )

    # And the refund's posting is a different transaction entirely.
    refund_posting = await scalar("SELECT ledger_transaction_id FROM refunds")
    assert refund_posting != original_posting
    description = await scalar(
        "SELECT description FROM ledger_transactions WHERE id = :t", {"t": refund_posting}
    )
    assert "refund" in description


async def test_omitting_the_amount_refunds_everything_left(client: AsyncClient) -> None:
    """An empty body means "the rest of it", which for a fresh charge is all of it.

    The default exists because refunding in full is the common case, and a caller
    who does not have to compute an amount cannot compute it wrongly.
    """
    account, payment_id = await charged(client)

    # 100000 back first, so "everything left" is a remainder rather than the whole.
    await refund_charge(client, payment_id, 100000)
    response = await refund_charge(client, payment_id)

    assert response.status_code == 201, response.text
    assert response.json()["amount"] == CHARGE - 100000
    assert response.json()["payment_status"] == PaymentStatus.REFUNDED
    assert await get_balance(client, account) == 0


# --- Partial refunds ---------------------------------------------------------------


async def test_a_partial_refund_leaves_the_payment_succeeded(client: AsyncClient) -> None:
    """THE STATE DECISION: partly refunded is not a state, it is an amount.

    The payment stays ``succeeded`` because it is still partly live. How much has
    come back is a SUM over ``refunds`` -- there is no ``partially_refunded`` status
    and no ``refunded_amount`` column, which is the same call as having no
    ``balance`` column, made a third time.
    """
    account, payment_id = await charged(client)

    response = await refund_charge(client, payment_id, 100000)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["amount"] == 100000
    assert body["total_refunded"] == 100000
    assert body["remaining_refundable"] == 150000
    assert body["payment_status"] == PaymentStatus.SUCCEEDED

    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.SUCCEEDED
    assert await get_balance(client, account) == 150000
    assert await ledger_sums_to_zero()


async def test_partial_refunds_accumulate_until_the_last_one_transitions(
    client: AsyncClient,
) -> None:
    """Three partials totalling the charge. Only the third moves the payment.

    This is the whole partial-versus-full rule executed rather than described: the
    transition is triggered by arithmetic against the refunds table, not by anything
    the caller says or by which endpoint was used.
    """
    account, payment_id = await charged(client)

    first = await refund_charge(client, payment_id, 50000)
    second = await refund_charge(client, payment_id, 100000)

    assert first.json()["payment_status"] == PaymentStatus.SUCCEEDED
    assert second.json()["payment_status"] == PaymentStatus.SUCCEEDED
    assert second.json()["total_refunded"] == 150000
    assert await get_balance(client, account) == 100000

    third = await refund_charge(client, payment_id, 100000)
    assert third.json()["payment_status"] == PaymentStatus.REFUNDED
    assert third.json()["remaining_refundable"] == 0

    assert await count_rows("refunds") == 3
    # One charge posting plus three reversals.
    assert await count_rows("ledger_transactions") == 4
    assert await get_balance(client, account) == 0
    assert await ledger_sums_to_zero()


async def test_a_partial_that_would_exceed_the_charge_is_rejected_and_writes_nothing(
    client: AsyncClient,
) -> None:
    """Over-refund by request: 422, and zero rows written.

    "Writes nothing" is asserted rather than assumed, because the failure mode worth
    fearing is a rejected refund that still leaves a refunds row, a posting, or a
    consumed idempotency key behind it.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 200000)

    before_refunds = await count_rows("refunds")
    before_postings = await count_rows("ledger_transactions")

    response = await refund_charge(client, payment_id, 100000)

    assert response.status_code == 422, response.text
    assert "exceeds" in response.json()["detail"]
    assert "50000 still refundable" in response.json()["detail"]

    assert await count_rows("refunds") == before_refunds
    assert await count_rows("ledger_transactions") == before_postings
    assert await get_balance(client, account) == 50000
    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.SUCCEEDED


async def test_two_simultaneous_refunds_cannot_together_exceed_the_charge(
    client: AsyncClient,
) -> None:
    """The Phase 4 overdraw race, wearing refund clothes.

        read refunded total -> decide there is room -> write a refund

    Two requests for 150000 each against a 250000 charge. Both read a total of
    zero if nothing serialises them, both conclude there is room, and the payment
    ends up over-refunded by 50000 -- the identical shape to two withdrawals
    against one balance, and correct in isolation both times.

    ``lock_payment_for_refund`` is what makes the second one wait and then read a
    total that already includes the first. Asserted on the *outcome* rather than on
    the mechanism: whatever happens, the money that went back cannot exceed what
    came in.
    """
    account, payment_id = await charged(client)

    left, right = await asyncio.gather(
        refund_charge(client, payment_id, 150000, key="left"),
        refund_charge(client, payment_id, 150000, key="right"),
    )

    statuses = sorted([left.status_code, right.status_code])
    assert statuses == [201, 422], (left.text, right.text)

    assert await count_rows("refunds") == 1
    total = await scalar(
        "SELECT COALESCE(SUM(amount), 0)::bigint FROM refunds WHERE status = 'succeeded'"
    )
    assert total == 150000
    assert await get_balance(client, account) == 100000
    assert await ledger_sums_to_zero()


# --- The invariant, in the database -------------------------------------------------


async def test_an_over_refund_is_unstorable_even_from_raw_sql(
    client: AsyncClient,
) -> None:
    """THE HARD INVARIANT: the database refuses, with the application bypassed.

    Written as a raw INSERT on the engine, not through the API, because that is the
    only way to test the claim being made. The route's check and the row lock both
    live in Python; a caller with a psql prompt has neither, and "unstorable" has to
    mean unstorable *for them too*.

    A CHECK constraint could not express this -- it sees one row and the rule is a
    SUM over others, the same reason ``CHECK (balance >= 0)`` cannot be written. So
    it is the trigger from migration 0007, which takes the payment's row lock itself
    before summing.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 200000)

    posting = await scalar("SELECT id FROM ledger_transactions LIMIT 1")

    with pytest.raises(Exception) as caught:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO refunds "
                    "(payment_id, amount, currency, status, ledger_transaction_id) "
                    "VALUES (:p, 100000, 'INR', 'succeeded', :t)"
                ),
                {"p": uuid.UUID(payment_id), "t": posting},
            )

    assert "exceeds" in str(caught.value)
    assert await count_rows("refunds") == 1


async def test_a_failed_refund_is_stored_and_reserves_nothing(
    client: AsyncClient,
) -> None:
    """A declined reversal is an audit record, not a claim on the charge.

    The trigger fires only for succeeded refunds, which is why this row can exist
    alongside a full refund without breaching anything. Storing it matters: "we
    tried and were refused" is exactly what a customer disputing a refund will ask
    about, and a flow that only records successes cannot answer.
    """
    account, payment_id = await charged(client)

    declined = await refund_charge(client, payment_id, force_outcome="failure")
    assert declined.status_code == 201, declined.text
    assert declined.json()["status"] == "failed"
    assert declined.json()["ledger_transaction_id"] is None
    assert declined.json()["total_refunded"] == 0
    assert "refund_declined" in declined.json()["failure_reason"]

    # No money moved, and the payment is untouched.
    assert await count_rows("ledger_transactions") == 1
    assert await get_balance(client, account) == CHARGE
    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.SUCCEEDED

    # And the full amount is still refundable, because a decline reserved nothing.
    full = await refund_charge(client, payment_id)
    assert full.status_code == 201, full.text
    assert full.json()["amount"] == CHARGE
    assert await get_balance(client, account) == 0


async def test_the_payment_constraint_now_admits_refunded(client: AsyncClient) -> None:
    """Migration 0003 left a note; 0007 is the answer to it.

    ``(status = 'succeeded') = (ledger_transaction_id IS NOT NULL)`` made 'refunded'
    literally unstorable, because a refunded payment keeps its charge's posting.
    This asserts the widened form holds and did not simply get dropped: a 'failed'
    payment still may not point at a posting.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id)

    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.REFUNDED
    assert (
        await scalar(
            "SELECT ledger_transaction_id IS NOT NULL FROM payments WHERE id = :id",
            {"id": payment_id},
        )
        is True
    )

    # The constraint got wider by one status and not one inch more.
    with pytest.raises(Exception) as caught:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE payments SET status = 'failed' WHERE id = :id"),
                {"id": uuid.UUID(payment_id)},
            )
    assert "ck_payments_posting_matches_status" in str(caught.value)


# --- What cannot be refunded --------------------------------------------------------


async def test_refunding_a_failed_charge_is_refused(client: AsyncClient) -> None:
    """There was never any money to send back, and the state machine says so.

    Falls out of ``ALLOWED_TRANSITIONS[FAILED] == frozenset()`` rather than needing
    a rule of its own -- though the route rejects it before the state machine is
    reached, so the caller gets a sentence instead of a stack trace.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, CHARGE, force_outcome="failure")

    response = await refund_charge(client, charge["id"])

    assert response.status_code == 409, response.text
    assert "cannot be refunded" in response.json()["detail"]
    assert await count_rows("refunds") == 0
    assert await count_rows("ledger_transactions") == 0


async def test_refunding_an_already_fully_refunded_payment_is_refused(
    client: AsyncClient,
) -> None:
    """Two full refunds would be two reversals of one charge."""
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id)

    response = await refund_charge(client, payment_id, 1)

    assert response.status_code == 409, response.text
    assert "already been refunded in full" in response.json()["detail"]
    assert await count_rows("refunds") == 1
    assert await get_balance(client, account) == 0


async def test_refunding_an_unknown_payment_is_a_404(client: AsyncClient) -> None:
    response = await refund_charge(client, str(uuid.uuid4()))
    assert response.status_code == 404, response.text
    assert await count_rows("refunds") == 0


async def test_refunding_a_payment_still_processing_is_refused(
    client: AsyncClient,
) -> None:
    """A charge whose outcome we do not know yet has nothing to reverse.

    Written straight into the database because a payment stranded in 'processing' is
    a Phase 5a state, and the point here is that Phase 6 refuses to act on it rather
    than guessing. The sweep settles it first; only then is there something to
    refund.
    """
    account = await create_account(client, "Customer")
    payment_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO payments (id, account_id, amount, currency, status) "
                "VALUES (:id, :a, :amt, 'INR', 'processing')"
            ),
            {"id": payment_id, "a": uuid.UUID(account), "amt": CHARGE},
        )

    response = await refund_charge(client, str(payment_id))

    assert response.status_code == 409, response.text
    assert "processing" in response.json()["detail"]
    assert await count_rows("refunds") == 0


# --- Idempotency: Phase 3's machinery, third use ------------------------------------


async def test_the_same_refund_key_twice_refunds_once(client: AsyncClient) -> None:
    """THE IDEMPOTENCY PROPERTY: one refund, and a byte-identical replay.

    Byte-identical is asserted on ``.content`` rather than on the parsed body,
    because that is the promise Phase 3 made and the mechanism that keeps it --
    serving both responses from what was read back out of JSONB -- is invisible to a
    comparison of dicts.
    """
    account, payment_id = await charged(client)

    first = await refund_charge(client, payment_id, 100000, key=KEY)
    second = await refund_charge(client, payment_id, 100000, key=KEY)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.content == second.content

    assert await count_rows("refunds") == 1
    # One charge posting and exactly one reversal, not two.
    assert await count_rows("ledger_transactions") == 2
    assert await get_balance(client, account) == 150000

    # And the processor was asked to reverse once, not twice.
    assert await count_rows("processor_refunds") == 1


async def test_the_same_key_with_a_different_amount_is_rejected(
    client: AsyncClient,
) -> None:
    """A key is a promise about one specific refund."""
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000, key=KEY)

    response = await refund_charge(client, payment_id, 50000, key=KEY)

    assert response.status_code == 422, response.text
    assert "different payload" in response.json()["detail"]
    assert await count_rows("refunds") == 1
    assert await get_balance(client, account) == 150000


async def test_a_refund_without_an_idempotency_key_is_refused(
    client: AsyncClient,
) -> None:
    """Required, not optional -- the same call Phase 3 made and for the same reason.

    An optional safety net is missing from exactly the caller that needed it, and a
    caller who has not thought about retrying a refund is the one most likely to
    send it twice.
    """
    account, payment_id = await charged(client)

    response = await client.post(f"/charges/{payment_id}/refund", json={"amount": 1000})

    assert response.status_code == 400, response.text
    assert "Idempotency-Key" in response.json()["detail"]
    assert await count_rows("refunds") == 0


async def test_a_rejected_refund_leaves_its_key_free(client: AsyncClient) -> None:
    """Ask for too much, fix the amount, retry with the same key. Phase 3, reused.

    The claim rolls back with the request, so a caller who got a 422 for an amount
    is not also locked out of their own key for 24 hours over it.
    """
    account, payment_id = await charged(client)

    too_much = await refund_charge(client, payment_id, CHARGE + 1, key=KEY)
    assert too_much.status_code == 422

    corrected = await refund_charge(client, payment_id, CHARGE, key=KEY)
    assert corrected.status_code == 201, corrected.text
    assert await get_balance(client, account) == 0


async def test_a_different_key_is_a_genuinely_second_refund(client: AsyncClient) -> None:
    """Idempotency is per key, so two keys mean two refunds -- if there is room."""
    account, payment_id = await charged(client)

    await refund_charge(client, payment_id, 100000, key="first")
    second = await refund_charge(client, payment_id, 100000, key="second")

    assert second.status_code == 201, second.text
    assert await count_rows("refunds") == 2
    assert await get_balance(client, account) == 50000


async def test_the_derived_attempt_ref_makes_a_retry_a_replay_at_the_processor(
    client: AsyncClient,
) -> None:
    """Why refunds do not need Phase 5a's two-transaction split.

    The processor-side reference is uuid5 over the payment and the idempotency key,
    so a retry computes the same reference without having stored it. Asserted at the
    processor's books: one reversal recorded, and the second request's reference is
    the same value as the first's.
    """
    from app.refunds import refund_attempt_ref

    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000, key=KEY)

    expected = refund_attempt_ref(uuid.UUID(payment_id), KEY)
    stored = await scalar("SELECT attempt_ref FROM processor_refunds")
    assert stored == expected

    # Stable across calls, which is the entire property being relied on.
    assert refund_attempt_ref(uuid.UUID(payment_id), KEY) == expected


# --- The outbox ----------------------------------------------------------------------


async def test_a_successful_refund_emits_its_event_in_the_same_transaction(
    client: AsyncClient,
) -> None:
    """Phase 5b's rule, unchanged for refunds: the event exists iff the money moved."""
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, 100000)

    types = [
        row
        for row in await scalar_list(
            "SELECT event_type FROM outbox_events ORDER BY created_at"
        )
    ]
    assert types == [PAYMENT_SUCCEEDED, PAYMENT_REFUNDED]

    payload = await scalar(
        "SELECT payload FROM outbox_events WHERE event_type = :t", {"t": PAYMENT_REFUNDED}
    )
    assert payload["payment_id"] == payment_id
    assert payload["amount"] == 100000
    assert payload["total_refunded"] == 100000
    assert payload["remaining_refundable"] == 150000
    assert payload["payment_status"] == PaymentStatus.SUCCEEDED
    assert payload["ledger_transaction_id"] is not None


async def test_a_declined_refund_emits_nothing(client: AsyncClient) -> None:
    """No money went back, so nothing is announced."""
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, force_outcome="failure")

    count = await scalar(
        "SELECT count(*) FROM outbox_events WHERE event_type = :t",
        {"t": PAYMENT_REFUNDED},
    )
    assert count == 0


# --- Listing --------------------------------------------------------------------------


async def test_listing_refunds_includes_declined_attempts(client: AsyncClient) -> None:
    """"What was attempted?" and "what was refunded?" are different questions."""
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id, force_outcome="failure")
    await refund_charge(client, payment_id, 100000)

    response = await client.get(f"/charges/{payment_id}/refunds")
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body) == 2
    assert [r["status"] for r in body] == ["failed", "succeeded"]
    # The derived total counts only the successful one.
    assert all(r["total_refunded"] == 100000 for r in body)


# --- A refund is not subject to the balance floor ---------------------------------------


async def test_a_refund_may_take_the_balance_negative(client: AsyncClient) -> None:
    """The deliberate asymmetry with withdrawals, asserted so it cannot drift.

    The customer withdrew the money, then the charge was reversed. A withdrawal
    refuses to cross the floor; a refund does not, because it is a reversal of
    something that already happened rather than a request Ledgerline may decline.
    The money is going back to the card either way, and a negative balance is an
    accurate statement that the customer owes it.
    """
    account, payment_id = await charged(client)

    withdrawal = await client.post(
        "/withdrawals", json={"account_id": account, "amount": CHARGE}
    )
    assert withdrawal.status_code == 201, withdrawal.text
    assert await get_balance(client, account) == 0

    response = await refund_charge(client, payment_id)

    assert response.status_code == 201, response.text
    assert await get_balance(client, account) == -CHARGE
    assert await ledger_sums_to_zero()


async def scalar_list(sql: str, params: dict | None = None) -> list:
    """Every value in the first column, for the handful of assertions that need it."""
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return [row[0] for row in result]


async def test_a_charge_after_a_full_refund_is_a_new_payment(client: AsyncClient) -> None:
    """'refunded' is terminal: reversing a reversal is a new charge, not a state move.

    There is no un-refund endpoint and no transition out of ``refunded``. Charging
    the customer again produces a second payment with its own posting, which is what
    actually happened and is what the audit trail should say.
    """
    account, payment_id = await charged(client)
    await refund_charge(client, payment_id)

    again = await post_charge(client, account, CHARGE)
    assert again.status_code == 201, again.text
    assert again.json()["id"] != payment_id

    assert await count_rows("payments") == 2
    assert await get_balance(client, account) == CHARGE
    assert await ledger_sums_to_zero()
