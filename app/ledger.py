"""The money rules, expressed as explicit SQL.

These are written as raw SQL rather than ORM queries so that the database
behaviour is visible on the page instead of buried in a query builder:

1. :func:`account_balance` -- the derived balance. There is no balance column.
2. :func:`transaction_totals` -- the sum-to-zero invariant check, run against the
   rows actually flushed to the database, before commit.
3. :func:`transaction_currencies` -- the single-currency invariant check, likewise
   read back from the flushed rows.

Balance convention (used everywhere, no exceptions)::

    balance = SUM(credit amounts) - SUM(debit amounts)

So crediting an account raises its balance and debiting it lowers it. A single
convention applied consistently matters more than which one you pick; this one is
stated once here, repeated in the README, and never re-litigated in a route.

A note on types: ``SUM()`` over a ``bigint`` column returns ``numeric`` in
Postgres, which asyncpg hands back as a Python ``Decimal``. Every SUM below is
cast back to ``bigint`` so the value reaching Python is a plain ``int``. Money
does not become a Decimal on its way out of the database.
"""

import uuid
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Derived balance for one account: credits positive, debits negative.
_BALANCE_SQL = text(
    """
    SELECT COALESCE(
        SUM(
            CASE direction
                WHEN 'credit' THEN amount
                WHEN 'debit'  THEN -amount
            END
        ),
        0
    )::bigint AS balance
    FROM ledger_entries
    WHERE account_id = :account_id
    """
)

# Totals for one posting, used to prove debits == credits before commit.
_TOTALS_SQL = text(
    """
    SELECT
        COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)::bigint  AS debits,
        COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)::bigint AS credits
    FROM ledger_entries
    WHERE transaction_id = :transaction_id
    """
)

# The distinct currencies a posting touches, resolved through the accounts its
# entries reference. More than one means the posting cannot be summed at all.
_CURRENCIES_SQL = text(
    """
    SELECT DISTINCT accounts.currency AS currency
    FROM ledger_entries
    JOIN accounts ON accounts.id = ledger_entries.account_id
    WHERE ledger_entries.transaction_id = :transaction_id
    ORDER BY currency
    """
)


class TransactionTotals(NamedTuple):
    """Debit and credit totals for a single ledger transaction, in minor units."""

    debits: int
    credits: int

    @property
    def is_balanced(self) -> bool:
        """True when the posting sums to zero.

        A posting with no entries at all totals 0 == 0, which is arithmetically
        balanced but is not a posting; callers reject empty sets before getting
        here (see ``TransactionCreate.entries``), and the explicit check for a
        zero-sided posting below keeps that honest.
        """
        return self.debits == self.credits and self.debits > 0


async def account_balance(session: AsyncSession, account_id: uuid.UUID) -> int:
    """Return the account's balance in minor units, derived from the ledger.

    This is the only way a balance is ever produced. If you find yourself wanting
    to cache this in a column, that is the anti-pattern this project is about.
    """
    result = await session.execute(_BALANCE_SQL, {"account_id": account_id})
    return int(result.scalar_one())


async def transaction_totals(session: AsyncSession, transaction_id: uuid.UUID) -> TransactionTotals:
    """Sum the debits and credits already written for one transaction.

    Deliberately reads back from the database rather than re-adding the numbers
    that are still sitting in Python objects: what matters is that the rows *in
    the transaction* balance, not that the request body did.
    """
    row = (await session.execute(_TOTALS_SQL, {"transaction_id": transaction_id})).one()
    return TransactionTotals(debits=int(row.debits), credits=int(row.credits))


async def transaction_currencies(session: AsyncSession, transaction_id: uuid.UUID) -> list[str]:
    """Return the distinct account currencies a posting touches, sorted.

    Like :func:`transaction_totals`, this resolves currencies from the database
    rather than trusting the request: the caller never states a currency, so the
    only honest source is the ``accounts`` rows the entries actually point at.

    A posting spanning more than one currency is not a posting that happens to be
    unbalanced -- it is one whose totals cannot be compared at all, since 100 paise
    and 100 cents are different units wearing the same integer.
    """
    rows = await session.execute(_CURRENCIES_SQL, {"transaction_id": transaction_id})
    return [row.currency for row in rows]
