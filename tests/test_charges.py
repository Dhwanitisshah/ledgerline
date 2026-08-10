"""Phase 2 acceptance tests: the charge flow, against a real Postgres.

The test that matters most in this file is
``test_a_declined_charge_moves_no_money``. Everything else establishes that the
happy path works; that one establishes that the unhappy path costs nothing, which
is the only reason the phase exists.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import engine
from app.ledger import settlement_account_name
from tests.conftest import count_rows, create_account, create_charge, get_balance, scalar


async def test_a_successful_charge_credits_the_account(client: AsyncClient) -> None:
    """201, status 'succeeded', and the balance moves by exactly the amount."""
    account = await create_account(client, "Customer")
    assert await get_balance(client, account) == 0

    charge = await create_charge(client, account, 250000)

    assert charge["status"] == "succeeded"
    assert charge["amount"] == 250000
    assert charge["currency"] == "INR"
    assert charge["processor_ref"].startswith("fake_ch_")
    assert charge["failure_reason"] is None
    assert charge["ledger_transaction_id"] is not None

    assert await get_balance(client, account) == 250000


async def test_a_successful_charge_writes_a_balanced_two_legged_posting(
    client: AsyncClient,
) -> None:
    """The money side is a real double-entry posting, not a balance bump."""
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 250000)
    ledger_transaction_id = charge["ledger_transaction_id"]

    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2

    debits = await scalar(
        "SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)::bigint "
        "FROM ledger_entries WHERE transaction_id = :tid",
        {"tid": ledger_transaction_id},
    )
    credits = await scalar(
        "SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)::bigint "
        "FROM ledger_entries WHERE transaction_id = :tid",
        {"tid": ledger_transaction_id},
    )
    assert debits == credits == 250000

    # The counterparty is the house settlement account, debited by exactly what the
    # customer was credited -- so the ledger as a whole still sums to zero.
    house_balance = await scalar(
        "SELECT COALESCE(SUM(CASE direction WHEN 'credit' THEN amount ELSE -amount END), 0)"
        "::bigint FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
        "WHERE a.name = :name",
        {"name": settlement_account_name("INR")},
    )
    assert house_balance == -250000
    assert house_balance + await get_balance(client, account) == 0


async def test_a_declined_charge_moves_no_money(client: AsyncClient) -> None:
    """The headline property of Phase 2.

    A forced processor failure must leave the payment recorded as 'failed' and the
    ledger completely untouched: no transaction row, no entry rows, not one half of
    a posting.
    """
    account = await create_account(client, "Customer")

    charge = await create_charge(client, account, 250000, force_outcome="failure")

    assert charge["status"] == "failed"
    assert charge["failure_reason"] is not None
    # The reference survives the decline -- it is what you quote to the processor.
    assert charge["processor_ref"].startswith("fake_ch_")

    # Nothing on the money side. Not "rolled back afterwards" -- never written.
    assert charge["ledger_transaction_id"] is None
    assert await count_rows("ledger_transactions") == 0
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, account) == 0

    # The payment row itself *was* committed: a declined charge is a fact worth
    # keeping, and losing it would leave no evidence the attempt ever happened.
    assert await count_rows("payments") == 1

    # No house account either -- the success path is the only thing that creates one.
    assert await count_rows("accounts") == 1


async def test_a_declined_charge_leaves_an_existing_balance_exactly_as_it_was(
    client: AsyncClient,
) -> None:
    """Before/after: charge, then fail, and prove the first charge is undisturbed."""
    account = await create_account(client, "Customer")

    await create_charge(client, account, 250000)
    balance_before = await get_balance(client, account)
    entries_before = await count_rows("ledger_entries")
    assert balance_before == 250000

    declined = await create_charge(client, account, 99999, force_outcome="failure")
    assert declined["status"] == "failed"

    assert await get_balance(client, account) == balance_before
    assert await count_rows("ledger_entries") == entries_before
    assert await count_rows("payments") == 2


async def test_state_persists_across_the_lifecycle(client: AsyncClient) -> None:
    """Read the payment back from the database, not from the response that wrote it."""
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 4200)

    response = await client.get(f"/charges/{charge['id']}")
    assert response.status_code == 200, response.text
    fetched = response.json()

    assert fetched["status"] == "succeeded"
    assert fetched["processor_ref"] == charge["processor_ref"]
    assert fetched["ledger_transaction_id"] == charge["ledger_transaction_id"]
    assert fetched["amount"] == 4200

    # Straight from the column, past the API and its serialisation entirely.
    stored = await scalar("SELECT status::text FROM payments WHERE id = :id", {"id": charge["id"]})
    assert stored == "succeeded"


async def test_a_declined_payment_persists_as_failed(client: AsyncClient) -> None:
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 4200, force_outcome="failure")

    response = await client.get(f"/charges/{charge['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"

    stored = await scalar("SELECT status::text FROM payments WHERE id = :id", {"id": charge["id"]})
    assert stored == "failed"


async def test_updated_at_advances_past_created_at(client: AsyncClient) -> None:
    """The lifecycle actually moved through states rather than being written once."""
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 4200)

    moved = await scalar(
        "SELECT updated_at > created_at FROM payments WHERE id = :id", {"id": charge["id"]}
    )
    assert moved is True


async def test_charges_reuse_one_settlement_account_per_currency(client: AsyncClient) -> None:
    """The house account is got-or-created, not created per charge."""
    account = await create_account(client, "Customer")

    for _ in range(3):
        await create_charge(client, account, 1000)

    house_accounts = await scalar(
        "SELECT count(*) FROM accounts WHERE name LIKE 'house:%'",
    )
    assert house_accounts == 1
    assert await get_balance(client, account) == 3000


async def test_each_currency_gets_its_own_settlement_account(client: AsyncClient) -> None:
    rupees = await create_account(client, "Rupee customer", currency="INR")
    dollars = await create_account(client, "Dollar customer", currency="USD")

    await create_charge(client, rupees, 250000)
    await create_charge(client, dollars, 12500)

    house_accounts = await scalar("SELECT count(*) FROM accounts WHERE name LIKE 'house:%'")
    assert house_accounts == 2

    # And no posting spans both, or the single-currency invariant would have raised.
    assert await get_balance(client, rupees) == 250000
    assert await get_balance(client, dollars) == 12500


async def test_a_charge_in_a_non_default_currency_succeeds(client: AsyncClient) -> None:
    account = await create_account(client, "Dollar customer", currency="USD")

    charge = await create_charge(client, account, 12500, currency="USD")

    assert charge["status"] == "succeeded"
    assert charge["currency"] == "USD"
    assert await get_balance(client, account) == 12500


async def test_a_currency_mismatch_is_rejected_before_anything_is_written(
    client: AsyncClient,
) -> None:
    """There is no FX here, so a stated currency is an assertion, not a request."""
    account = await create_account(client, "Rupee customer", currency="INR")

    response = await client.post(
        "/charges", json={"account_id": account, "amount": 250000, "currency": "USD"}
    )

    assert response.status_code == 422, response.text
    assert "currency mismatch" in response.json()["detail"]
    assert await count_rows("payments") == 0
    assert await count_rows("ledger_entries") == 0


async def test_a_charge_against_an_unknown_account_is_404(client: AsyncClient) -> None:
    response = await client.post(
        "/charges",
        json={"account_id": "00000000-0000-0000-0000-000000000000", "amount": 1000},
    )

    assert response.status_code == 404, response.text
    assert await count_rows("payments") == 0


async def test_fetching_an_unknown_charge_is_404(client: AsyncClient) -> None:
    response = await client.get("/charges/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


@pytest.mark.parametrize("amount", [0, -1, 100.5, 100.0, "100"])
async def test_a_charge_amount_must_be_positive_minor_units(
    client: AsyncClient, amount: object
) -> None:
    """Same money rule as the ledger: an integer count of minor units, or a 422."""
    account = await create_account(client, "Customer")

    response = await client.post("/charges", json={"account_id": account, "amount": amount})

    assert response.status_code == 422, response.text
    assert await count_rows("payments") == 0
    assert await count_rows("ledger_entries") == 0


async def test_injected_latency_does_not_change_the_outcome(client: AsyncClient) -> None:
    """The latency knob delays the processor; it does not otherwise alter the flow.

    It is also the knob that makes Phase 4's problem visible: this whole delay is
    spent with a Postgres write transaction open.
    """
    account = await create_account(client, "Customer")

    charge = await create_charge(client, account, 1000, force_latency_ms=50)

    assert charge["status"] == "succeeded"
    assert await get_balance(client, account) == 1000


async def test_latency_and_a_forced_failure_combine(client: AsyncClient) -> None:
    account = await create_account(client, "Customer")

    charge = await create_charge(
        client, account, 1000, force_outcome="failure", force_latency_ms=50
    )

    assert charge["status"] == "failed"
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, account) == 0


async def test_the_database_refuses_a_succeeded_payment_with_no_posting(
    client: AsyncClient,
) -> None:
    """The atomicity guarantee is a CHECK constraint, not just careful routing.

    Marking a payment succeeded without a ledger transaction behind it is exactly
    the half-charge this phase forbids, and Postgres will not store it even when
    the application is bypassed entirely.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 1000, force_outcome="failure")

    with pytest.raises((IntegrityError, DBAPIError)) as excinfo:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE payments SET status = 'succeeded' WHERE id = :id"),
                {"id": charge["id"]},
            )

    assert "ck_payments_posting_matches_status" in str(excinfo.value)


async def test_the_database_refuses_a_failed_payment_that_points_at_a_posting(
    client: AsyncClient,
) -> None:
    """The constraint runs both ways: a failed charge may not own money either."""
    account = await create_account(client, "Customer")
    succeeded = await create_charge(client, account, 1000)

    with pytest.raises((IntegrityError, DBAPIError)) as excinfo:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE payments SET status = 'failed' WHERE id = :id"),
                {"id": succeeded["id"]},
            )

    assert "ck_payments_posting_matches_status" in str(excinfo.value)


async def test_payments_are_mutable_unlike_the_ledger(client: AsyncClient) -> None:
    """Sanity check on the asymmetry: only the money is append-only.

    The ledger tables reject UPDATE at the database. ``payments`` must not, or the
    state machine could never move a row at all.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 1000, force_outcome="failure")

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE payments SET failure_reason = 'edited' WHERE id = :id"),
            {"id": charge["id"]},
        )

    reason = await scalar(
        "SELECT failure_reason FROM payments WHERE id = :id", {"id": charge["id"]}
    )
    assert reason == "edited"
