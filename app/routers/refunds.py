"""``POST /charges/{id}/refund`` -- money going back, exactly once.

Phase 6 is the phase where every earlier phase gets used at once, and almost
nothing here is new machinery. The refund flow is:

* Phase 1's ledger invariants, via ``write_posting`` -- a refund is an ordinary
  balanced two-legged posting with the charge's legs the other way round;
* Phase 2's state machine, via ``transition`` -- ``succeeded -> refunded``, but
  only when the refunds total the whole charge;
* Phase 3's idempotency, via ``claim_key`` / ``finalize_key`` -- unchanged, with a
  different fingerprint;
* Phase 4's row lock, via ``lock_payment_for_refund`` -- because an over-refund is
  the overdraw race;
* Phase 5b's outbox, via ``record_payment_refunded`` -- in the settlement
  transaction, beside the posting.

If any of those had been built as a special case for charges rather than as a
mechanism, this file is where that would have shown up. It did not, which is the
most useful thing Phase 6 says about Phases 1-5.

## Reversing, not editing

A refund does not touch the charge's posting. It cannot: the ledger tables reject
UPDATE and DELETE outright (migration 0002), and that is the correct constraint
rather than an obstacle to work around. A refund writes a **new** posting with the
legs reversed::

    charge:  DEBIT  house:card_settlement:INR   250000
             CREDIT customer                    250000

    refund:  DEBIT  customer                    100000
             CREDIT house:card_settlement:INR   100000

The customer's derived balance falls by the refunded amount, the house account's
rises by the same, the ledger still sums to zero, and the audit trail says what
actually happened in the order it happened. Corrections are made by posting, not by
rewriting -- which is what the Phase 1 README promised, delivered five phases later
without amending the promise.

A full refund therefore returns the customer's balance to exactly what it was
before the charge, by arithmetic rather than by assertion.

## What is NOT guarded, deliberately

**A refund is not subject to the balance floor.** A withdrawal refuses to take an
account below zero (Phase 4); a refund will happily do it, if the customer has
already withdrawn the money that is now going back to their card.

That asymmetry is not an oversight and it is worth stating loudly, because it looks
like one. A withdrawal is a request Ledgerline may decline. A refund is a
*reversal of something that already happened*, and the money is going back to the
card whether or not the customer's ledger balance can afford it. Refusing would not
keep the money; it would only mean the processor reversed a charge that Ledgerline
still shows as fully live. A negative balance here is an accurate statement that
the customer owes money, which is a collections problem and not a bookkeeping one.

## One transaction, and why that is not a Phase 5a regression

The processor call happens **inside** the transaction, which is the arrangement
Phase 5a took apart for charges. That is deliberate, and the reason is in
``app/refunds.py``: a refund's processor-side attempt reference is *derived* from
the payment id and the caller's ``Idempotency-Key`` rather than generated, so it
can be recomputed by a retry after a crash.

A charge could not do that -- its reference existed only in a Python variable until
the payment committed, so a crash made the attempt unaskable-about, and the fix was
to commit an intent first. A refund's reference survives without being stored. If
this transaction dies after the processor reversed the money, the client's retry
with the same key computes the same reference, the processor recognises it and
replays the original reversal instead of sending the money twice, and Ledgerline
records it then.

The honest costs, both real:

* the processor call holds a transaction and the payment's row lock open, which is
  the ``idle in transaction`` cost Phase 5a measured and removed from the charge
  path. Refunds are far lower volume and the lock is per payment, so it is a much
  smaller version of the same problem -- but it is the same problem, not a
  different one;
* between the processor reversing and this transaction committing, the two sides
  disagree. Nothing here closes that window; ``app/drift.py`` is what *notices*
  it, which is the Phase 6 answer and is detection rather than prevention.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_processor, get_session, processor_books
from app.idempotency import (
    MAX_KEY_LENGTH,
    claim_key,
    finalize_key,
    load_claim,
    refund_fingerprint,
)
from app.ledger import LedgerInvariantError, settlement_account_id, write_posting
from app.locking import lock_payment_for_refund, try_lock_idempotency_key
from app.models import Payment, PaymentStatus, Refund
from app.outbox import record_payment_refunded
from app.payments import transition
from app.processor import FakeProcessor, ProcessorAdapter
from app.refunds import (
    RefundAssessment,
    RefundNotAllowedError,
    assess_refund,
    refund_attempt_ref,
)
from app.schemas import RefundCreate, RefundOut

# Same prefix as app/routers/charges.py, mounted as a second router. A refund is an
# operation *on a charge* and its URL says so; keeping it in its own module keeps
# the charge flow's already long file about charging.
router = APIRouter(prefix="/charges", tags=["refunds"])


def _out(payment: Payment, refund: Refund, total_refunded: int) -> RefundOut:
    """Build the response, with the derived figures the caller will want next."""
    return RefundOut(
        id=refund.id,
        payment_id=refund.payment_id,
        amount=refund.amount,
        currency=refund.currency,
        status=refund.status,
        processor_ref=refund.processor_ref,
        failure_reason=refund.failure_reason,
        ledger_transaction_id=refund.ledger_transaction_id,
        created_at=refund.created_at,
        total_refunded=total_refunded,
        remaining_refundable=payment.amount - total_refunded,
        payment_status=PaymentStatus(payment.status),
    )


async def _write_reversing_posting(
    session: AsyncSession, payment: Payment, assessment: RefundAssessment
) -> uuid.UUID:
    """Post the mirror of the charge, and let Phase 1 prove it balances.

    The legs are the charge's, swapped: DEBIT the customer, CREDIT the house
    settlement account. ``write_posting`` neither knows nor cares that this is a
    refund -- it takes two accounts and an amount and enforces the same
    single-currency and sum-to-zero invariants it has enforced since Phase 1. That
    is the whole reason it takes accounts rather than a payment.
    """
    house_id = await settlement_account_id(session, payment.currency)
    ledger_transaction = await write_posting(
        session,
        description=f"refund of charge {payment.id}",
        debit_account_id=payment.account_id,
        credit_account_id=house_id,
        amount=assessment.amount,
    )
    return ledger_transaction.id


@router.post(
    "/{payment_id}/refund",
    response_model=RefundOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Idempotency-Key header missing, blank, or too long"},
        404: {"description": "Unknown payment"},
        409: {
            "description": (
                "The payment is not refundable (failed, still processing, or "
                "already fully refunded), or a refund for this key is in progress"
            )
        },
        422: {
            "description": (
                "The amount exceeds what is still refundable, or the "
                "Idempotency-Key was reused with a different payload"
            )
        },
    },
)
async def create_refund(
    payment_id: uuid.UUID,
    payload: RefundCreate | None = None,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "REQUIRED. Identifies this refund attempt so a retry replays the "
            "original response instead of sending the money back twice. Reuse the "
            "same key for every retry of one refund; use a new key for a genuinely "
            "second refund of the same charge. Omitting it is a 400."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    processor: ProcessorAdapter = Depends(get_processor),
) -> JSONResponse | RefundOut:
    """Return some or all of a charge, and record honestly what happened.

    ``{"amount": N}`` refunds N minor units; an omitted or empty body refunds
    **whatever is still refundable**, which for an untouched payment is the whole
    charge. Returns **201 on both outcomes**, including a refund the processor
    declined, for the same reason ``POST /charges`` does: the refund resource was
    created either way and a decline is a business result, not a failed request.
    Read ``status`` to find out which happened; a 4xx means the *request* was wrong.

    Idempotency is mandatory and behaves exactly as it does for a charge:

    * same key, same body -> the stored response, replayed byte for byte. No second
      refund, and the processor is not called again.
    * same key, different amount -> 422, and nothing is written.
    * no key -> 400.
    * same key while the first attempt is still running -> 409.
    """
    body = payload or RefundCreate()

    key = (idempotency_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing required header: Idempotency-Key",
        )
    if len(key) > MAX_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters",
        )

    request_hash = refund_fingerprint(payment_id=payment_id, amount=body.amount)

    # --- The claim. Phase 3's machinery, unchanged. ------------------------------
    # Gated on the same non-blocking advisory lock as a charge, covering the window
    # in which this request's claim exists but is not yet visible to anyone else.
    if not await try_lock_idempotency_key(session, key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a refund for idempotency key {key} is already in progress; "
                "retry shortly to receive its recorded result"
            ),
        )

    if not await claim_key(session, key, request_hash):
        existing = await load_claim(session, key)
        if existing is None:  # pragma: no cover - the claim just said it exists
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"idempotency key {key} vanished between claim and read",
            )
        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "idempotency key reused with a different payload: this key is "
                    "already bound to another refund. Use a new key for a new "
                    "refund, or resend the original body to replay its result."
                ),
            )
        if not existing.is_completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a refund for idempotency key {key} is still in progress",
            )
        return JSONResponse(
            content=existing.response_snapshot, status_code=existing.response_status
        )

    # --- The payment, under a lock -----------------------------------------------
    # The lock comes BEFORE the refunded total is read. That ordering is the entire
    # concurrency argument (see app/locking.py); a lock taken after the read guards
    # a value already copied into Python.
    if not await lock_payment_for_refund(session, payment_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"payment {payment_id} not found",
        )

    payment = await session.get(Payment, payment_id)
    if payment is None:  # pragma: no cover - the lock just returned this row
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"payment {payment_id} not found",
        )

    try:
        assessment = await assess_refund(session, payment, body.amount)
    except RefundNotAllowedError as exc:
        # Nothing has been written that matters, and the claim rolls back with the
        # request -- so a caller who asked for too much can fix the amount and
        # retry with the same key. Phase 3's "a failed request leaves its key
        # unconsumed", reused a third time.
        await session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Per-request forcing of the fake processor, exactly as the charge route does
    # it, and the first thing to delete when a real adapter lands.
    if body.force_outcome is not None:
        processor = FakeProcessor(
            outcome=body.force_outcome,
            latency_ms=settings.PROCESSOR_LATENCY_MS,
            books=processor_books,
        )

    # --- The reversal ------------------------------------------------------------
    # The attempt reference is DERIVED, not generated: same payment plus same
    # idempotency key gives the same uuid in any process, at any time. That is what
    # makes a retry after a crash a replay on the processor's side rather than a
    # second reversal, and it is why this flow does not need Phase 5a's two-
    # transaction split. See app/refunds.py.
    attempt_ref = refund_attempt_ref(payment_id, key)
    result = await processor.refund(
        attempt_ref=attempt_ref,
        charge_ref=payment.id,
        amount=assessment.amount,
        currency=payment.currency,
    )

    refund = Refund(
        payment_id=payment.id,
        amount=assessment.amount,
        currency=payment.currency,
        status="succeeded" if result.succeeded else "failed",
        processor_ref=result.processor_ref,
        failure_reason=result.failure_reason,
    )

    if not result.succeeded:
        # Declined. Record the attempt and move no money: no posting, no transition,
        # no outbox event, and the refunded total is unchanged. The row exists so
        # that "we tried and were refused" is answerable later, which is exactly
        # what a customer disputing a refund will ask.
        session.add(refund)
        await session.flush()
        await session.refresh(refund)
        return await _finalize(
            session, key, _out(payment, refund, assessment.already_refunded)
        )

    # Approved. Now, and only now, money moves.
    try:
        refund.ledger_transaction_id = await _write_reversing_posting(
            session, payment, assessment
        )
    except LedgerInvariantError as exc:  # pragma: no cover - defensive
        # Ledgerline built a bad posting. Abandon the whole refund rather than
        # commit a reversal that does not balance -- the same call the charge route
        # makes, for the same reason. Note that the processor has already reversed
        # the money at this point, so this leaves a real discrepancy: that is what
        # app/drift.py is for, and pretending otherwise by committing a broken
        # posting would be strictly worse.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"refund aborted, no ledger rows written: {exc}",
        ) from exc

    session.add(refund)

    # The transition, and the partial-versus-full decision in one line. A refund
    # that returns the last of the charge moves the payment to 'refunded'; anything
    # less leaves it 'succeeded', because the payment is still partly live. Note
    # that migration 0007's trigger has already run by the time this flush returns
    # -- if the invariant were breached, it would have raised rather than let this
    # get as far as a status change.
    await session.flush()
    if assessment.is_final:
        transition(payment, PaymentStatus.REFUNDED)

    await session.flush()
    await session.refresh(refund)
    await session.refresh(payment)

    # The event, beside the posting, in this transaction. Phase 5b's rule holds for
    # refunds unchanged: the announcement exists if and only if the money moved.
    await record_payment_refunded(
        session, payment, refund, total_refunded=assessment.total_after
    )

    return await _finalize(session, key, _out(payment, refund, assessment.total_after))


async def _finalize(session: AsyncSession, key: str, out: RefundOut) -> JSONResponse:
    """Record the response this key will replay, and commit everything at once.

    One commit covering the refunds row, the reversing posting, the payment's
    status, the outbox event and the idempotency key. They cannot disagree because
    they land together -- the same single-commit discipline every settlement in this
    project has had since Phase 2.

    The response is served from what ``finalize_key`` read back out of the database
    rather than from the dict that went in, so the first response and every replay
    are serialised from byte-identical input. JSONB normalises key order on write;
    without this the original and its replays would differ in exactly the way
    nobody notices until a client diffs them.
    """
    stored = await finalize_key(
        session, key, out.model_dump(mode="json"), status.HTTP_201_CREATED
    )
    await session.commit()
    return JSONResponse(content=stored.body, status_code=stored.status_code)


@router.get("/{payment_id}/refunds", response_model=list[RefundOut])
async def list_refunds(
    payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[RefundOut]:
    """Every refund attempt against one charge, oldest first.

    Includes declined attempts. A list that quietly dropped them would answer "what
    was refunded?" while appearing to answer "what was attempted?", and the second
    question is the one asked during a dispute.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"payment {payment_id} not found"
        )

    rows = await session.execute(
        select(Refund).where(Refund.payment_id == payment_id).order_by(Refund.created_at)
    )
    refunds = list(rows.scalars())

    # Computed once over the rows already in hand rather than with another SUM: the
    # answer is the same and this way there is no window in which the list and the
    # total could describe different moments.
    total = sum(refund.amount for refund in refunds if refund.status == "succeeded")
    return [_out(payment, refund, total) for refund in refunds]
