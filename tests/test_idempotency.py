"""Phase 3 acceptance tests: retrying a charge must not charge twice.

The scenario throughout is the boring one that costs real money: the same request
arrives more than once because a customer double-clicked, or a client retried a
POST whose response it never saw. Every test here sends requests **serially** --
two genuinely simultaneous requests on one key are Phase 4's problem, and nothing
in this file pretends otherwise.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import engine
from app.idempotency import request_fingerprint
from tests.conftest import (
    count_rows,
    create_account,
    create_charge,
    get_balance,
    post_charge,
    scalar,
)

KEY = "idem-key-fixed-for-the-test"


async def expire_key(key: str) -> None:
    """Age a key past its TTL, standing in for 24 hours passing."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys SET created_at = now() - interval '25 hours', "
                "expires_at = now() - interval '1 hour' WHERE key = :key"
            ),
            {"key": key},
        )


async def test_the_same_charge_sent_twice_happens_once(client: AsyncClient) -> None:
    """The headline property: one key, one charge, whatever the client does."""
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 250000, key=KEY)
    second = await post_charge(client, account, 250000, key=KEY)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    # Byte-identical, not merely equivalent. Compared as raw bytes so a difference
    # in key order or number formatting would fail here rather than pass quietly.
    assert first.content == second.content
    assert first.json()["id"] == second.json()["id"]

    # Exactly one of everything on the money side.
    assert await count_rows("payments") == 1
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await count_rows("idempotency_keys") == 1

    # And the balance moved once, not twice.
    assert await get_balance(client, account) == 250000


async def test_a_replay_does_not_call_the_processor_again(client: AsyncClient) -> None:
    """The retry is answered from the record, not by asking the card again.

    Proved by flipping the processor's forced outcome on the second call. If the
    processor were consulted the response would be a decline; because the key
    already holds an answer, the original success replays unchanged.
    """
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 250000, key=KEY, force_outcome="success")
    second = await post_charge(client, account, 250000, key=KEY, force_outcome="failure")

    assert first.json()["status"] == "succeeded"
    assert second.json()["status"] == "succeeded"
    assert first.content == second.content
    assert await count_rows("payments") == 1
    assert await get_balance(client, account) == 250000


async def test_the_failure_path_is_idempotent_too(client: AsyncClient) -> None:
    """A decline is a recorded outcome, so a retry replays it rather than re-trying.

    This matters more than the success case: retrying a declined card is how a
    customer collects three failed-payment emails and a fraud flag for one
    purchase attempt.
    """
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 250000, key=KEY, force_outcome="failure")
    second = await post_charge(client, account, 250000, key=KEY, force_outcome="failure")

    assert first.status_code == 201
    assert first.json()["status"] == "failed"
    assert first.content == second.content

    assert await count_rows("payments") == 1
    # Still the Phase 2 guarantee: a declined charge moves nothing, twice over.
    assert await count_rows("ledger_entries") == 0
    assert await get_balance(client, account) == 0


async def test_the_same_key_with_a_different_amount_is_rejected(client: AsyncClient) -> None:
    """A key is a promise about one specific request."""
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 250000, key=KEY)
    assert first.status_code == 201

    second = await post_charge(client, account, 99999, key=KEY)

    assert second.status_code == 422, second.text
    assert "different payload" in second.json()["detail"]

    # Nothing new written, and the original is untouched.
    assert await count_rows("payments") == 1
    assert await count_rows("ledger_entries") == 2
    assert await get_balance(client, account) == 250000

    stored = await scalar(
        "SELECT response_snapshot->>'amount' FROM idempotency_keys WHERE key = :key",
        {"key": KEY},
    )
    assert stored == "250000"


async def test_the_same_key_against_a_different_account_is_rejected(
    client: AsyncClient,
) -> None:
    account = await create_account(client, "Customer")
    other = await create_account(client, "Someone else")

    await create_charge(client, account, 1000, key=KEY)
    second = await post_charge(client, other, 1000, key=KEY)

    assert second.status_code == 422, second.text
    assert await count_rows("payments") == 1
    assert await get_balance(client, other) == 0


async def test_a_missing_idempotency_key_is_a_400(client: AsyncClient) -> None:
    account = await create_account(client, "Customer")

    response = await client.post("/charges", json={"account_id": account, "amount": 1000})

    assert response.status_code == 400, response.text
    assert "Idempotency-Key" in response.json()["detail"]
    assert await count_rows("payments") == 0


async def test_a_blank_idempotency_key_is_a_400(client: AsyncClient) -> None:
    """Whitespace is not a key. Accepting it would give every such caller one key."""
    account = await create_account(client, "Customer")

    response = await client.post(
        "/charges",
        json={"account_id": account, "amount": 1000},
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 400, response.text
    assert await count_rows("payments") == 0


async def test_an_over_long_idempotency_key_is_a_400(client: AsyncClient) -> None:
    account = await create_account(client, "Customer")

    response = await client.post(
        "/charges",
        json={"account_id": account, "amount": 1000},
        headers={"Idempotency-Key": "k" * 256},
    )

    assert response.status_code == 400, response.text
    assert await count_rows("payments") == 0


async def test_different_keys_are_different_charges(client: AsyncClient) -> None:
    """Idempotency must not become deduplication -- two real purchases are two."""
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 250000, key="key-one")
    second = await post_charge(client, account, 250000, key="key-two")

    assert first.json()["id"] != second.json()["id"]
    assert await count_rows("payments") == 2
    assert await count_rows("ledger_entries") == 4
    assert await get_balance(client, account) == 500000


async def test_an_expired_key_behaves_as_a_fresh_one(client: AsyncClient) -> None:
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 250000, key=KEY)
    assert first.status_code == 201

    await expire_key(KEY)

    second = await post_charge(client, account, 250000, key=KEY)

    assert second.status_code == 201, second.text
    # A genuinely new charge, not a replay.
    assert second.json()["id"] != first.json()["id"]
    assert await count_rows("payments") == 2
    assert await get_balance(client, account) == 500000

    # The row was reclaimed in place rather than duplicated, and now holds the
    # newer response.
    assert await count_rows("idempotency_keys") == 1
    replayed = await post_charge(client, account, 250000, key=KEY)
    assert replayed.content == second.content


async def test_an_expired_key_may_be_reused_for_a_different_payload(
    client: AsyncClient,
) -> None:
    """Expiry resets the binding, not just the clock."""
    account = await create_account(client, "Customer")

    await create_charge(client, account, 250000, key=KEY)
    await expire_key(KEY)

    second = await post_charge(client, account, 111, key=KEY)

    assert second.status_code == 201, second.text
    assert await count_rows("payments") == 2


async def test_the_processor_test_knobs_are_excluded_from_the_fingerprint() -> None:
    """force_outcome / force_latency_ms must not make a request 'different'.

    Tested at the function rather than through the API because it is a statement
    about what the hash covers, and the route only ever shows the consequence.
    """
    account_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    baseline = request_fingerprint(account_id=account_id, amount=1000, currency=None)

    # The knobs are simply not arguments -- there is no way to feed them in.
    assert baseline == request_fingerprint(account_id=account_id, amount=1000, currency=None)

    # But the things that define a charge do change it.
    assert baseline != request_fingerprint(account_id=account_id, amount=1001, currency=None)
    assert baseline != request_fingerprint(account_id=account_id, amount=1000, currency="INR")


async def test_a_latency_knob_change_still_replays(client: AsyncClient) -> None:
    """The API-level consequence of the exclusion above."""
    account = await create_account(client, "Customer")

    first = await post_charge(client, account, 1000, key=KEY, force_latency_ms=0)
    second = await post_charge(client, account, 1000, key=KEY, force_latency_ms=200)

    assert second.status_code == 201, second.text
    assert first.content == second.content
    assert await count_rows("payments") == 1


async def test_a_stated_currency_is_a_different_fingerprint_than_an_omitted_one(
    client: AsyncClient,
) -> None:
    """Documented conservative choice: the hash is over the body as sent.

    Omitting `currency` and stating the account's own currency resolve to the same
    charge, but they are different request bodies, so reusing one key across both
    is rejected rather than silently replayed.
    """
    account = await create_account(client, "Customer", currency="INR")

    first = await post_charge(client, account, 1000, key=KEY)
    assert first.status_code == 201

    second = await post_charge(client, account, 1000, key=KEY, currency="INR")

    assert second.status_code == 422, second.text
    assert await count_rows("payments") == 1


async def test_a_key_is_recorded_as_completed_with_its_response(client: AsyncClient) -> None:
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, 4200, key=KEY)

    row_status = await scalar(
        "SELECT status FROM idempotency_keys WHERE key = :key", {"key": KEY}
    )
    response_status = await scalar(
        "SELECT response_status FROM idempotency_keys WHERE key = :key", {"key": KEY}
    )
    snapshot_id = await scalar(
        "SELECT response_snapshot->>'id' FROM idempotency_keys WHERE key = :key",
        {"key": KEY},
    )

    assert row_status == "completed"
    assert response_status == 201
    assert snapshot_id == charge["id"]


async def test_a_failed_request_does_not_consume_its_key(client: AsyncClient) -> None:
    """An error before completion leaves the key free, so a retry can succeed.

    This is what the claim sharing the charge's transaction buys: there is no
    half-finished key to clean up, because a key that did not commit does not
    exist.
    """
    ghost = "00000000-0000-0000-0000-000000000000"

    failed = await client.post(
        "/charges",
        json={"account_id": ghost, "amount": 1000},
        headers={"Idempotency-Key": KEY},
    )
    assert failed.status_code == 404
    assert await count_rows("idempotency_keys") == 0

    # Same key, now against a real account: allowed, because the key was never
    # consumed by the request that failed.
    account = await create_account(client, "Customer")
    retried = await post_charge(client, account, 1000, key=KEY)

    assert retried.status_code == 201, retried.text
    assert await get_balance(client, account) == 1000


async def test_the_database_rejects_a_duplicate_key(client: AsyncClient) -> None:
    """Ownership of a key is the primary key, not an application-level check."""
    account = await create_account(client, "Customer")
    await create_charge(client, account, 1000, key=KEY)

    with pytest.raises((IntegrityError, DBAPIError)):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO idempotency_keys (key, request_hash, expires_at) "
                    "VALUES (:key, 'whatever', now() + interval '1 day')"
                ),
                {"key": KEY},
            )


async def test_the_database_rejects_a_completed_key_with_no_snapshot(
    client: AsyncClient,
) -> None:
    """A 'completed' key that replays nothing is the failure mode worth blocking."""
    account = await create_account(client, "Customer")
    await create_charge(client, account, 1000, key=KEY)

    with pytest.raises((IntegrityError, DBAPIError)) as excinfo:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE idempotency_keys SET response_snapshot = NULL, "
                    "response_status = NULL WHERE key = :key"
                ),
                {"key": KEY},
            )

    assert "ck_idempotency_keys_snapshot_matches_status" in str(excinfo.value)
