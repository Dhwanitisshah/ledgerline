"""The charge flow: a payment lifecycle wrapped around a processor call.

Five properties live here now. The first was established in Phase 2 and is the one
everything else is built on:

    **A charge that fails moves no money. Not less money -- none.**

The second arrived in Phase 3:

    **A charge that is sent twice with one key happens once.**

The third arrived in Phase 4:

    **A duplicate request in flight is turned away, not parked.**

The fourth is Phase 5a, which is the one that required rearranging everything
below:

    **A charge that the processor accepted is never silently lost, even if this
    process dies before it can write the result down.**

And the fifth is Phase 5b, which needed only one line added to the settlement:

    **Anything downstream is told about the charge if and only if the money moved.**

That line is ``record_payment_succeeded`` in step 6c. It writes a row to
``outbox_events`` inside transaction B, next to the postings, and publishes nothing
-- ``app/publisher.py`` does that afterwards and separately. The temptation it
replaces is a broker call after the commit, which is a dual write with no safe
ordering; see app/outbox.py for why there is no arrangement of two writes to two
systems that survives a crash in between.

## The problem the arrangement solves

Through Phase 4 this route ran as a single database transaction wrapped around the
processor call::

    BEGIN
      claim the key
      insert payment, move it to 'processing'
      ── call the processor ──          <- a third party, outside the transaction
      write the posting, move to 'succeeded'
      finalise the key
    COMMIT

Read that sequence and find the moment the process can die. It is the arrow. The
processor has taken the money, and every row that would record it is still sitting
uncommitted in a transaction that is about to be rolled back by a dying backend.
There is no error handler that helps, because the handler dies too. There is no
retry that helps, because nothing was written to retry *from*. The customer's card
is debited and Ledgerline does not know the attempt ever happened.

This is not a bug in the code as written. A single database transaction cannot span
a third party's system, so no arrangement of one transaction can make "the
processor charged the card" and "we recorded it" atomic. The only move available is
to stop trying, and instead **make the attempt durable before the money moves**, so
that a crash leaves evidence.

## How the transaction is arranged now

Under ``CHARGE_DURABILITY=durable_intent`` (the default) there are **two**
transactions with the processor call in the gap between them::

    BEGIN                                    -- transaction A: the intent
      claim the key                          (ON CONFLICT: replay or reject, stop)
      look up the account, check the currency
      insert payment, move it to 'processing'
      bind the key to the payment
    COMMIT                                   <- the attempt is now durable

    ── call the processor ──                 <- NO transaction open, NO locks held

    BEGIN                                    -- transaction B: the settlement
      success:  insert ledger_transaction + 2 entries, move to 'succeeded'
      failure:  move to 'failed'
      finalise the key with the response to replay
    COMMIT

Now the arrow is survivable. A crash there leaves a committed payment in
``processing`` -- a durable statement that Ledgerline *asked* for this charge and
does not know how it ended. ``app/reconcile.py`` finds those, asks the processor
what actually happened using the payment id as the attempt reference, and finishes
the job. The gap is closed not by making the two systems atomic, which is
impossible, but by making the inconsistency **detectable and repairable**.

Transaction B is still all-or-nothing, and that part is unchanged from Phase 2: the
posting, the status and the key land together or not at all.

## What rolls back, and what does not

Under ``durable_intent``:

| Outcome                          | payments row | ledger rows     | outbox    | key          |
| -------------------------------- | ------------ | --------------- | --------- | ------------ |
| processor succeeded              | `succeeded`  | committed       | **event** | completed    |
| processor declined               | `failed`     | **never written** | **none**| completed    |
| **crash after processor said ok**| `processing` | not written     | none      | **consumed** |
| ledger invariant violated (bug)  | `processing` | rolled back     | rolled back | **consumed** |
| bad request before A commits     | no row       | none            | none      | not consumed |
| replayed retry                   | untouched    | untouched       | untouched | unchanged    |

The outbox column tracks the ledger column exactly, in every row, and that is the
Phase 5b guarantee rather than a coincidence of how the table is laid out: both are
written by the same transaction, so no failure can produce one without the other.
Rows three and four are the ones to check -- a crash and a rolled-back settlement
each leave no event, because there is no money to announce. The sweep emits it
later, when and if there is.

("consumed" above means the key is held at ``in_progress`` with no response to
replay, so retries get a 409 until the sweep finalises it.)

Rows three and four are new, and they are the price of the fix. Both leave a
payment parked in ``processing`` and a key consumed with no response to replay, so
the customer's retries get a 409 rather than an answer. Neither state is
self-healing and neither is meant to be: the sweep is what resolves them, and it
finalises the key alongside the payment so that a crash on our side costs the
customer a short wait rather than a 24-hour lockout on their own idempotency key.

Row four deserves its own note, because it changed meaning. Through Phase 4, a
ledger invariant failure abandoned the payment row too -- the reasoning being that
a payment record which cannot be justified by balanced postings is worse than no
record, since it is a number someone will later trust. That reasoning still holds
for the *money*, and the posting is still rolled back entirely. What can no longer
be abandoned is the intent, because it was committed before the processor was
called. The payment therefore survives as ``processing``, which is not a claim that
anything succeeded -- it is a claim that we tried and do not yet know. That is
exactly what happened, and it is a better thing to have written down than nothing.

Row five is the one that did **not** change, and it matters: an unknown account, a
currency mismatch or a malformed body is rejected before transaction A commits, so
the claim goes down with it and the key stays free. Those paths never reach the
processor. There is no money to reconcile and no reason to consume anything.

## What Phase 5a does not close

The reconciler makes a lost outcome recoverable; it does not make it impossible.
Precisely:

* Between the processor being called and its books being written there is an
  instant where a crash leaves the payment ``processing`` and the processor with no
  record. The sweep settles that as ``failed``, which is correct **because the
  processor is the authority**: if it has no record, no money moved. A real
  processor makes the same guarantee through its idempotency-key lookup.
* The window is not eliminated but bounded: a payment can sit unresolved for up to
  ``RECONCILE_STUCK_AFTER_SECONDS`` plus one sweep interval. During it, the truth
  exists and is discoverable; it is just not discovered yet.
* If the sweep never runs, nothing is reconciled. Durability buys the *ability* to
  recover; it does not perform the recovery.
* None of this depends on Ledgerline alone. It requires the processor to answer
  "what happened to attempt X?" -- see ``ProcessorAdapter`` in app/processor.py. A
  processor without that API leaves a gap that cannot be closed from this side.

## A second thing the split bought

The processor call no longer happens inside an open write transaction. Phase 4
recorded the cost of that arrangement -- a slow authorisation pinning a Postgres
connection, holding whatever locks it had, for the length of a card round trip --
and fixed the *symptom* with a non-blocking advisory lock so duplicates were turned
away instead of queueing behind it. Phase 5a removes the cause. Set
``force_latency_ms`` high and watch: under ``single_txn`` a backend sits in
``idle in transaction`` for the whole call, and under ``durable_intent`` it does
not, because there is no transaction to be idle in.
"""

import asyncio
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.config import settings
from app.deps import (
    get_processor,
    get_session,
    processor_books,
    reject_test_affordances_if_disabled,
)
from app.idempotency import (
    MAX_KEY_LENGTH,
    bind_payment,
    claim_key,
    claim_key_naive,
    finalize_key,
    finalize_key_naive,
    load_claim,
    request_fingerprint,
)
from app.ledger import LedgerInvariantError, settlement_account_id, write_posting
from app.locking import try_lock_idempotency_key
from app.models import Account, Payment, PaymentStatus
from app.outbox import record_payment_succeeded
from app.payments import transition
from app.processor import FakeProcessor, ProcessorAdapter
from app.schemas import ChargeCreate, ChargeOut
from app.strategies import ChargeDurability, ClaimStrategy

router = APIRouter(prefix="/charges", tags=["charges"])


class SimulatedCrash(RuntimeError):
    """Raised by ``force_crash_after_processor`` to abandon a charge mid-flight.

    An exception rather than ``os._exit()``, and the difference is worth being
    honest about. To Postgres they are the same event -- a transaction abandoned
    without a COMMIT, rolled back either by the session closing or by the backend
    dying -- and that equivalence is what makes this a fair reproduction of the
    crash rather than a mock of it.

    What it does not reproduce is the process failing to come back. That is
    deliberate and it is why the reconciler is a separate module runnable as a
    separate process: recovery that lives in this process is recovery that dies
    with it.
    """


def _replay_or_reject(key: str, existing, request_hash: str) -> JSONResponse:
    """Turn a claim this request did not win into the right answer.

    Shared by both strategies so that "what a loser gets" is decided in one place
    and cannot drift between the broken path and the fixed one -- which matters,
    because the difference between them must be the *locking*, not the semantics.
    """
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a charge for idempotency key {key} is still in progress",
        )

    return JSONResponse(
        content=existing.response_snapshot, status_code=existing.response_status
    )


async def _widen_the_race_window() -> None:
    """Hold still, so a race that already exists becomes one you can reproduce.

    Only ever called on the naive path. This does not create the bug: the gap
    between reading a key and writing it is two statements and a processor call
    wide at zero milliseconds. It makes that gap reliably observable rather than
    dependent on how the event loop happened to interleave.
    """
    if settings.NAIVE_RACE_WINDOW_MS > 0:
        await asyncio.sleep(settings.NAIVE_RACE_WINDOW_MS / 1000)


async def _complete(
    session: AsyncSession,
    key: str,
    request_hash: str,
    payment: Payment,
    strategy: ClaimStrategy,
) -> JSONResponse:
    """Record the outcome against the idempotency key and commit everything.

    Both charge outcomes end here, and both end with a single commit that covers
    the settlement: the payment's final status, its postings (if any), and the key
    that now owes this response to any retry. The three cannot disagree because
    they land together.

    On the fixed path the response is built from what ``finalize_key`` read back
    out of the database, not from the dict that went in, so the first response and
    every replay are serialised from identical bytes.
    """
    await session.flush()
    await session.refresh(payment)

    body = ChargeOut.model_validate(payment).model_dump(mode="json")
    if strategy is ClaimStrategy.NAIVE:
        stored = await finalize_key_naive(
            session, key, request_hash, body, status.HTTP_201_CREATED
        )
    else:
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
    * same key while the first attempt is **still running** -> 409, now decided by
      the committed ``in_progress`` claim rather than only by the advisory lock.
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

    # Phase 7: the fake processor's knobs are not reachable in production. Checked
    # before the key is claimed, so a refused request consumes nothing.
    reject_test_affordances_if_disabled(
        force_outcome=payload.force_outcome,
        force_latency_ms=payload.force_latency_ms,
        force_crash_after_processor=payload.force_crash_after_processor,
    )

    request_hash = request_fingerprint(
        account_id=payload.account_id, amount=payload.amount, currency=payload.currency
    )
    strategy = settings.IDEMPOTENCY_CLAIM_STRATEGY
    durability = settings.CHARGE_DURABILITY

    # =========================================================================
    # Transaction A: make the attempt durable before any money can move.
    # =========================================================================

    if strategy is ClaimStrategy.NAIVE:
        # --- The preserved broken path. Not a default; see app/strategies.py. ---
        existing = await claim_key_naive(session, key)
        if existing is not None:
            return _replay_or_reject(key, existing, request_hash)
        # `None` means "no key a moment ago", which this path then treats as "no
        # charge is happening". Under concurrency that is false, and the request
        # walks straight into charging a card another request is already charging.
        await _widen_the_race_window()
    else:
        # --- The shipped path ---------------------------------------------------
        # Gate on a non-blocking advisory lock *before* the claim. Its job shrank in
        # Phase 5a but did not disappear. Transaction A is now short and commits
        # before the processor is called, so a duplicate arriving after that commit
        # is turned away by the committed 'in_progress' claim, with no lock
        # involved. The lock still covers the window *inside* transaction A, where
        # the claim exists but is invisible -- and without it a duplicate landing in
        # that window would block on the uncommitted row instead of being refused.
        #
        # Smaller window, same argument: a duplicate should cost a 409 and a few
        # milliseconds, never a parked connection.
        if not await try_lock_idempotency_key(session, key):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"a charge for idempotency key {key} is already in progress; "
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
            return _replay_or_reject(key, existing, request_hash)

    # Everything from here to the end of transaction A is validation, and every
    # rejection in it rolls the claim back with the request. That is why a bad
    # request still does not consume its key: none of these paths reach the
    # processor, so there is no money to reconcile later.
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
    # delete when a real adapter arrives. The books are passed through unchanged:
    # an override changes what the processor *decides*, never whether it remembers.
    if payload.force_outcome is not None or payload.force_latency_ms is not None:
        processor = FakeProcessor(
            outcome=payload.force_outcome or settings.PROCESSOR_OUTCOME,
            latency_ms=(
                settings.PROCESSOR_LATENCY_MS
                if payload.force_latency_ms is None
                else payload.force_latency_ms
            ),
            books=processor_books,
        )

    # 1. The payment exists before the processor is called, so that the row and the
    #    attempt are the same event.
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

    if durability is ChargeDurability.DURABLE_INTENT:
        # 3. Tie the key to the payment, so a sweep that settles this payment can
        #    find the key still waiting on it. Skipped on the naive claim path,
        #    which has no key row yet to tie anything to.
        if strategy is not ClaimStrategy.NAIVE:
            await bind_payment(session, key, payment.id)

        # 4. The commit that makes the difference. Past this line, no failure --
        #    exception, rollback, kill -9, power cut -- can erase the fact that
        #    Ledgerline was about to charge this card.
        await session.commit()

    # =========================================================================
    # The processor call. Under durable_intent there is no open transaction here,
    # so nothing is locked and no connection is held mid-write while a third party
    # takes its time.
    # =========================================================================

    # 5. The attempt reference is the payment id, and it is doing real work: it is
    #    the processor-side idempotency key, so charging twice on it cannot take the
    #    money twice, and it is the handle the reconciler quotes when it asks what
    #    happened. A random ref generated here and then lost in the crash would make
    #    the attempt unaskable-about.
    result = await processor.charge(
        attempt_ref=payment.id, amount=payload.amount, currency=currency
    )

    # Counted here: the processor answered, so this is a real charge attempt with a
    # real outcome. Deliberately after the call and before the crash knob, so a
    # simulated crash still counts the attempt the card actually saw.
    metrics.observe_charge(succeeded=result.succeeded)

    if payload.force_crash_after_processor:
        # Exactly the instant Phase 2 named as unrecoverable. The card has been
        # charged and committed on the processor's side; nothing has been written
        # on ours. Under single_txn this erases the payment as well. Under
        # durable_intent it leaves it in 'processing' for the sweep.
        raise SimulatedCrash(
            f"simulated crash after the processor answered for payment {payment.id}"
        )

    # =========================================================================
    # Transaction B: settle. All-or-nothing, exactly as Phase 2 built it.
    # =========================================================================

    # 6a. Declined. Record the outcome on the payment and commit *that alone*. The
    #     ledger is not rolled back here because the ledger was never touched.
    if not result.succeeded:
        payment.processor_ref = result.processor_ref
        payment.failure_reason = result.failure_reason
        transition(payment, PaymentStatus.FAILED)
        # A decline is a completed outcome, so the key is finalised against it. A
        # retry replays the decline rather than trying the card again -- the
        # customer's second click must not become a second authorisation attempt.
        return await _complete(session, key, request_hash, payment, strategy)

    # 6b. Approved. Now, and only now, money moves.
    try:
        # DEBIT the house settlement account, CREDIT the customer. The house
        # account stands in for the outside world the money came from; its balance
        # goes negative by exactly what customers were credited, so the ledger
        # still sums to zero.
        house_id = await settlement_account_id(session, currency)
        ledger_transaction = await write_posting(
            session,
            description=f"charge {payment.id}",
            debit_account_id=house_id,
            credit_account_id=account.id,
            amount=payload.amount,
        )
    except LedgerInvariantError as exc:
        # Ledgerline built a bad posting. Abandon the settlement entirely -- no
        # half-posting is committed. Under durable_intent the *intent* survives in
        # 'processing', because it was committed before this transaction began; see
        # the module docstring's table for why that is the right thing to be left
        # with rather than a regression.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"charge aborted, no money moved: {exc}",
        ) from exc

    payment.processor_ref = result.processor_ref
    payment.ledger_transaction_id = ledger_transaction.id
    transition(payment, PaymentStatus.SUCCEEDED)

    # 6c. The outbox row, written *here* -- inside transaction B, alongside the
    #     postings it describes. This is the entire transactional outbox: not a
    #     publish, not a broker call, just an INSERT that shares a COMMIT with the
    #     money. The alternative, publishing after the commit, is a dual write with
    #     no safe ordering; see app/outbox.py. Nothing is delivered by this request.
    await record_payment_succeeded(session, payment)

    # One commit. The payment reaching 'succeeded', the postings that justify it,
    # the event announcing it, and the idempotency key that now owes this response
    # become visible in the same instant, or none of them do.
    return await _complete(session, key, request_hash, payment, strategy)


@router.get("/{payment_id}", response_model=ChargeOut)
async def get_charge(
    payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Payment:
    """Read a payment back from the database.

    Exists so that "the state persisted" can be checked against the database
    rather than against the echo of the response that claimed to write it -- and,
    since Phase 5a, so that a payment abandoned in ``processing`` can be observed
    sitting there before and after the sweep runs.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"payment {payment_id} not found"
        )
    return payment
