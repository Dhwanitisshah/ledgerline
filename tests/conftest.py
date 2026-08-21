"""Test fixtures. These run against a real Postgres, not a fake.

The whole point of Phase 1 is behaviour the database provides -- CHECK
constraints, triggers, transactional rollback, SUM semantics. A SQLite stand-in
or a mocked session would test none of it.
"""

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from app.db import engine
from app.main import app

# TRUNCATE, not DELETE, and deliberately so: the append-only triggers reject
# DELETE on the ledger tables, and TRUNCATE does not fire UPDATE/DELETE triggers.
# This is the single sanctioned way to clear ledger state, and it exists only for
# tests -- no application code path removes a ledger row.
#
# ``processor_charges`` and ``event_deliveries`` are in the list but are not
# Ledgerline's data (see app/models.py). They are cleared between tests for the
# same reason the rest is: a fake processor that remembered yesterday's charges
# would let one test's attempt reference resolve inside another test's sweep, and a
# consumer that remembered yesterday's deliveries would make every event in the
# next test look like a duplicate that had already been handled.
_TRUNCATE_SQL = text(
    "TRUNCATE TABLE ledger_entries, ledger_transactions, payments, "
    "idempotency_keys, processor_charges, outbox_events, event_deliveries, "
    "webhook_events, accounts RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with engine.begin() as conn:
        await conn.execute(_TRUNCATE_SQL)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client

    # pytest-asyncio gives each test a fresh event loop, and asyncpg connections
    # cannot outlive the loop that created them. Drop the pool between tests.
    await engine.dispose()


async def count_rows(table: str) -> int:
    """Row count straight from the database, bypassing the app entirely."""
    async with engine.connect() as conn:
        result = await conn.execute(text(f"SELECT count(*) FROM {table}"))
        return int(result.scalar_one())


async def create_account(client: AsyncClient, name: str, currency: str = "INR") -> str:
    response = await client.post("/accounts", json={"name": name, "currency": currency})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def get_balance(client: AsyncClient, account_id: str) -> int:
    response = await client.get(f"/accounts/{account_id}/balance")
    assert response.status_code == 200, response.text
    return response.json()["balance"]


async def scalar(sql: str, params: dict | None = None) -> object:
    """Run a scalar query straight against the database, bypassing the app."""
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return result.scalar_one()


def idempotency_headers(key: str | None = None) -> dict[str, str]:
    """Headers for POST /charges, with a fresh key unless one is given.

    Defaulting to a new UUID per call keeps every test that is *not* about
    idempotency behaving as it did before the header became mandatory: each call
    is a distinct charge. Tests that care pass an explicit key.
    """
    return {"Idempotency-Key": key or str(uuid.uuid4())}


async def post_charge(
    client: AsyncClient,
    account_id: str,
    amount: int,
    *,
    key: str | None = None,
    **overrides: object,
) -> Response:
    """POST /charges and return the raw response, asserting nothing.

    Raw rather than parsed because the idempotency tests compare response bytes,
    and ``response.json()`` would throw that comparison away.
    """
    body: dict[str, object] = {"account_id": account_id, "amount": amount}
    body.update(overrides)

    return await client.post("/charges", json=body, headers=idempotency_headers(key))


async def create_charge(
    client: AsyncClient,
    account_id: str,
    amount: int,
    *,
    key: str | None = None,
    **overrides: object,
) -> dict:
    """POST /charges and return the body, asserting it was accepted.

    ``overrides`` carries the fake-processor knobs (``force_outcome``,
    ``force_latency_ms``) and anything else the body accepts, so a test can force
    a decline without touching environment variables.
    """
    response = await post_charge(client, account_id, amount, key=key, **overrides)
    assert response.status_code == 201, response.text
    return response.json()
