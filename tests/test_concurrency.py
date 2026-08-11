"""Phase 4: the two races, reproduced and then fixed.

## How the concurrency here is real

Every test in this file fires N requests through ``asyncio.gather``. That is not a
loop wearing a costume, and the chain is worth spelling out because a concurrency
suite that quietly serialises proves the opposite of what it claims:

1. ``asyncio.gather`` schedules all N coroutines before any of them completes.
2. Each request is handled by FastAPI's ``get_session`` dependency, which builds a
   **separate ``AsyncSession``** per request.
3. Each session checks out a **separate asyncpg connection** from the pool, sized
   in ``app/db.py`` to 25 + 35 overflow so that 50 requests do not queue.
4. Each connection is a **separate Postgres backend running a separate
   transaction**. They are concurrent in the database, which is the only place
   that counts.

``test_the_requests_actually_overlap`` asserts this rather than trusting it: N
requests that each hold a 300ms processor call must finish in far less than N ×
300ms, which is only possible if they ran at the same time.

Determinism comes from ``NAIVE_RACE_WINDOW_MS``, which widens the check-then-act
window on the naive paths. It does not create either bug -- both windows exist at
zero -- it makes them reproducible on every run instead of on unlucky ones.

## The preserved failures

``test_the_naive_claim_double_charges`` and
``test_the_unguarded_withdrawal_overdraws`` assert that the *broken* code is still
broken. They are not xfails: an xfail passes when the thing silently stops
happening, and a reproduction that stopped reproducing is exactly what you need to
be told about. If either starts failing, the before/after in the README has become
fiction and should be re-measured.
"""

import asyncio
import time
import uuid
from collections import Counter

import pytest
from httpx import AsyncClient, Response

from app.config import settings
from app.strategies import ClaimStrategy, WithdrawalGuard
from tests.conftest import count_rows, create_account, get_balance, idempotency_headers

CONCURRENCY = 50
PROCESSOR_LATENCY_MS = 300
RACE_WINDOW_MS = 300


async def timed(coro) -> tuple[Response, float]:
    """Run a request and report how long it personally took."""
    started = time.perf_counter()
    response = await coro
    return response, time.perf_counter() - started


def status_counts(results: list) -> Counter:
    return Counter(
        f"EXC:{type(r).__name__}" if isinstance(r, BaseException) else r.status_code
        for r in results
    )


async def fund(client: AsyncClient, account: str, amount: int) -> None:
    """Put money in an account via a charge, so withdrawals have something to take."""
    response = await client.post(
        "/charges",
        json={"account_id": account, "amount": amount},
        headers=idempotency_headers(),
    )
    assert response.status_code == 201, response.text


# --- Race 1: two requests, one idempotency key ---------------------------------


async def test_the_requests_actually_overlap(client: AsyncClient) -> None:
    """Prove the harness is concurrent before trusting anything it says.

    Twelve charges, each holding a 300ms processor call, against twelve different
    keys so nothing serialises them on purpose. Run sequentially that is 3.6
    seconds. Anything close to that means the pool, the event loop, or the test
    itself is serialising, and every other test in this file would be measuring
    nothing.
    """
    account = await create_account(client, "Customer")
    n = 12

    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            client.post(
                "/charges",
                json={
                    "account_id": account,
                    "amount": 100,
                    "force_latency_ms": PROCESSOR_LATENCY_MS,
                },
                headers=idempotency_headers(),
            )
            for _ in range(n)
        )
    )
    elapsed = time.perf_counter() - started

    assert all(r.status_code == 201 for r in results)
    sequential = n * PROCESSOR_LATENCY_MS / 1000
    # A 2x margin rather than a tight one: the claim being tested is "these
    # overlapped at all", which a 2x speedup already establishes beyond argument,
    # and CI runners are slower and noisier than a laptop. Locally this comes in
    # around 0.4s against a 3.6s sequential baseline.
    assert elapsed < sequential / 2, (
        f"{n} requests took {elapsed:.2f}s; sequential would be {sequential:.2f}s. "
        "They are not running concurrently."
    )
    assert await count_rows("payments") == n


async def test_fifty_concurrent_same_key_charges_produce_exactly_one_payment(
    client: AsyncClient,
) -> None:
    """The Phase 4 acceptance criterion, on the shipped path.

    Fifty requests, one key, all in flight together. Exactly one may charge.
    """
    account = await create_account(client, "Customer")
    key = str(uuid.uuid4())
    body = {
        "account_id": account,
        "amount": 250000,
        "force_latency_ms": PROCESSOR_LATENCY_MS,
    }

    results = await asyncio.gather(
        *(
            client.post("/charges", json=body, headers={"Idempotency-Key": key})
            for _ in range(CONCURRENCY)
        ),
        return_exceptions=True,
    )

    counts = status_counts(results)
    assert set(counts) <= {201, 409}, f"unexpected outcomes: {counts}"
    assert counts[201] >= 1

    # One payment, one posting, one key. This is the whole phase.
    assert await count_rows("payments") == 1
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await count_rows("idempotency_keys") == 1
    assert await get_balance(client, account) == 250000


async def test_the_losers_are_rejected_fast_rather_than_parked(
    client: AsyncClient,
) -> None:
    """The defect the advisory lock actually fixes.

    Without ``pg_try_advisory_xact_lock`` the duplicate requests are still
    *correct* -- Postgres makes them wait on the winner's uncommitted claim row and
    they replay once it commits. They are simply asleep for the length of a card
    authorisation, holding a connection each. Fifty retries of one charge become
    fifty backends parked behind one slow authorisation.

    Two things separate the fixed path from that, and only one of them is a
    stopwatch:

    * **structurally**, a parked request eventually returns 201 (it replays),
      whereas a turned-away request returns 409. Seeing any 409 at all is proof it
      was refused rather than queued.
    * **in time**, the refusal must cost milliseconds, not the length of the
      processor call.

    Deliberately run at low concurrency. Wall-clock time per request includes every
    slice the event loop spent on the *other* requests, so at fifty-way concurrency
    every response looks slow regardless of what it did -- measured at 50, the
    refusals appear to take ~740ms against a 300ms processor call, which says
    nothing about locking and everything about sharing one thread. At eight, the
    signal is the lock.
    """
    account = await create_account(client, "Customer")
    n = 8
    latency_ms = 1200

    # Warm the pool first: establishing eight fresh asyncpg connections would
    # otherwise be charged to the requests being measured.
    await asyncio.gather(*(get_balance(client, account) for _ in range(n)))

    key = str(uuid.uuid4())
    body = {"account_id": account, "amount": 1000, "force_latency_ms": latency_ms}

    results = await asyncio.gather(
        *(
            timed(client.post("/charges", json=body, headers={"Idempotency-Key": key}))
            for _ in range(n)
        )
    )

    latency = latency_ms / 1000
    rejected = [seconds for response, seconds in results if response.status_code == 409]
    winners = [seconds for response, seconds in results if response.status_code == 201]

    assert rejected, "no request was refused -- the losers are being parked, not turned away"
    assert len(winners) == 1

    # The winner genuinely spent the processor call; the losers genuinely did not.
    assert winners[0] > latency * 0.8
    assert max(rejected) < latency / 4, (
        f"losers took up to {max(rejected) * 1000:.0f}ms against a {latency_ms}ms "
        "processor call -- they are blocking on the winner, not being turned away"
    )
    assert await count_rows("payments") == 1


@pytest.mark.race
async def test_the_naive_claim_double_charges(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESERVED REPRODUCTION -- asserts the broken claim is still broken.

    Read the key, find nothing, charge, then write the key with ON CONFLICT DO
    NOTHING. Every request reads "nothing" before any of them writes, so every
    request charges. The idempotency key is written exactly once and prevents
    exactly nothing.

    A failure here means the reproduction stopped reproducing, and the README's
    before/after needs re-measuring -- not that the bug is fixed.
    """
    monkeypatch.setattr(settings, "IDEMPOTENCY_CLAIM_STRATEGY", ClaimStrategy.NAIVE)
    monkeypatch.setattr(settings, "NAIVE_RACE_WINDOW_MS", RACE_WINDOW_MS)

    account = await create_account(client, "Customer")
    key = str(uuid.uuid4())
    body = {"account_id": account, "amount": 250000}

    results = await asyncio.gather(
        *(
            client.post("/charges", json=body, headers={"Idempotency-Key": key})
            for _ in range(CONCURRENCY)
        ),
        return_exceptions=True,
    )

    payments = await count_rows("payments")
    postings = await count_rows("ledger_transactions")
    keys = await count_rows("idempotency_keys")

    assert payments > 1, (
        f"expected the naive claim to double-charge, got {payments} payment(s). "
        f"statuses: {status_counts(results)}"
    )
    # The shape of the bug: many payments, many postings, and one lonely key that
    # was supposed to stop all of it.
    assert postings == payments
    assert keys == 1
    assert await get_balance(client, account) == payments * 250000


@pytest.mark.race
async def test_the_naive_claim_is_fine_when_requests_are_serial(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the bug survived three phases of tests.

    Sent one after another, the naive claim behaves perfectly: the second request
    finds the first one's key and replays it. Every idempotency test written in
    Phase 3 passes against this code. Concurrency is the only thing that reveals
    it, which is exactly why it needs a harness rather than a code review.
    """
    monkeypatch.setattr(settings, "IDEMPOTENCY_CLAIM_STRATEGY", ClaimStrategy.NAIVE)

    account = await create_account(client, "Customer")
    key = str(uuid.uuid4())
    body = {"account_id": account, "amount": 250000}

    first = await client.post("/charges", json=body, headers={"Idempotency-Key": key})
    second = await client.post("/charges", json=body, headers={"Idempotency-Key": key})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert await count_rows("payments") == 1


# --- Race 2: two withdrawals, one account --------------------------------------


async def test_concurrent_withdrawals_never_breach_the_floor(
    client: AsyncClient,
) -> None:
    """The Phase 4 acceptance criterion for overdraw, repeated across rounds.

    Fund 100,000 and demand 200,000 in twenty simultaneous withdrawals. Exactly ten
    can be honoured. The arithmetic is deterministic under the row lock: each
    withdrawal reads a balance that already includes every withdrawal ahead of it.
    """
    for round_number in range(3):
        account = await create_account(client, f"Customer {round_number}")
        await fund(client, account, 100000)

        results = await asyncio.gather(
            *(
                client.post(
                    "/withdrawals", json={"account_id": account, "amount": 10000}
                )
                for _ in range(20)
            ),
            return_exceptions=True,
        )

        counts = status_counts(results)
        assert set(counts) <= {201, 422}, f"round {round_number}: {counts}"
        assert counts[201] == 10, f"round {round_number}: {counts}"
        assert counts[422] == 10, f"round {round_number}: {counts}"

        balance = await get_balance(client, account)
        assert balance == 0, f"round {round_number}: balance {balance}"
        assert balance >= settings.BALANCE_FLOOR_MINOR_UNITS


async def test_uneven_concurrent_withdrawals_still_cannot_overdraw(
    client: AsyncClient,
) -> None:
    """Amounts that do not divide evenly into the balance still cannot breach it."""
    account = await create_account(client, "Customer")
    await fund(client, account, 100000)

    amounts = [7000, 31000, 15000, 44000, 22000, 9000, 63000, 12000]
    results = await asyncio.gather(
        *(
            client.post("/withdrawals", json={"account_id": account, "amount": amount})
            for amount in amounts
        ),
        return_exceptions=True,
    )

    honoured = sum(
        r.json()["amount"]
        for r in results
        if not isinstance(r, BaseException) and r.status_code == 201
    )
    balance = await get_balance(client, account)

    assert balance == 100000 - honoured
    assert balance >= settings.BALANCE_FLOOR_MINOR_UNITS, f"overdrawn to {balance}"


async def test_withdrawals_on_different_accounts_do_not_serialise(
    client: AsyncClient,
) -> None:
    """The lock is per account, not global.

    Ten withdrawals against ten different accounts contend for nothing and must all
    succeed. A global lock would also produce a correct balance, and would make
    every account in the system wait behind every other one.
    """
    accounts = [await create_account(client, f"Customer {i}") for i in range(10)]
    for account in accounts:
        await fund(client, account, 50000)

    results = await asyncio.gather(
        *(
            client.post("/withdrawals", json={"account_id": account, "amount": 50000})
            for account in accounts
        )
    )

    assert all(r.status_code == 201 for r in results)
    for account in accounts:
        assert await get_balance(client, account) == 0


@pytest.mark.race
async def test_the_unguarded_withdrawal_overdraws(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESERVED REPRODUCTION -- asserts the unlocked balance check still overdraws.

    Twenty withdrawals of 10,000 against a balance of 100,000, with no lock. Every
    one of them reads 100,000, every one concludes there is plenty, and every one
    writes. The account ends deeply negative and no single request did anything
    wrong.
    """
    monkeypatch.setattr(settings, "WITHDRAWAL_GUARD", WithdrawalGuard.NAIVE)
    monkeypatch.setattr(settings, "NAIVE_RACE_WINDOW_MS", RACE_WINDOW_MS)

    account = await create_account(client, "Customer")
    await fund(client, account, 100000)

    results = await asyncio.gather(
        *(
            client.post("/withdrawals", json={"account_id": account, "amount": 10000})
            for _ in range(20)
        ),
        return_exceptions=True,
    )

    counts = status_counts(results)
    balance = await get_balance(client, account)

    assert balance < 0, (
        f"expected the unguarded withdrawal to overdraw, balance is {balance}. "
        f"statuses: {counts}"
    )
    assert counts[201] > 10, f"expected more than 10 withdrawals to slip through: {counts}"
    # The ledger is still internally consistent -- every posting balances. It is the
    # *rule* that was broken, not the bookkeeping, which is what makes this class of
    # bug so hard to spot after the fact.
    assert await count_rows("ledger_entries") == 2 * (counts[201] + 1)
