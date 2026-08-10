"""Posting a balanced double-entry transaction."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.ledger import transaction_currencies, transaction_totals
from app.models import LedgerEntry, LedgerTransaction
from app.routers.accounts import load_accounts
from app.schemas import EntryOut, TransactionCreate, TransactionOut

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    payload: TransactionCreate, session: AsyncSession = Depends(get_session)
) -> TransactionOut:
    """Post a set of entries atomically as one balanced ledger transaction.

    The shape of this function is the point of Phase 1:

    1. Write every entry inside a single database transaction.
    2. Ask the database which currencies those entries touch, and to total the
       debits and credits it now holds.
    3. Commit only if the posting is single-currency and sums to zero; otherwise
       roll the whole thing back and 422.

    Step 2 reads back from the database rather than re-adding the request body,
    so the thing being verified is the state that would actually be committed.
    Because the checks run before COMMIT, a rejected posting leaves nothing
    behind -- not an orphaned transaction row, not a half-written entry.
    """
    account_ids = {entry.account_id for entry in payload.entries}
    existing = await load_accounts(session, account_ids)
    missing = account_ids - existing
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown account_id(s): {', '.join(sorted(str(i) for i in missing))}",
        )

    transaction = LedgerTransaction(description=payload.description)
    session.add(transaction)
    entries = [
        LedgerEntry(
            transaction=transaction,
            account_id=entry.account_id,
            direction=entry.direction,
            amount=entry.amount,
        )
        for entry in payload.entries
    ]
    session.add_all(entries)

    # Rows are on disk but not committed. Now make the database prove they balance.
    await session.flush()

    # Currency is checked first, and not merely for tidiness: summing debits
    # against credits across different currencies is meaningless arithmetic, so
    # "does it balance?" is not a question worth asking until the answer to "is it
    # one currency?" is yes. Otherwise 100 paise appears to offset 100 cents.
    currencies = await transaction_currencies(session, transaction.id)
    if len(currencies) > 1:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "mixed-currency posting: all entries must share one currency "
                f"(got {', '.join(currencies)})"
            ),
        )

    totals = await transaction_totals(session, transaction.id)

    if not totals.is_balanced:
        await session.rollback()
        if totals.debits == 0 or totals.credits == 0:
            detail = (
                f"single-sided posting: debits={totals.debits} credits={totals.credits} "
                "(minor units); a ledger transaction needs at least one debit and "
                "one credit"
            )
        else:
            detail = (
                f"unbalanced posting: debits={totals.debits} != credits={totals.credits} "
                "(minor units); every transaction must sum to zero"
            )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)

    await session.commit()

    return TransactionOut(
        id=transaction.id,
        description=transaction.description,
        created_at=transaction.created_at,
        entries=[
            EntryOut(
                id=entry.id,
                account_id=entry.account_id,
                direction=entry.direction,
                amount=entry.amount,
            )
            for entry in entries
        ],
    )
