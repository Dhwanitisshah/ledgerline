"""Phase 5b: the outbox event commits with the money, and is delivered once.

Two claims, and they are separate claims that fail in separate ways:

1. **The event exists if and only if the money moved.** Enforced by writing the
   outbox row in the same transaction as the postings, so there is no window
   between them in which anything can happen. Tests in the first section.
2. **The event has its effect exactly once, even though it may be delivered more
   than once.** Enforced by the consumer's primary key, not by the publisher being
   careful. Tests in the second section, including the one this phase exists for:
   killing the worker after it has delivered and before it can say so.

The second claim is the one worth reading carefully, because the tempting version
of it -- "the publisher delivers each event exactly once" -- is not true and cannot
be made true. See app/publisher.py.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from app.db import async_session
from app.deps import processor_books
from app.models import PaymentStatus
from app.outbox import PAYMENT_SUCCEEDED
from app.processor import FakeProcessor
from app.publisher import (
    DeliveryLog,
    PublishOutcome,
    SimulatedWorkerCrash,
    publish_once,
)
from app.reconcile import sweep_once
from app.routers.charges import SimulatedCrash
from tests.conftest import (
    count_rows,
    create_account,
    create_charge,
    get_balance,
    post_charge,
    scalar,
)

KEY = "phase5b-key"
AMOUNT = 250000


def sink(**kwargs: object) -> DeliveryLog:
    """The downstream consumer, on its own session factory.

    Its own, not the publisher's -- that independence is what lets a delivery
    survive the publisher's transaction rolling back, which is the whole mechanism
    under test in ``test_a_worker_killed_mid_publish_delivers_exactly_once``.
    """
    return DeliveryLog(async_session, **kwargs)


async def publish(**kwargs: object):
    return await publish_once(async_session, sink(), **kwargs)


async def sweep():
    """One reconciliation pass over everything currently stuck."""
    return await sweep_once(
        async_session, FakeProcessor(books=processor_books), stuck_after_seconds=0
    )


async def crash_a_charge(client: AsyncClient, account: str, **overrides: object) -> None:
    with pytest.raises(SimulatedCrash):
        await post_charge(
            client, account, AMOUNT, key=KEY, force_crash_after_processor=True, **overrides
        )


async def only_event() -> dict:
    """The single outbox event, as columns plus its payload."""
    async with async_session() as session:
        from sqlalchemy import text

        row = (
            await session.execute(
                text(
                    "SELECT id, event_type, payload, status, published_at, attempts "
                    "FROM outbox_events"
                )
            )
        ).one()
    return {
        "id": row.id,
        "event_type": row.event_type,
        "payload": row.payload,
        "status": row.status,
        "published_at": row.published_at,
        "attempts": row.attempts,
    }


# --- The event and the money are one commit --------------------------------------


async def test_a_successful_charge_writes_exactly_one_pending_event(
    client: AsyncClient,
) -> None:
    """The ordinary case: money moved, so an event says so, and nothing published it.

    Note what the request did *not* do. It did not call a broker, did not open a
    second connection, and did not do anything that could have failed independently
    of the charge. Publishing is somebody else's problem, later, and the event sits
    in 'pending' until that somebody runs.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT, key=KEY)

    assert await count_rows("outbox_events") == 1
    event = await only_event()

    assert event["event_type"] == PAYMENT_SUCCEEDED
    assert event["status"] == "pending"
    assert event["published_at"] is None
    assert event["attempts"] == 0

    # The payload describes the charge that was actually committed, and carries the
    # posting that justifies it -- a consumer can follow it back to the money.
    assert event["payload"]["payment_id"] == charge["id"]
    assert event["payload"]["account_id"] == account
    assert event["payload"]["amount"] == AMOUNT
    assert event["payload"]["status"] == PaymentStatus.SUCCEEDED
    assert event["payload"]["ledger_transaction_id"] == charge["ledger_transaction_id"]

    # Nothing has been delivered. The event is durable; the delivery is pending.
    assert await count_rows("event_deliveries") == 0


async def test_a_declined_charge_writes_no_event(client: AsyncClient) -> None:
    """No money moved, so nothing is announced.

    This is the half of the guarantee that a broker-after-commit implementation also
    gets right, and it is still worth pinning: the natural way to break it is to
    emit an event for every charge *attempt* and let consumers filter on status,
    which turns "the event means money moved" into "the event means someone tried",
    and every downstream consumer into a place the distinction can be forgotten.
    """
    account = await create_account(client, "Customer")
    charge = await create_charge(client, account, AMOUNT, key=KEY, force_outcome="failure")

    assert charge["status"] == PaymentStatus.FAILED
    assert await count_rows("ledger_entries") == 0
    assert await count_rows("outbox_events") == 0


async def test_a_crash_after_the_processor_writes_no_event(client: AsyncClient) -> None:
    """The Phase 5a crash produces no event, because it produces no posting.

    The card was charged, and there is still nothing to announce -- Ledgerline does
    not yet know the charge succeeded, and an event asserting that it did would be a
    guess sent to systems that will act on it. The sweep emits the event later, when
    the processor has actually confirmed it.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account)

    assert await count_rows("processor_charges") == 1
    assert await count_rows("payments") == 1
    assert await count_rows("outbox_events") == 0


async def test_the_event_and_the_posting_live_or_die_together(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomicity claim itself, rather than one of its consequences.

    The settlement is killed *after* the posting and the outbox row are both sitting
    in the transaction, so this is the moment where a dual write would have already
    told somebody. Both die. Not one, not the event without the money, not the money
    without the event -- there is a single commit and it did not happen.

    Then the sweep settles the payment, and now there is exactly one posting and
    exactly one event, written by the same commit that finally moved the money.
    """

    class SettlementDied(RuntimeError):
        pass

    async def die(*args: object, **kwargs: object):
        raise SettlementDied("the settlement transaction died before COMMIT")

    monkeypatch.setattr("app.routers.charges._complete", die)

    account = await create_account(client, "Customer")
    with pytest.raises(SettlementDied):
        await post_charge(client, account, AMOUNT, key=KEY)

    # Transaction A committed, so the attempt survives -- that is Phase 5a.
    status = await scalar("SELECT status::text FROM payments")
    assert status == PaymentStatus.PROCESSING

    # Transaction B did not. Neither the money nor the announcement of it exists.
    assert await count_rows("ledger_transactions") == 0
    assert await count_rows("outbox_events") == 0
    assert await get_balance(client, account) == 0

    report = await sweep()
    assert report.settled == 1

    assert await count_rows("ledger_transactions") == 1
    assert await count_rows("outbox_events") == 1
    assert await get_balance(client, account) == AMOUNT


async def test_the_sweep_emits_the_same_event_the_charge_route_would_have(
    client: AsyncClient,
) -> None:
    """A consumer cannot tell that this charge settled through the recovery path.

    Deliberately indistinguishable. A charge that succeeded four minutes late
    because its request died is still a charge that succeeded, and making the event
    type or shape depend on which code path noticed would push a detail of
    Ledgerline's internal recovery into every downstream system's branching.
    """
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account)
    payment_id = str(await scalar("SELECT id FROM payments"))

    await sweep()

    assert await count_rows("outbox_events") == 1
    event = await only_event()
    assert event["event_type"] == PAYMENT_SUCCEEDED
    assert event["payload"]["payment_id"] == payment_id
    assert event["payload"]["amount"] == AMOUNT
    assert event["payload"]["ledger_transaction_id"] is not None


async def test_the_sweep_settling_a_decline_emits_nothing(client: AsyncClient) -> None:
    """Reconciled to 'failed' means no money moved, then or now, so nothing is owed."""
    account = await create_account(client, "Customer")
    await crash_a_charge(client, account, force_outcome="failure")

    await sweep()

    assert await scalar("SELECT status::text FROM payments") == PaymentStatus.FAILED
    assert await count_rows("outbox_events") == 0


# --- At-least-once delivery, exactly-once effect ----------------------------------


async def test_the_publisher_delivers_a_pending_event_and_marks_it(
    client: AsyncClient,
) -> None:
    """The happy path, so the interesting failures below have a baseline."""
    account = await create_account(client, "Customer")
    await create_charge(client, account, AMOUNT, key=KEY)

    report = await publish()

    assert report.outcomes == {PublishOutcome.DELIVERED: 1}
    assert await count_rows("event_deliveries") == 1

    event = await only_event()
    assert event["status"] == "published"
    assert event["published_at"] is not None
    assert event["attempts"] == 1

    # The consumer received the event under the same id the outbox holds. That id
    # is the entire basis of its ability to deduplicate, so it is asserted rather
    # than assumed.
    delivered_id = await scalar("SELECT event_id FROM event_deliveries")
    assert delivered_id == event["id"]


async def test_a_worker_killed_mid_publish_delivers_exactly_once(
    client: AsyncClient,
) -> None:
    """THE PHASE 5b PROPERTY: at-least-once delivery, exactly-once effect.

    The worker delivers the event, and dies before it can record that it did. This
    is not a contrived instant -- it is the *only* possible instant, because a
    delivery and the local mark of it are two writes to two systems and no arrangement
    makes them atomic. Every message pipeline in existence has this window.

    So the event is still 'pending' and gets delivered a second time. The claim under
    test is not that this does not happen; it is that when it happens, **nothing
    happens twice**: the consumer's primary key rejects the duplicate, and the
    publisher can see that it did.

    Note where the correctness lives. Not in the worker, which behaved identically
    on both passes and knew nothing. In the receiving database's unique index.
    """
    account = await create_account(client, "Customer")
    await create_charge(client, account, AMOUNT, key=KEY)

    # --- the worker dies mid-publish ---
    with pytest.raises(SimulatedWorkerCrash):
        await publish_once(async_session, sink(crash_after_delivery=True))

    # The consumer has it. We have no idea that the consumer has it.
    assert await count_rows("event_deliveries") == 1
    event = await only_event()
    assert event["status"] == "pending", (
        "the mark was in the transaction the crash rolled back; if this says "
        "'published' then the delivery and the mark are no longer ordered the way "
        "app/publisher.py claims"
    )
    # The attempt increment went down with the same transaction. Honest, and
    # documented as the cost of the ordering that makes redelivery correct.
    assert event["attempts"] == 0

    # --- restart, with a worker that does not die ---
    report = await publish()

    # Delivered a second time, and correctly ignored the second time.
    assert report.outcomes == {PublishOutcome.DUPLICATE_SUPPRESSED: 1}
    assert report.published == 1

    # One effect. Two deliveries, one row.
    assert await count_rows("event_deliveries") == 1

    settled = await only_event()
    assert settled["status"] == "published"
    assert settled["attempts"] == 1


async def test_publishing_a_second_time_finds_nothing_to_do(client: AsyncClient) -> None:
    """A published event is not a candidate, so a second pass is a no-op.

    The publisher runs on a timer and will therefore run when there is nothing to
    do far more often than not. That case must cost one indexed query against the
    partial index, not a scan of every event ever emitted.
    """
    account = await create_account(client, "Customer")
    await create_charge(client, account, AMOUNT, key=KEY)

    first = await publish()
    second = await publish()

    assert first.published == 1
    assert second.examined == 0
    assert await count_rows("event_deliveries") == 1


async def test_two_publishers_running_at_once_deliver_each_event_once(
    client: AsyncClient,
) -> None:
    """Two workers, one backlog. SKIP LOCKED divides it rather than duplicating it.

    A second worker is the normal way to drain a backlog faster, and it must not be
    the way to send everything twice. The claim is a locked statement, so an event
    another worker holds is skipped, not waited for and not re-delivered.
    """
    account = await create_account(client, "Customer")
    for index in range(6):
        await create_charge(client, account, AMOUNT, key=f"{KEY}-{index}")

    assert await count_rows("outbox_events") == 6

    left, right = await asyncio.gather(publish(), publish())

    assert left.published + right.published == 6
    assert left.outcomes.get(PublishOutcome.DUPLICATE_SUPPRESSED, 0) == 0
    assert right.outcomes.get(PublishOutcome.DUPLICATE_SUPPRESSED, 0) == 0

    assert await count_rows("event_deliveries") == 6
    assert await scalar("SELECT count(*) FROM outbox_events WHERE status = 'pending'") == 0


async def test_a_sink_that_refuses_leaves_the_event_pending_and_counts_the_attempt(
    client: AsyncClient,
) -> None:
    """A failed delivery is a delay, not a loss -- which is the point of the outbox.

    Before the outbox, a broker that was down at the wrong moment meant an event
    that no longer existed anywhere. Now the event is a durable row, so "the
    consumer is down" degrades to "the consumer is behind", and the only thing that
    accumulates is a backlog.
    """

    class RefusingSink:
        async def deliver(self, event: object) -> bool:
            raise RuntimeError("the consumer is down")

    account = await create_account(client, "Customer")
    await create_charge(client, account, AMOUNT, key=KEY)

    report = await publish_once(async_session, RefusingSink())

    assert report.outcomes == {PublishOutcome.FAILED: 1}
    assert report.published == 0

    event = await only_event()
    assert event["status"] == "pending"
    # Recorded on its own transaction, because the delivery's transaction was rolled
    # back and the record of having tried must not go with it.
    assert event["attempts"] == 1
    assert await count_rows("event_deliveries") == 0

    # And when the consumer comes back, the event is still there waiting.
    recovered = await publish()
    assert recovered.outcomes == {PublishOutcome.DELIVERED: 1}
    assert (await only_event())["attempts"] == 2


async def test_one_undeliverable_event_does_not_block_the_ones_behind_it(
    client: AsyncClient,
) -> None:
    """A poison event costs itself a pass, not the whole backlog.

    Worth a test because the obvious loop has a live-lock in it. A failed delivery
    rolls its transaction back, which makes the event immediately claimable again --
    so a pass that simply re-claims the oldest pending event will pick the same
    undeliverable one every time, spin until the batch limit, and never reach
    anything behind it. The fix is that a pass remembers what it has already failed
    (``claim_next_pending(skip=...)``); the next pass forgets, which is correct,
    because the reason to skip expires with the attempt that produced it.
    """
    account = await create_account(client, "Customer")
    for index in range(3):
        await create_charge(client, account, AMOUNT, key=f"{KEY}-{index}")

    first_id = await scalar("SELECT id FROM outbox_events ORDER BY created_at LIMIT 1")

    class PoisonSink(DeliveryLog):
        async def deliver(self, event: object) -> bool:
            if event.id == first_id:
                raise RuntimeError("this one always fails")
            return await super().deliver(event)

    report = await publish_once(async_session, PoisonSink(async_session))

    assert report.outcomes == {
        PublishOutcome.FAILED: 1,
        PublishOutcome.DELIVERED: 2,
    }
    # The other two got through rather than queueing behind the poison event.
    assert await count_rows("event_deliveries") == 2
    assert await scalar("SELECT count(*) FROM outbox_events WHERE status = 'pending'") == 1


async def test_a_batch_bounds_the_pass_rather_than_the_backlog(
    client: AsyncClient,
) -> None:
    """A pass publishes at most ``batch_size`` and leaves the rest for the next one.

    Bounded so that a pass terminates under sustained load. An unbounded drain would
    make ``--once`` a lie and would give an operator no way to stop the worker at a
    predictable point.
    """
    account = await create_account(client, "Customer")
    for index in range(5):
        await create_charge(client, account, AMOUNT, key=f"{KEY}-{index}")

    first = await publish(batch_size=2)
    assert first.published == 2
    assert await scalar("SELECT count(*) FROM outbox_events WHERE status = 'pending'") == 3

    second = await publish(batch_size=10)
    assert second.published == 3
    assert await count_rows("event_deliveries") == 5


async def test_events_are_published_oldest_first(client: AsyncClient) -> None:
    """FIFO by ``created_at``, so a backlog drains in the order it accumulated.

    Not a correctness property -- consumers are required to tolerate any order,
    because SKIP LOCKED across several workers gives no global ordering anyway. It
    is a fairness property: the event that has been waiting longest is the one whose
    consumer has been wrong for longest.
    """
    account = await create_account(client, "Customer")
    charges = [
        await create_charge(client, account, AMOUNT + index, key=f"{KEY}-{index}")
        for index in range(3)
    ]

    await publish(batch_size=1)

    delivered = await scalar("SELECT payload->>'payment_id' FROM event_deliveries")
    assert delivered == charges[0]["id"]


async def test_an_event_delivered_by_hand_is_not_delivered_again(
    client: AsyncClient,
) -> None:
    """The consumer's dedupe, asserted directly rather than through a crash.

    The crash test above proves the effect happens once; this proves *why*, without
    a worker in the way. Two deliveries of one event, one row, and the sink says so.
    """
    account = await create_account(client, "Customer")
    await create_charge(client, account, AMOUNT, key=KEY)

    async with async_session() as session:
        from app.outbox import claim_next_pending

        event = await claim_next_pending(session)
        assert event is not None

    consumer = sink()
    assert await consumer.deliver(event) is True
    assert await consumer.deliver(event) is False
    assert await consumer.count() == 1

    # A different event id is a different event, however similar it looks.
    from dataclasses import replace

    assert await consumer.deliver(replace(event, id=uuid.uuid4())) is True
    assert await consumer.count() == 2
