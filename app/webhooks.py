"""Recording an inbound webhook, so that the next copy of it does nothing.

## The contract nobody gets to opt out of

Payment processors deliver webhooks **at least once**. That is not a caveat about
flaky networks, it is the strongest promise a sender can make: the provider posts
an event, does not receive a response, and cannot distinguish "the receiver never
saw it" from "the receiver handled it and the response was lost". The only safe
action from there is to send it again. Every provider does. A receiver written on
the assumption of single delivery is one that eventually settles a payment twice,
and it will do so under exactly the conditions -- a slow response, a deploy, a
timeout -- that also make it hardest to notice.

So the receiver is where exactly-once has to be manufactured, and it is
manufactured the same way it is everywhere else in this project: with a unique
index.

    INSERT INTO webhook_events (event_id, ...) VALUES (...)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id

A row comes back exactly when this delivery is the first one, and this request
therefore owns the work. No row means the event has already been handled and there
is nothing to do. Written as SELECT-then-INSERT it would be the same check-then-act
mistake Phase 4 is about, and two simultaneous deliveries of one event -- which
providers genuinely produce -- would both find nothing and both act.

## Why there is no 'in_progress' here

``idempotency_keys`` needs a two-state claim because Phase 5a had to commit a
charge's claim before calling the processor, leaving a window where a key is held
by work that has not finished. A webhook has no such window: the claim above and
the settlement it authorises are **one transaction**. If the settlement does not
commit, the claim does not either, so a redelivery correctly re-processes an event
whose handling was lost, rather than finding a tombstone for work that never
happened.

That is precisely the property Phase 3's idempotency claim had and Phase 5a had to
trade away. This table keeps it for one structural reason worth stating: handling a
webhook never calls out to a third party mid-transaction, so there is nothing here
that a single transaction cannot span.

## What the receiver trusts

Almost nothing. See ``app/routers/webhooks.py``: the payload's ``type`` is recorded
but is not what decides a payment's fate. The event is treated as a *hint that it
is time to reconcile*, and the processor's own books stay the authority.
"""

import json
import uuid
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Longest provider event id accepted. Arbitrary but stated, for the same reason
# MAX_KEY_LENGTH is: an unbounded primary key is an unbounded index.
MAX_EVENT_ID_LENGTH = 255

_CLAIM_SQL = text(
    """
    INSERT INTO webhook_events (event_id, event_type, payload, payment_id)
    VALUES (:event_id, :event_type, CAST(:payload AS jsonb), :payment_id)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
    """
)

_RECORD_OUTCOME_SQL = text(
    "UPDATE webhook_events SET outcome = :outcome WHERE event_id = :event_id"
)

_LOAD_SQL = text(
    """
    SELECT event_type, payment_id, outcome, received_at
    FROM webhook_events
    WHERE event_id = :event_id
    """
)


class RecordedWebhook(NamedTuple):
    """A webhook this service has already handled."""

    event_type: str
    payment_id: uuid.UUID | None
    outcome: str


async def claim_event(
    session: AsyncSession,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    payment_id: uuid.UUID | None,
) -> bool:
    """Try to become the delivery that handles ``event_id``. Does not commit.

    True means this request owns the work. False means the event has been handled
    already and this delivery must do nothing -- not "probably", not "unless the
    first one failed": if the first attempt's work had failed, its transaction
    would have rolled this row back with it and there would be nothing to conflict
    with.

    The whole payload is stored, not just the fields the handler reads. A webhook
    is evidence about what a third party told us, and the version of it that is
    worth keeping is the one they actually sent -- including the fields this
    service does not understand yet, which are the fields somebody will want during
    the incident that makes them matter.
    """
    row = (
        await session.execute(
            _CLAIM_SQL,
            {
                "event_id": event_id,
                "event_type": event_type,
                # Serialised here rather than passed as a dict, matching
                # app/idempotency.py and app/outbox.py: the bind is a text parameter
                # cast to jsonb, and the encoding decision stays in one place.
                "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
                "payment_id": payment_id,
            },
        )
    ).first()
    return row is not None


async def record_outcome(session: AsyncSession, event_id: str, outcome: str) -> None:
    """Write down what handling this event did. Does not commit.

    Runs in the same transaction as the handling itself, so the record and the
    effect become visible together. There is no path that commits one without the
    other, which is why the column can be NOT NULL with a default rather than a
    nullable field somebody has to remember to check.
    """
    await session.execute(_RECORD_OUTCOME_SQL, {"event_id": event_id, "outcome": outcome})


async def load_event(session: AsyncSession, event_id: str) -> RecordedWebhook | None:
    """Read back an event this service has already handled, if it has."""
    row = (await session.execute(_LOAD_SQL, {"event_id": event_id})).first()
    if row is None:
        return None
    return RecordedWebhook(
        event_type=row.event_type, payment_id=row.payment_id, outcome=row.outcome
    )
