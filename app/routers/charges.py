"""The charge flow: a payment lifecycle wrapped around a processor call.

Two properties live here. The first was established in Phase 2 and is the one
everything else is built on:

    **A charge that fails moves no money. Not less money -- none.**

The second arrived in Phase 3:

    **A charge that is sent twice with one key happens once.**

The route still takes no lock (Phase 4) and emits no event (Phase 5).

## How the transaction is arranged

Everything below happens inside one database transaction, opened implicitly by
the first statement and ended by exactly one ``commit()`` on each path::

    INSERT idempotency claim  (ON CONFLICT: replay or reject, and stop here)
    INSERT payment (created)
    UPDATE payment -> processing
    ── call the processor ───────────────────────  (no database work in flight)
    success:  INSERT ledger_transaction + 2 entries, UPDATE payment -> succeeded
    failure:  UPDATE payment -> failed
    UPDATE idempotency claim -> completed, with the response to replay
    COMMIT

The claim sharing the charge's transaction is what makes a half-finished request
safe: if the charge does not commit, the key was never consumed, and the retry
that follows is free to try again. See app/idempotency.py.

## What rolls back, and what does not

| Outcome                          | payments row          | ledger rows        | key          |
| -------------------------------- | --------------------- | ------------------ | ------------ |
| processor succeeded              | committed `succeeded` | committed, balanced| completed    |
| processor declined               | committed `failed`    | **never written**  | completed    |
| ledger invariant violated (bug)  | rolled back, no row   | rolled back        | not consumed |
| illegal transition (bug)         | rolled back, no row   | rolled back        | not consumed |
| replayed retry                   | untouched             | untouched          | unchanged    |

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

And the idempotency gap, equally plainly: Phase 3 handles a retry that arrives
*after* the first request finished -- the double-click, the client that retried a
lost response. Two requests genuinely **in flight at once** on the same key are a
different problem, because the first claim is not visible to anyone until it
commits. Phase 4 owns that, and it is why the claim's 'in_progress' status exists
but is never observed here.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_processor, get_session
from app.idempotency import (
    MAX_KEY_LENGTH,
    claim_key,
    finalize_key,
    load_claim,
    request_fingerprint,
)
from app.ledger import LedgerInvariantError, settlement_account_id, write_charge_posting
from app.models import Account, Payment, PaymentStatus
from app.payments import transition
from app.processor import FakeProcessor, ProcessorAdapter
from app.schemas import ChargeCreate, ChargeOut

router = APIRouter(prefix="/charges", tags=["charges"])


async def _complete(session: AsyncSession, key: str, payment: Payment) -> JSONResponse:
    """Record the outcome against the idempotency key and commit everything.

    Both charge outcomes end here, and both end with a single commit that covers
    the payment, its postings (if any), and the key that now owes this response to
    any retry. The three cannot disagree because they land together.

    The response is built from what ``finalize_key`` read back out of the database,
    not from the dict that went in, so the first response and every replay are
    serialised from identical bytes.
    """
    await session.flush()
    await session.refresh(payment)

    body = ChargeOut.model_validate(payment).model_dump(mode="json")
    stored = await finalize_key(session, key, body, status.HTTP_201_CREATED)

    await session.commit()
    return JSONResponse(content=stored.body, status_code=stored.status_code)


@router.post(
    "",
    response_model=ChargeOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Idempotency-Key header missing, blank, or too long"},
        404: {"description": "Unknown account"},
        409: {"description": "A charge for this key is still in progress"},
        422: {"description": "Idempotency-Key reused with a different payload, or invalid body"},
    },
)
async def create_charge(
    payload: ChargeCreate,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        # Declared optional so a missing header can be answered with a 400 rather
        # than FastAPI's automatic 422 for a required header. It is required in
        # practice; the description carries what the type cannot.
        description=(
            "REQUIRED. Identifies this charge attempt so a retry replays the "
            "original response instead of charging again. Reuse the same key for "
            "every retry of one charge; use a new key for a new charge. Omitting "
            "it is a 400. Keys expire after 24 hours."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    processor: ProcessorAdapter = Depends(get_processor),
) -> JSONResponse:
    """Attempt a charge, and record honestly what happened.

    Returns **201 on both outcomes**, including a declined charge. The payment
    resource was created either way, and a decline is a business result rather
    than a failed request: the caller asked Ledgerline to attempt a charge and
    Ledgerline did, then recorded the answer. Signalling that with a 4xx would
    conflate "your request was wrong" with "the card said no", and would make the
    replay-the-recorded-outcome behaviour below awkward for no benefit. Read
    ``status`` to find out which happened; 4xx here means the *request* was bad.

    **An ``Idempotency-Key`` header is required.** Every retry of the same charge
    must carry the same key, and a key is a promise about one specific request:

    * same key, same body -> the stored response, replayed byte for byte. No
      second payment, no second posting, and the processor is not called again.
    * same key, different body -> 422, and nothing is written.
    * no key -> 400. Made mandatory rather than optional because an optional
      safety net is one that is missing from exactly the client that needed it,
      and a caller who has not thought about retries is the caller most likely to
      send one.
    * expired key (24h) -> treated as never used, and the charge proceeds.
    """
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

    request_hash = request_fingerprint(
        account_id=payload.account_id, amount=payload.amount, currency=payload.currency
    )

    # The claim is the first thing that touches the database, and it runs in the
    # same transaction as everything below it. A request that fails anywhere after
    # this point takes the claim down with it, leaving the key free to retry.
    if not await claim_key(session, key, request_hash):
        existing = await load_claim(session, key)
        if existing is None:  # pragma: no cover - the claim just told us it exists
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"idempotency key {key} vanished between claim and read",
            )

        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "idempotency key reused with a different payload: this key is "
                    "already bound to another charge. Use a new key for a new "
                    "charge, or resend the original body to replay its result."
                ),
            )

        if not existing.is_completed:
            # Unreachable in Phase 3: a claim becomes visible to another request
            # only when it commits, and it commits only once completed. Phase 4
            # commits the claim up front so an in-flight charge *is* visible, and
            # this is the branch that will handle it.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a charge for idempotency key {key} is still in progress",
            )

        return JSONResponse(
            content=existing.response_snapshot, status_code=existing.response_status
        )

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
        # A decline is a completed outcome, so the key is finalised against it. A
        # retry replays the decline rather than trying the card again -- the
        # customer's second click must not become a second authorisation attempt.
        return await _complete(session, key, payment)

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

    # One commit. The payment reaching 'succeeded', the postings that justify it,
    # and the idempotency key that now owes this response become visible in the
    # same instant, or none of them do.
    return await _complete(session, key, payment)


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
