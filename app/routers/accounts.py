"""Account creation and the derived-balance read."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.ledger import account_balance
from app.models import Account
from app.schemas import AccountCreate, AccountOut, BalanceOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate, session: AsyncSession = Depends(get_session)
) -> Account:
    account = Account(name=payload.name, currency=payload.currency)
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


@router.get("/{account_id}/balance", response_model=BalanceOut)
async def get_account_balance(
    account_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> BalanceOut:
    """Return the account's balance, derived by SUM over its ledger entries.

    The account row is fetched only for its currency and to distinguish "account
    does not exist" (404) from "account exists and has never been posted to"
    (200 with a balance of 0). The balance itself never comes from this row.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"account {account_id} not found"
        )

    balance = await account_balance(session, account_id)
    return BalanceOut(account_id=account.id, currency=account.currency, balance=balance)


async def load_accounts(session: AsyncSession, account_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Return the subset of ``account_ids`` that actually exist."""
    rows = await session.execute(select(Account.id).where(Account.id.in_(account_ids)))
    return set(rows.scalars())
