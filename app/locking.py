"""Explicit concurrency control, written as SQL you can read.

Every lock in Ledgerline is taken by a statement in this module. Nothing here
hides behind the ORM, because the whole subject of Phase 4 is *which* rows are
locked, *when*, and *for how long* -- and a session-level abstraction that decides
that for you is the thing that makes concurrency bugs invisible until production.

Two mechanisms, chosen for two different shapes of problem:

``try_lock_idempotency_key``
    A transaction-scoped **advisory** lock, taken with ``pg_try_advisory_xact_lock``
    -- the non-blocking variant. Advisory because there is no row to lock: the
    thing being serialised is "a charge for this key", which at the moment the
    lock is needed does not exist yet.

``lock_account_for_update``
    A row lock on ``accounts``, taken with ``SELECT ... FOR UPDATE``. Real row,
    real lock, held to the end of the transaction.

Both are transaction-scoped rather than session-scoped, which matters more than it
looks: a transaction-scoped lock is released by ``COMMIT``, by ``ROLLBACK``, and by
the backend dying. There is no path -- including a hard crash mid-charge -- that
leaks one. A ``pg_advisory_lock`` (session-scoped) would need an explicit unlock,
and every explicit unlock is a line that some error path fails to reach.
"""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def advisory_lock_id(key: str) -> int:
    """Map an idempotency key onto the bigint that ``pg_advisory_*`` requires.

    BLAKE2b truncated to 8 bytes and read as a signed 64-bit integer, so the whole
    bigint range is used and the value is stable across processes and restarts.

    Computed in Python rather than with Postgres's ``hashtext()`` on purpose:
    ``hashtext`` is an internal function with no compatibility promise, and its
    output has changed between major versions. A lock id that silently changes
    under you during an upgrade is a lock that stops locking.

    Collisions are possible -- 2^64 values, and two different keys could in
    principle map to one lock. The consequence is bounded and benign: two
    unrelated charges would serialise against each other for the length of one
    processor call. They would not corrupt each other, because the lock is a
    gate, not the correctness mechanism -- the idempotency claim behind it is.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


# pg_try_advisory_xact_lock returns immediately: true when the lock was taken,
# false when someone else holds it. The blocking sibling (pg_advisory_xact_lock)
# would park this backend until the holder commits -- see the module docstring in
# app/routers/charges.py for why that is the thing being fixed rather than used.
_TRY_ADVISORY_LOCK_SQL = text("SELECT pg_try_advisory_xact_lock(:lock_id) AS acquired")

# FOR UPDATE takes an exclusive row lock held until the transaction ends. Any other
# transaction that runs this same statement for this same account waits here. Only
# the id is selected: the row is being used as a mutex, and reading columns from it
# would suggest the balance lives there, which it does not -- balances are derived
# by SUM over ledger_entries and always have been.
_LOCK_ACCOUNT_SQL = text("SELECT id FROM accounts WHERE id = :account_id FOR UPDATE")


async def try_lock_idempotency_key(session: AsyncSession, key: str) -> bool:
    """Try to become the request that charges ``key``. False means someone else is.

    Returns immediately either way. A caller that gets False has learned something
    the database could not otherwise tell it: a charge for this key is *in flight
    right now*, as opposed to finished (which the claim row would show) or never
    started (which it also would). That distinction does not exist in the committed
    data, because the winner's claim is invisible until it commits.
    """
    result = await session.execute(
        _TRY_ADVISORY_LOCK_SQL, {"lock_id": advisory_lock_id(key)}
    )
    return bool(result.scalar_one())


async def lock_account_for_update(session: AsyncSession, account_id: uuid.UUID) -> bool:
    """Take an exclusive row lock on one account. False if no such account.

    Everything that reads a balance in order to *decide* something must call this
    first, and must do so inside the transaction that will act on the decision.
    That is the entire fix for the overdraw race: the balance is read under a lock
    that no other withdrawal can hold at the same time, so no other withdrawal can
    read the same balance and reach the same conclusion.

    The lock is per account, so withdrawals against different accounts do not
    contend at all. Withdrawals against the *same* account are serialised, which is
    exactly the intent -- they are the ones that can overdraw it.
    """
    result = await session.execute(_LOCK_ACCOUNT_SQL, {"account_id": account_id})
    return result.first() is not None
