"""Phase 5b: processor callbacks, delivered at least once, taking effect once.

The provider's contract is at-least-once and there is no opting out of it: it posts
an event, gets no response, and cannot distinguish "never arrived" from "handled,
response lost". So it sends again. A receiver that assumes single delivery settles
a payment twice, and does so under precisely the conditions -- a slow response, a
deploy, a timeout -- that make it hardest to notice.

Two things are under test here and they are independent:

1. **Deduplication by event id.** The same event twice is handled once.
2. **The payload is not the authority.** The webhook says *when* to settle; the
   processor's books say *what* the settlement is. ``test_the_processor_decides_the
   _outcome_not_the_payload`` is the one that matters, because the obvious
   implementation -- read ``type``, write the matching status -- passes every
   dedupe test in this file and is still wrong.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient, Response

from app.db import async_session
from app.deps import processor_books
from app.models import PaymentStatus
from app.processor import FakeProcessor
from app.reconcile import ReconcileOutcome, sweep_once
from app.routers.charges import SimulatedCrash
from tests.conftest import (
    count_rows,
    create_account,
    create_charge,
    get_balance,
    post_charge,
    scalar,
)

KEY = "phase5b-webhook-key"
AMOUNT = 250000


def event_body(payment_id: str, *, event_id: str, event_type: str = "charge.succeeded") -> dict:
    """One processor callback, in the shape a provider posts.

    ``attempt_ref`` is the payment id because that is what Ledgerline sent the
    processor as its idempotency key; see app/routers/charges.py.
    """
    return {"id": event_id, "type": event_type, "data": {"attempt_ref": payment_id}}


async def send_webhook(client: AsyncClient, body: dict) -> Response:
    return await client.post("/webhooks", json=body)


async def crash_a_charge(
    client: AsyncClient, account: str, *, key: str = KEY, **overrides: object
) -> str:
    """Abandon a charge at the fatal instant, and return the stranded payment id."""
    with pytest.raises(SimulatedCrash):
        await post_charge(
            client, account, AMOUNT, key=key, force_crash_after_processor=True, **overrides
        )
    return str(await scalar("SELECT id FROM payments ORDER BY created_at DESC LIMIT 1"))


# --- The push path settles what the pull path would have --------------------------


async def test_a_webhook_settles_a_payment_whose_request_died(client: AsyncClient) -> None:
    """The push counterpart of the Phase 5a sweep, and the reason it is worth having.

    Phase 5a's guarantee was *eventual*: a stranded payment is resolved within
    ``RECONCILE_STUCK_AFTER_SECONDS`` plus one sweep interval. The webhook collapses
    that to the moment the processor knows, without weakening anything -- the same
    ``reconcile_payment`` does the work, on the same authority.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)

    assert await get_balance(client, account) == 0

    response = await send_webhook(client, event_body(payment_id, event_id="evt_1"))

    assert response.status_code == 200, response.text
    assert response.json()["duplicate"] is False
    assert response.json()["outcome"] == ReconcileOutcome.SETTLED_SUCCEEDED

    # Settled, posted, and announced -- all in the webhook's single transaction.
    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.SUCCEEDED
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await count_rows("outbox_events") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_the_same_webhook_delivered_twice_takes_effect_once(
    client: AsyncClient,
) -> None:
    """THE RECEIVER PROPERTY: a duplicate delivery is a no-op.

    Byte-identical body, same event id, sent twice -- which is exactly what a
    provider does when it does not hear back in time. The second delivery must
    change nothing: not the payment, not the ledger, not the outbox, not even the
    recorded outcome of the first one.

    It also returns 200 rather than an error. A provider reads any non-2xx as "try
    again", so answering a duplicate with a 409 would ask it to keep sending an
    event that has already been handled, forever.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)
    body = event_body(payment_id, event_id="evt_duplicate")

    first = await send_webhook(client, body)
    second = await send_webhook(client, body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True

    # The duplicate quotes the original delivery's outcome rather than recomputing
    # one, so a provider replaying an event gets a consistent answer every time.
    assert second.json()["outcome"] == first.json()["outcome"]

    # One of everything. The second delivery did no work at all.
    assert await count_rows("webhook_events") == 1
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await count_rows("outbox_events") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_two_copies_of_one_webhook_arriving_at_once_settle_once(
    client: AsyncClient,
) -> None:
    """Simultaneous redelivery, which is the case a SELECT-then-INSERT would lose.

    Providers retry on a timer, and a retry can overlap the delivery it is retrying
    -- that is the whole reason the first attempt looked slow. Both requests find no
    row, both would proceed, and only the unique index decides it. The loser blocks
    on the winner's uncommitted row, then finds the conflict and does nothing.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)
    body = event_body(payment_id, event_id="evt_simultaneous")

    left, right = await asyncio.gather(send_webhook(client, body), send_webhook(client, body))

    assert {left.status_code, right.status_code} == {200}
    assert sorted([left.json()["duplicate"], right.json()["duplicate"]]) == [False, True]

    assert await count_rows("webhook_events") == 1
    assert await count_rows("ledger_transactions") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_a_new_event_id_for_a_settled_payment_still_changes_nothing(
    client: AsyncClient,
) -> None:
    """The second layer of idempotency, with the first one removed.

    Some providers issue a fresh event id when they retry certain event classes, so
    dedupe-by-id is not the last line of defence. Here a genuinely new event arrives
    for a payment that is already settled: it is claimed, handled, and the handling
    correctly does nothing, because ``reconcile_payment`` locks on
    ``status = 'processing'`` and a settled payment is not that.

    Not prevented by a check. Unrepresentable: there is no second move out of a
    terminal state in the state machine.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)

    await send_webhook(client, event_body(payment_id, event_id="evt_first"))
    again = await send_webhook(client, event_body(payment_id, event_id="evt_second"))

    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is False
    assert again.json()["outcome"] == ReconcileOutcome.SKIPPED

    # Two events recorded, one effect.
    assert await count_rows("webhook_events") == 2
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("outbox_events") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_a_webhook_about_a_healthy_charge_does_nothing(client: AsyncClient) -> None:
    """The common case in production: the callback arrives after we already know.

    A charge that completed inline settles itself, and the processor's webhook shows
    up moments later about a payment with nothing left to decide. That is not an
    error and must not read like one -- it is the system working, twice.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT, key=KEY)

    response = await send_webhook(client, event_body(charge["id"], event_id="evt_late"))

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == ReconcileOutcome.SKIPPED
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("outbox_events") == 1
    assert await get_balance(client, account) == AMOUNT


# --- What the receiver refuses to trust -------------------------------------------


async def test_the_processor_decides_the_outcome_not_the_payload(
    client: AsyncClient,
) -> None:
    """THE ONE THAT MATTERS: a `charge.succeeded` event does not make a charge succeed.

    The processor declined this card. The event says otherwise. The payment settles
    as **failed** and no money moves, because the handler calls ``reconcile_payment``
    and the processor's books are the authority -- the event only decided *when* to
    look.

    The obvious implementation, reading ``type`` and writing the matching status,
    passes every deduplication test in this file and fails here. It would also mean
    anyone who can reach this endpoint can credit an account, that event ordering
    becomes a correctness problem rather than a latency one, and that a provider's
    retry of a stale event can resurrect a settled payment.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account, force_outcome="failure")

    response = await send_webhook(
        client, event_body(payment_id, event_id="evt_lying", event_type="charge.succeeded")
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == ReconcileOutcome.SETTLED_FAILED

    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.FAILED
    assert await count_rows("ledger_entries") == 0
    assert await count_rows("outbox_events") == 0
    assert await get_balance(client, account) == 0

    # The event that lied is still recorded verbatim -- what a third party told us is
    # worth keeping even when, especially when, it was wrong.
    recorded = await scalar("SELECT payload->>'type' FROM webhook_events")
    assert recorded == "charge.succeeded"


async def test_an_unknown_attempt_reference_is_not_recorded_and_asks_for_a_retry(
    client: AsyncClient,
) -> None:
    """A webhook can overtake our own commit, so 'unknown' must mean 'retry', not 'done'.

    The processor's books and ours are written on different connections at almost
    the same instant, with no ordering between them. An event for a payment that
    does not exist *yet* is a normal race, not a bogus event -- and recording it as
    handled would swallow a real notification for a payment about to appear.

    So the claim is rolled back with the request and the event id stays free. That
    is Phase 3's "a failed request leaves its key unconsumed", reused exactly, and
    the second half of this test is what proves the id really was freed.
    """
    account = await create_account(client, "Customer")
    stranger = str(uuid.uuid4())

    refused = await send_webhook(client, event_body(stranger, event_id="evt_early"))

    assert refused.status_code == 404, refused.text
    assert await count_rows("webhook_events") == 0

    # The payment now exists, and the provider's retry -- same event id -- lands.
    payment_id = await crash_a_charge(client, account)
    accepted = await send_webhook(client, event_body(payment_id, event_id="evt_early"))

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["duplicate"] is False
    assert await count_rows("webhook_events") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_an_unrecognised_event_type_is_rejected(client: AsyncClient) -> None:
    """A provider sending something new should be loud, not silently discarded.

    A 200 for an event this service does not understand tells the provider it was
    handled and stops the retries, which is the one answer guaranteed to lose it.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)

    response = await send_webhook(
        client, event_body(payment_id, event_id="evt_novel", event_type="charge.disputed")
    )

    assert response.status_code == 422, response.text
    assert await count_rows("webhook_events") == 0
    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.PROCESSING


async def test_a_blank_event_id_is_rejected(client: AsyncClient) -> None:
    """The dedupe key is the event id, so an absent one is not a small problem.

    An event with no id cannot be deduplicated at all, and accepting it would mean
    accepting it repeatedly. Rejecting at the schema keeps that impossible rather
    than merely unlikely.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)

    response = await send_webhook(client, event_body(payment_id, event_id=""))
    assert response.status_code == 422, response.text
    assert await count_rows("webhook_events") == 0


# --- What settling through a webhook does for the customer ------------------------


async def test_a_webhook_frees_the_abandoned_idempotency_key(client: AsyncClient) -> None:
    """The crash consumed the customer's key; the webhook gives it its answer back.

    Inherited from the sweep rather than reimplemented -- ``reconcile_payment``
    finalises the key alongside the payment, and the webhook path gets that for
    free by being the same function. The customer's retry replays the settled
    outcome instead of a 409, and does so seconds after the crash rather than after
    the sweep's threshold.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)

    blocked = await post_charge(client, account, AMOUNT, key=KEY)
    assert blocked.status_code == 409, blocked.text

    await send_webhook(client, event_body(payment_id, event_id="evt_unblock"))

    replayed = await post_charge(client, account, AMOUNT, key=KEY)
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["status"] == PaymentStatus.SUCCEEDED

    # Replayed, not re-charged.
    assert await count_rows("payments") == 1
    assert await count_rows("ledger_transactions") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_a_webhook_and_the_sweep_racing_settle_the_payment_once(
    client: AsyncClient,
) -> None:
    """Push and pull recovery running together, which is the normal configuration.

    Both paths are meant to be enabled at once, so they will regularly reach the
    same stranded payment. They settle it once for the same reason two sweeps do:
    ``FOR UPDATE SKIP LOCKED`` plus the ``status = 'processing'`` predicate inside
    the lock. Nothing about the webhook path needed to know the sweep exists.
    """
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)

    await asyncio.gather(
        send_webhook(client, event_body(payment_id, event_id="evt_racing")),
        sweep_once(
            async_session, FakeProcessor(books=processor_books), stuck_after_seconds=0
        ),
    )

    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.SUCCEEDED
    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("ledger_entries") == 2
    assert await count_rows("outbox_events") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_reading_a_recorded_webhook_back(client: AsyncClient) -> None:
    """``GET /webhooks/{id}`` exists so the smoke script can show one record, once."""
    account = await create_account(client, "Customer")
    payment_id = await crash_a_charge(client, account)
    await send_webhook(client, event_body(payment_id, event_id="evt_readable"))

    found = await client.get("/webhooks/evt_readable")
    assert found.status_code == 200, found.text
    assert found.json()["outcome"] == ReconcileOutcome.SETTLED_SUCCEEDED

    missing = await client.get("/webhooks/evt_never_sent")
    assert missing.status_code == 404
