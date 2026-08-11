"""Withdrawals, and the overdraw race they exist to demonstrate.

A withdrawal is the mirror of a charge: it DEBITs the customer and CREDITs the
house settlement account, so the customer's balance falls and the ledger still
sums to zero. It is the first operation in Ledgerline that has to *refuse* -- a
charge can always be accepted, but a withdrawal has to check whether the money is
there, and checking is where concurrency bugs live.

## The race

The naive version reads a balance, decides, and writes::

    balance = SELECT SUM(...) FROM ledger_entries WHERE account_id = ...
    if balance - amount < floor: reject
    INSERT the posting
    COMMIT

Two withdrawals of 600 against a balance of 1000, arriving together:

    request A          request B
    ---------          ---------
    reads 1000
                       reads 1000
    1000-600 >= 0 OK
                       1000-600 >= 0 OK
    writes -600
                       writes -600
    COMMIT             COMMIT        -> balance is now -200

Neither request did anything wrong in isolation. Both read a true balance and
applied a correct rule to it. The balance was simply no longer true by the time
either one acted, and nothing in the database was arranged to notice.

## Why a constraint cannot fix this

The obvious instinct is a CHECK constraint -- "balance >= 0" -- and it cannot be
written. A CHECK constraint sees only the row being written, and the balance is a
SUM over *other* rows. A trigger can compute the sum, but a trigger under READ
COMMITTED sees the same pre-race snapshot both transactions see, so it reaches the
same wrong answer twice. The problem is not that the check is in the wrong place;
it is that the check and the write are two moments, and something has to make them
one.

That is what the lock does. See ``WithdrawalGuard.ROW_LOCK`` and app/locking.py.

## Not here on purpose

Withdrawals take no ``Idempotency-Key``. Making them idempotent would be a
mechanical repeat of Phase 3 against a second endpoint, and Phase 4 is about the
overdraw race. A retried withdrawal will withdraw twice; that gap is real and is
noted in the README rather than quietly half-fixed.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_session
from app.ledger import (
    LedgerInvariantError,
    account_balance,
    settlement_account_id,
    write_posting,
)
from app.locking import lock_account_for_update
from app.models import Account
from app.schemas import WithdrawalCreate, WithdrawalOut
from app.strategies import WithdrawalGuard

router = APIRouter(prefix="/withdrawals", tags=["withdrawals"])


@router.post(
    "",
    response_model=WithdrawalOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Unknown account"},
        422: {"description": "Insufficient funds, or currency mismatch"},
    },
)
async def create_withdrawal(
    payload: WithdrawalCreate, session: AsyncSession = Depends(get_session)
) -> WithdrawalOut:
    """Debit an account, refusing to take it below the configured floor.

    Which guard runs is decided by ``WITHDRAWAL_GUARD``. Both paths do the same
    arithmetic and return the same shapes; they differ only in whether the balance
    is read under a lock. That is deliberate -- it keeps the before/after a
    comparison of one variable.
    """
    account = await session.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"account {payload.account_id} not found",
        )

    currency = payload.currency or account.currency
    if currency != account.currency:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"currency mismatch: account {account.id} is denominated in "
                f"{account.currency}, request asked for {currency}"
            ),
        )

    floor = settings.BALANCE_FLOOR_MINOR_UNITS

    if settings.WITHDRAWAL_GUARD is WithdrawalGuard.ROW_LOCK:
        # Take the row lock BEFORE reading the balance. Order is the whole point:
        # a lock taken after the read would protect nothing, because the value it
        # is meant to protect has already been copied into a Python variable.
        #
        # From here to COMMIT, no other withdrawal against this account can read
        # its balance. They queue on this statement, and each one in turn reads a
        # balance that already includes every withdrawal ahead of it.
        if not await lock_account_for_update(session, account.id):  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"account {payload.account_id} not found",
            )
        balance = await account_balance(session, account.id)
    else:
        # --- The preserved broken path. Not a default; see app/strategies.py. ---
        # An unlocked read. The number is true when it arrives and stale by the
        # time it is used, and nothing in between will say so.
        balance = await account_balance(session, account.id)
        if settings.NAIVE_RACE_WINDOW_MS > 0:
            await asyncio.sleep(settings.NAIVE_RACE_WINDOW_MS / 1000)

    remaining = balance - payload.amount
    if remaining < floor:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"insufficient funds: balance {balance} minus {payload.amount} "
                f"would leave {remaining}, below the floor of {floor} (minor units)"
            ),
        )

    try:
        house_id = await settlement_account_id(session, currency)
        ledger_transaction = await write_posting(
            session,
            description=f"withdrawal from {account.id}",
            debit_account_id=account.id,
            credit_account_id=house_id,
            amount=payload.amount,
        )
    except LedgerInvariantError as exc:  # pragma: no cover - defensive
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"withdrawal aborted, no money moved: {exc}",
        ) from exc

    await session.commit()

    return WithdrawalOut(
        ledger_transaction_id=ledger_transaction.id,
        account_id=account.id,
        amount=payload.amount,
        currency=currency,
        balance_after=remaining,
    )
