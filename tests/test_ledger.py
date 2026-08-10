"""Phase 1 acceptance tests: the money model, against a real Postgres."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from tests.conftest import count_rows, create_account, get_balance


async def test_balanced_transfer_moves_both_derived_balances(client: AsyncClient) -> None:
    """A two-entry posting moves both balances, and neither is stored anywhere."""
    payer = await create_account(client, "Payer")
    payee = await create_account(client, "Payee")

    assert await get_balance(client, payer) == 0
    assert await get_balance(client, payee) == 0

    # 2,500.00 INR expressed the only way Ledgerline expresses money: 250000 paise.
    response = await client.post(
        "/transactions",
        json={
            "description": "transfer payer -> payee",
            "entries": [
                {"account_id": payer, "direction": "debit", "amount": 250000},
                {"account_id": payee, "direction": "credit", "amount": 250000},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["entries"]) == 2

    # balance = credits - debits, so the debited account goes negative.
    assert await get_balance(client, payer) == -250000
    assert await get_balance(client, payee) == 250000

    # The ledger as a whole still sums to zero.
    assert await get_balance(client, payer) + await get_balance(client, payee) == 0


async def test_balances_accumulate_across_transactions(client: AsyncClient) -> None:
    payer = await create_account(client, "Payer")
    payee = await create_account(client, "Payee")

    for amount in (100, 250, 675):
        response = await client.post(
            "/transactions",
            json={
                "description": f"transfer {amount}",
                "entries": [
                    {"account_id": payer, "direction": "debit", "amount": amount},
                    {"account_id": payee, "direction": "credit", "amount": amount},
                ],
            },
        )
        assert response.status_code == 201, response.text

    assert await get_balance(client, payee) == 1025
    assert await get_balance(client, payer) == -1025


async def test_unbalanced_posting_is_rejected_and_writes_zero_rows(client: AsyncClient) -> None:
    """422 and a full rollback -- no transaction row, no entry rows, no residue."""
    payer = await create_account(client, "Payer")
    payee = await create_account(client, "Payee")

    response = await client.post(
        "/transactions",
        json={
            "description": "debits do not match credits",
            "entries": [
                {"account_id": payer, "direction": "debit", "amount": 250000},
                {"account_id": payee, "direction": "credit", "amount": 249999},
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert "unbalanced" in response.json()["detail"]

    assert await count_rows("ledger_transactions") == 0
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, payer) == 0
    assert await get_balance(client, payee) == 0


async def test_mixed_currency_posting_is_rejected_and_writes_zero_rows(
    client: AsyncClient,
) -> None:
    """100 paise must not appear to offset 100 cents just because both are 100."""
    rupees = await create_account(client, "Rupee account", currency="INR")
    dollars = await create_account(client, "Dollar account", currency="USD")

    response = await client.post(
        "/transactions",
        json={
            "description": "paise against cents",
            "entries": [
                {"account_id": rupees, "direction": "debit", "amount": 100},
                {"account_id": dollars, "direction": "credit", "amount": 100},
            ],
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "mixed-currency" in detail
    assert "INR" in detail and "USD" in detail

    assert await count_rows("ledger_transactions") == 0
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, rupees) == 0
    assert await get_balance(client, dollars) == 0


async def test_mixed_currency_is_rejected_even_when_amounts_sum_to_zero(
    client: AsyncClient,
) -> None:
    """The currency check runs first, so this fails on currency, not on balance."""
    rupees = await create_account(client, "Rupee account", currency="INR")
    dollars_a = await create_account(client, "Dollar account A", currency="USD")
    dollars_b = await create_account(client, "Dollar account B", currency="USD")

    response = await client.post(
        "/transactions",
        json={
            "description": "sums to zero, still nonsense",
            "entries": [
                {"account_id": rupees, "direction": "debit", "amount": 500},
                {"account_id": dollars_a, "direction": "credit", "amount": 300},
                {"account_id": dollars_b, "direction": "credit", "amount": 200},
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert "mixed-currency" in response.json()["detail"]
    assert await count_rows("ledger_entries") == 0


async def test_same_currency_posting_still_succeeds_for_non_default_currency(
    client: AsyncClient,
) -> None:
    """The single-currency rule constrains postings; it does not force INR."""
    payer = await create_account(client, "Payer", currency="USD")
    payee = await create_account(client, "Payee", currency="USD")

    response = await client.post(
        "/transactions",
        json={
            "description": "USD transfer",
            "entries": [
                {"account_id": payer, "direction": "debit", "amount": 12500},
                {"account_id": payee, "direction": "credit", "amount": 12500},
            ],
        },
    )
    assert response.status_code == 201, response.text

    assert await get_balance(client, payer) == -12500
    assert await get_balance(client, payee) == 12500


async def test_single_account_posting_is_not_treated_as_mixed_currency(
    client: AsyncClient,
) -> None:
    """One account, two legs: a single currency, so only the sum check applies."""
    account = await create_account(client, "Solo", currency="INR")

    response = await client.post(
        "/transactions",
        json={
            "description": "debit and credit the same account",
            "entries": [
                {"account_id": account, "direction": "debit", "amount": 700},
                {"account_id": account, "direction": "credit", "amount": 700},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert await get_balance(client, account) == 0


async def test_single_sided_posting_is_rejected(client: AsyncClient) -> None:
    account = await create_account(client, "Lonely")

    response = await client.post(
        "/transactions",
        json={
            "description": "one side only",
            "entries": [{"account_id": account, "direction": "debit", "amount": 500}],
        },
    )
    assert response.status_code == 422, response.text
    assert "single-sided" in response.json()["detail"]
    assert await count_rows("ledger_entries") == 0


async def test_empty_posting_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/transactions", json={"description": "nothing at all", "entries": []}
    )
    assert response.status_code == 422, response.text
    assert await count_rows("ledger_transactions") == 0


@pytest.mark.parametrize("amount", [0, -1, -250000])
async def test_non_positive_amount_is_rejected(client: AsyncClient, amount: int) -> None:
    payer = await create_account(client, "Payer")
    payee = await create_account(client, "Payee")

    response = await client.post(
        "/transactions",
        json={
            "description": "non-positive amount",
            "entries": [
                {"account_id": payer, "direction": "debit", "amount": amount},
                {"account_id": payee, "direction": "credit", "amount": amount},
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert await count_rows("ledger_entries") == 0


@pytest.mark.parametrize("amount", [100.5, 100.0, "100"])
async def test_non_integer_amount_is_rejected(client: AsyncClient, amount: object) -> None:
    """Money is an integer count of minor units. 100.0 is not 100."""
    payer = await create_account(client, "Payer")
    payee = await create_account(client, "Payee")

    response = await client.post(
        "/transactions",
        json={
            "description": "not an integer",
            "entries": [
                {"account_id": payer, "direction": "debit", "amount": amount},
                {"account_id": payee, "direction": "credit", "amount": amount},
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert await count_rows("ledger_entries") == 0


async def test_ledger_entries_reject_update_at_the_database(client: AsyncClient) -> None:
    """Not "the app never updates" -- Postgres itself refuses."""
    entry_id = await _post_one_entry_and_return_its_id(client)

    with pytest.raises(DBAPIError) as excinfo:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE ledger_entries SET amount = 1 WHERE id = :id"), {"id": entry_id}
            )

    assert "append-only" in str(excinfo.value)


async def test_ledger_entries_reject_delete_at_the_database(client: AsyncClient) -> None:
    entry_id = await _post_one_entry_and_return_its_id(client)

    with pytest.raises(DBAPIError) as excinfo:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id}
            )

    assert "append-only" in str(excinfo.value)
    assert await count_rows("ledger_entries") == 2


async def test_ledger_transactions_reject_update_and_delete(client: AsyncClient) -> None:
    await _post_one_entry_and_return_its_id(client)

    for statement in (
        "UPDATE ledger_transactions SET description = 'rewritten'",
        "DELETE FROM ledger_transactions",
    ):
        with pytest.raises(DBAPIError) as excinfo:
            async with engine.begin() as conn:
                await conn.execute(text(statement))
        assert "append-only" in str(excinfo.value)

    assert await count_rows("ledger_transactions") == 1


async def test_amount_check_constraint_blocks_direct_insert(client: AsyncClient) -> None:
    """Even bypassing the API, the database will not store a non-positive amount."""
    account = await create_account(client, "Direct")

    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO ledger_transactions (description) VALUES ('direct')
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO ledger_entries (transaction_id, account_id, direction, amount)
                    SELECT id, :account_id, 'debit', 0 FROM ledger_transactions LIMIT 1
                    """
                ),
                {"account_id": account},
            )


async def test_balance_of_unknown_account_is_404(client: AsyncClient) -> None:
    response = await client.get("/accounts/00000000-0000-0000-0000-000000000000/balance")
    assert response.status_code == 404


async def test_posting_to_unknown_account_is_rejected(client: AsyncClient) -> None:
    account = await create_account(client, "Real")
    ghost = "00000000-0000-0000-0000-000000000000"

    response = await client.post(
        "/transactions",
        json={
            "description": "one leg points nowhere",
            "entries": [
                {"account_id": account, "direction": "debit", "amount": 100},
                {"account_id": ghost, "direction": "credit", "amount": 100},
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert "unknown account_id" in response.json()["detail"]
    assert await count_rows("ledger_entries") == 0


async def test_account_defaults_to_inr(client: AsyncClient) -> None:
    response = await client.post("/accounts", json={"name": "Defaulted"})
    assert response.status_code == 201, response.text
    assert response.json()["currency"] == "INR"


async def _post_one_entry_and_return_its_id(client: AsyncClient) -> str:
    """Post a balanced transfer and hand back one entry id to attack."""
    payer = await create_account(client, "Payer")
    payee = await create_account(client, "Payee")

    response = await client.post(
        "/transactions",
        json={
            "description": "a posting that must never change",
            "entries": [
                {"account_id": payer, "direction": "debit", "amount": 4200},
                {"account_id": payee, "direction": "credit", "amount": 4200},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["entries"][0]["id"]
