"""The naive charge flow: a payment lifecycle wrapped around a processor call.

"Naive" is meant precisely. This route has no idempotency key (Phase 3), takes no
lock (Phase 4), and emits no event (Phase 5). What it does have is the property
those phases build on top of, and which is worth nothing if it is not established
first:

    **A charge that fails moves no money. Not less money -- none.**

## How the transaction is arranged

Everything below happens inside one database transaction, opened implicitly by
the first statement and ended by exactly one ``commit()`` on each path::

    INSERT payment (created)
    UPDATE payment -> processing
    ── call the processor ───────────────────────  (no database work in flight)
    success:  INSERT ledger_transaction + 2 entries, UPDATE payment -> succeeded
    failure:  UPDATE payment -> failed
    COMMIT

## What rolls back, and what does not

| Outcome                          | payments row          | ledger rows        |
| -------------------------------- | --------------------- | ------------------ |
| processor succeeded              | committed `succeeded` | committed, balanced|
| processor declined               | committed `failed`    | **never written**  |
| ledger invariant violated (bug)  | rolled back, no row   | rolled back        |
| illegal transition (bug)         | rolled back, no row   | rolled back        |

The second row is the headline, and note *how* it is achieved: not by writing
ledger entries and then deleting them, and not by a compensating posting, but by
never writing them at all until the processor has already said yes. A partial
posting is not cleaned up here -- it is unrepresentable. The failure path commits
one UPDATE to one row in the ``payments`` table and touches nothing else.

The last two rows are the deliberate asymmetry. If Ledgerline builds a posting
that does not balance, or asks for a transition the state machine forbids, that is
a bug in Ledgerline, and the whole transaction is abandoned -- including the
payment row. A payment record that cannot be justified by balanced postings is
worse than no payment record, because it is a number someone will later trust.

## The gap this leaves, stated plainly

If the process dies between the processor returning success and ``COMMIT``, the
card was charged and Ledgerline has no record of it: the payment row was never
committed, so nothing even knows to go looking. The naive flow cannot close that
window -- a single database transaction cannot span a third party's system. Phase 5
(outbox + reconciliation) is the answer, and this docstring is the reason it is on
the roadmap.

Related: the database transaction is held open across the processor call, so a
slow processor pins a Postgres connection inside a write transaction. Set
``force_latency_ms`` and watch it happen. That is Phase 4's problem statement.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_processor, get_session
from app.ledger import LedgerInvariantError, settlement_account_id, write_charge_posting
from app.models import Account, Payment, PaymentStatus
from app.payments import transition
from app.processor import FakeProcessor, ProcessorAdapter
from app.schemas import ChargeCreate, ChargeOut

router = APIRouter(prefix="/charges", tags=["charges"])


@router.post("", response_model=ChargeOut, status_code=status.HTTP_201_CREATED)
async def create_charge(
    payload: ChargeCreate,
    session: AsyncSession = Depends(get_session),
    processor: ProcessorAdapter = Depends(get_processor),
) -> Payment:
    """Attempt a charge, and record honestly what happened.

    Returns **201 on both outcomes**, including a declined charge. The payment
    resource was created either way, and a decline is a business result rather
    than a failed request: the caller asked Ledgerline to attempt a charge and
    Ledgerline did, then recorded the answer. Signalling that with a 4xx would
    conflate "your request was wrong" with "the card said no", and would make
    Phase 3's replay-the-recorded-outcome behaviour awkward for no benefit. Read
    ``status`` to find out which happened; 4xx here means the *request* was bad.
    """
    account = await session.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"account {payload.account_id} not found",
        )

    # A charge posts against the account's own currency. If the caller named one,
    # it is an assertion to check rather than a conversion to perform -- there is
    # no FX in Ledgerline, so a mismatch is a mistake, not a request for a rate.
    currency = payload.currency or account.currency
    if currency != account.currency:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"currency mismatch: account {account.id} is denominated in "
                f"{account.currency}, request asked for {currency}"
            ),
        )

    # Per-request forcing of the fake processor. Deliberately explicit and local
    # rather than hidden in the dependency, so it is obvious at the call site that
    # a request can change the processor's behaviour -- and equally obvious what to
    # delete when a real adapter arrives.
    if payload.force_outcome is not None or payload.force_latency_ms is not None:
        processor = FakeProcessor(
            outcome=payload.force_outcome or settings.PROCESSOR_OUTCOME,
            latency_ms=(
                settings.PROCESSOR_LATENCY_MS
                if payload.force_latency_ms is None
                else payload.force_latency_ms
            ),
        )

    # 1. The payment exists before the processor is called, so that the row and the
    #    attempt are the same event. It is not committed yet -- see the module
    #    docstring on what that costs.
    payment = Payment(
        account_id=account.id,
        amount=payload.amount,
        currency=currency,
        status=PaymentStatus.CREATED,
    )
    session.add(payment)
    await session.flush()

    # 2. created -> processing, through the state machine like every other move.
    transition(payment, PaymentStatus.PROCESSING)
    await session.flush()

    # 3. The one call in this function that leaves the database. No ledger row has
    #    been written at this point, and that is the whole design.
    result = await processor.charge(payload.amount, currency)

    # 4a. Declined. Record the outcome on the payment and commit *that alone*. The
    #     ledger is not rolled back here because the ledger was never touched.
    if not result.succeeded:
        payment.processor_ref = result.processor_ref
        payment.failure_reason = result.failure_reason
        transition(payment, PaymentStatus.FAILED)
        await session.commit()
        await session.refresh(payment)
        return payment

    # 4b. Approved. Now, and only now, money moves.
    try:
        # DEBIT the house settlement account, CREDIT the customer. The house
        # account stands in for the outside world the money came from; its balance
        # goes negative by exactly what customers were credited, so the ledger
        # still sums to zero.
        house_id = await settlement_account_id(session, currency)
        ledger_transaction = await write_charge_posting(
            session,
            description=f"charge {payment.id}",
            debit_account_id=house_id,
            credit_account_id=account.id,
            amount=payload.amount,
        )
    except LedgerInvariantError as exc:
        # Ledgerline built a bad posting. Abandon everything, including the payment
        # row: see the module docstring's table.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"charge aborted, no money moved: {exc}",
        ) from exc

    payment.processor_ref = result.processor_ref
    payment.ledger_transaction_id = ledger_transaction.id
    transition(payment, PaymentStatus.SUCCEEDED)

    # One commit. The payment reaching 'succeeded' and the postings that justify it
    # become visible in the same instant, or neither does.
    await session.commit()
    await session.refresh(payment)
    return payment


@router.get("/{payment_id}", response_model=ChargeOut)
async def get_charge(
    payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Payment:
    """Read a payment back from the database.

    Exists so that "the state persisted" can be checked against the database
    rather than against the echo of the response that claimed to write it.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"payment {payment_id} not found"
        )
    return payment
