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

from app.models import EntryDirection, LedgerEntry, LedgerTransaction

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


# --- Phase 2: the posting a successful charge writes ---------------------------


class LedgerInvariantError(RuntimeError):
    """A posting *this service* built has failed the ledger invariants.

    Distinct from the 422 that ``POST /transactions`` returns for a caller's
    unbalanced request. That is bad input; this is a bug -- Ledgerline itself
    constructed a posting that does not balance or spans two currencies. It is
    raised rather than returned so the charge flow cannot shrug and commit it: the
    only correct response to "the money I just built is wrong" is to abandon the
    whole transaction.
    """


# Every account whose name starts with this prefix is a house account rather than
# a customer's. Migration 0003 puts a partial UNIQUE index over exactly this
# prefix, so there can never be two settlement accounts for one currency.
SETTLEMENT_ACCOUNT_PREFIX = "house:"


def settlement_account_name(currency: str) -> str:
    """The well-known name of the house account that funds charges in ``currency``."""
    return f"{SETTLEMENT_ACCOUNT_PREFIX}card_settlement:{currency}"


# Get-or-create in one statement. ON CONFLICT here is not concurrency control in
# the Phase 4 sense -- no lock is taken and there is no retry loop; it is a UNIQUE
# index doing the one job a UNIQUE index exists to do. Written as
# SELECT-then-INSERT instead, this would leave a window where two charges each
# create their own settlement account and the house balance silently splits in two.
_UPSERT_SETTLEMENT_SQL = text(
    """
    INSERT INTO accounts (name, currency)
    VALUES (:name, :currency)
    ON CONFLICT (name) WHERE name LIKE 'house:%'
    DO NOTHING
    """
)

_SELECT_SETTLEMENT_SQL = text("SELECT id FROM accounts WHERE name = :name")


async def settlement_account_id(session: AsyncSession, currency: str) -> uuid.UUID:
    """Return the house settlement account for ``currency``, creating it if needed.

    A charge is money entering Ledgerline from outside it, and double-entry has no
    way to write "from outside" -- every leg needs an account. The house account is
    that counterparty: it is debited by exactly as much as customer accounts are
    credited, so its balance is the negative of everything ever funded in that
    currency, and the ledger as a whole still sums to zero.

    One per currency, because a posting may not mix currencies.
    """
    name = settlement_account_name(currency)
    await session.execute(_UPSERT_SETTLEMENT_SQL, {"name": name, "currency": currency})
    row = (await session.execute(_SELECT_SETTLEMENT_SQL, {"name": name})).one()
    return row.id


async def write_charge_posting(
    session: AsyncSession,
    *,
    description: str,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    amount: int,
) -> LedgerTransaction:
    """Write the two-legged posting for a successful charge, and prove it balances.

    Called only after the processor has said yes. Nothing writes a ledger row on
    speculation, which is what makes the failure path trivially safe: there is no
    partial posting to clean up because there was never a partial posting.

    The invariants are re-checked here even though this function builds both legs
    itself and they balance by construction. That is deliberate: "balanced by
    construction" is a property of the code as written today, and the check costs
    one round trip against rows already sitting in the transaction. If a later edit
    breaks the construction, this raises instead of committing a broken ledger. The
    invariants are the Phase 1 ones, unchanged and reused.

    Raises :class:`LedgerInvariantError` *without* rolling back -- the caller owns
    the transaction and decides what to abandon.
    """
    transaction = LedgerTransaction(description=description)
    session.add(transaction)
    session.add_all(
        [
            LedgerEntry(
                transaction=transaction,
                account_id=debit_account_id,
                direction=EntryDirection.DEBIT,
                amount=amount,
            ),
            LedgerEntry(
                transaction=transaction,
                account_id=credit_account_id,
                direction=EntryDirection.CREDIT,
                amount=amount,
            ),
        ]
    )

    # Rows are in the transaction but not committed. Make the database prove them.
    await session.flush()

    currencies = await transaction_currencies(session, transaction.id)
    if len(currencies) != 1:
        raise LedgerInvariantError(
            f"charge posting spans {len(currencies)} currencies "
            f"({', '.join(currencies) or 'none'}); a posting must be single-currency"
        )

    totals = await transaction_totals(session, transaction.id)
    if not totals.is_balanced:
        raise LedgerInvariantError(
            f"charge posting does not balance: debits={totals.debits} "
            f"credits={totals.credits} (minor units)"
        )

    return transaction
