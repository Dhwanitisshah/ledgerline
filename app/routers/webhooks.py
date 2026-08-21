"""``POST /webhooks`` -- processor callbacks, ingested exactly once.

Phase 5a gave Ledgerline a way to *pull* the truth about a payment it lost track
of: the sweep in ``app/reconcile.py`` finds payments stranded in ``processing`` and
asks the processor what became of them. This endpoint is the same recovery,
**pushed**: the processor tells us the moment it knows, instead of us discovering
it up to ``RECONCILE_STUCK_AFTER_SECONDS`` plus one sweep interval later.

Both paths run the identical code. That is not code reuse for its own sake -- it is
the reason the endpoint is safe:

    **The webhook decides *when* to settle. The processor decides *what* the
    settlement is.**

The handler does not read ``type`` and write the corresponding status. It calls
``reconcile_payment``, which locks the payment and asks the processor's books. So a
webhook that is replayed, reordered, delayed by an hour, or outright forged cannot
move money the processor does not have a record of. The worst a bogus event can do
is cause a lookup.

That is worth dwelling on because the obvious implementation -- read
``type == "charge.succeeded"``, mark the payment succeeded, write the posting -- is
one where the payload *is* the authority. Then anyone who can reach this endpoint
can credit an account, event ordering becomes a correctness problem rather than a
latency one, and a provider's retry of a stale event can resurrect a settled
payment. None of those are hypothetical failure modes; they are the standard ones.

## Two independent layers of idempotency

1. **The event id.** ``app/webhooks.py`` claims it with
   ``INSERT ... ON CONFLICT DO NOTHING``, in the same transaction as the
   settlement. A redelivery finds the row and returns without doing anything.
2. **The settlement itself.** Even with the first layer removed entirely,
   ``reconcile_payment`` takes ``SELECT ... FOR UPDATE SKIP LOCKED`` with a
   ``status = 'processing'`` predicate inside it, and the state machine refuses a
   second move out of a terminal state. A second settlement of one payment is not
   prevented by a check; it is unrepresentable.

Two layers because they fail differently. The first is defeated by a provider that
generates a fresh event id for a retry (some do, on some event classes); the
second is defeated by nothing, but only covers effects on *this* payment. Together
they cover both a duplicate event and a duplicate effect.

## Status codes

Everything a provider could reasonably retry gets a non-2xx; everything else gets
a 200, including a duplicate. Providers treat a 2xx as "handled, stop sending" and
anything else as "try again later", so the status code is not decoration here --
it is the flow control of somebody else's retry loop.

| Situation                            | Response | Why                            |
| ------------------------------------ | -------- | ------------------------------ |
| First delivery, payment settled      | 200      | Handled. Stop sending.         |
| Duplicate delivery                   | 200      | Already handled. Stop sending. |
| Payment already settled by the sweep | 200      | Nothing to do, and that is fine. |
| Unknown attempt reference            | 404      | **Please retry** -- see below. |
| Malformed body                       | 422      | Retrying will not help, but it is honest |

The 404 is the interesting one and it is deliberate rather than a default. A
webhook can legitimately arrive *before* the charge request's transaction A has
committed -- the processor's books and ours are written on different connections at
almost the same instant, and there is no ordering guarantee between them.
Recording such an event as handled would swallow the notification for a payment
that is about to exist. So the claim is rolled back with the request, the event id
is left free, and the provider's next delivery finds the payment and settles it.
Phase 3's "a failed request leaves its key unconsumed", reused exactly.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_processor, get_session
from app.models import Payment
from app.processor import ProcessorAdapter
from app.reconcile import reconcile_payment
from app.schemas import WebhookIn, WebhookOut
from app.webhooks import claim_event, load_event, record_outcome

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "",
    response_model=WebhookOut,
    responses={
        404: {"description": "Unknown attempt reference; the provider should retry"},
    },
)
async def receive_webhook(
    payload: WebhookIn,
    session: AsyncSession = Depends(get_session),
    processor: ProcessorAdapter = Depends(get_processor),
) -> WebhookOut:
    """Ingest one processor callback, at most once in effect.

    Returns 200 for both a first delivery and a duplicate, with ``duplicate``
    saying which -- the provider does not care, but an operator reading logs after
    an incident very much does, and it is the field the tests and the smoke script
    assert on to show that the second delivery did nothing.
    """
    attempt_ref = payload.data.attempt_ref

    # Layer one: claim the event id. Everything below happens only for the delivery
    # that wins, and rolls back with it if it fails.
    claimed = await claim_event(
        session,
        event_id=payload.id,
        event_type=payload.type,
        payload=payload.model_dump(mode="json"),
        payment_id=attempt_ref,
    )

    if not claimed:
        existing = await load_event(session, payload.id)
        if existing is None:  # pragma: no cover - the claim just said it exists
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"webhook {payload.id} vanished between claim and read",
            )
        # No commit: this request wrote nothing. The response quotes what the
        # original delivery did, so a provider replaying an event gets a consistent
        # answer rather than a different-looking one each time.
        return WebhookOut(
            event_id=payload.id, duplicate=True, outcome=existing.outcome
        )

    # An event about an attempt we have never heard of. Roll the claim back so the
    # id stays free, and ask for a retry -- this is far more likely to be a webhook
    # that overtook our own commit than a bogus one, and swallowing it would lose a
    # real notification.
    if await session.get(Payment, attempt_ref) is None:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no payment for attempt reference {attempt_ref}; this event has not "
                "been recorded and should be retried"
            ),
        )

    # Layer two, and the authority. Not `if payload.type == ...` -- the processor's
    # books decide, exactly as they do for the sweep. See the module docstring.
    outcome = await reconcile_payment(session, processor, attempt_ref)
    await record_outcome(session, payload.id, outcome.value)

    # One commit covering the event record, the payment's new status, the posting
    # that justifies it, the outbox event announcing it, and the idempotency key
    # that now owes a response to the customer's retry. They cannot disagree
    # because they land together.
    await session.commit()

    return WebhookOut(event_id=payload.id, duplicate=False, outcome=outcome.value)


@router.get("/{event_id}", response_model=WebhookOut)
async def get_webhook(
    event_id: str, session: AsyncSession = Depends(get_session)
) -> WebhookOut:
    """Read back what handling one event did.

    Exists so the smoke script can show that two deliveries of one event left one
    record with one outcome, without needing a psql session to prove it.
    """
    existing = await load_event(session, event_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"webhook {event_id} not found"
        )
    return WebhookOut(event_id=event_id, duplicate=False, outcome=existing.outcome)


def attempt_reference(payment_id: uuid.UUID) -> str:
    """The string a processor would put in ``data.attempt_ref`` for this payment.

    One line, but it names the coupling rather than leaving it implicit: the
    processor-side idempotency key Ledgerline sends with a charge *is* the payment
    id (see app/routers/charges.py), so that is what comes back on the callback.
    """
    return str(payment_id)
