"""The broken and the fixed implementations, selectable at runtime.

Phase 4 is a before/after, and a before/after is worth nothing if the "before" is
a paragraph of prose. Both versions of each race are real, runnable code, chosen
by configuration:

* ``IDEMPOTENCY_CLAIM_STRATEGY`` -- how ``POST /charges`` claims its key
* ``WITHDRAWAL_GUARD`` -- how ``POST /withdrawals`` guards the balance
* ``CHARGE_DURABILITY`` -- when ``POST /charges`` commits the attempt (Phase 5a)

The naive settings are **not defaults and are not production paths**. They exist
so the concurrency harness can demonstrate the bug on demand, in the same process
and against the same database as the fix, and so the numbers in the README are
measurements rather than claims. Deleting them would make the fix unfalsifiable.
"""

from enum import StrEnum


class ClaimStrategy(StrEnum):
    """How a charge takes ownership of its idempotency key."""

    #: Read the key, and if it is absent, charge and write the key afterwards.
    #: This is the version most people write, and it double-charges under
    #: concurrency: two requests both read "absent" before either writes. See
    #: app/idempotency.py::claim_key_naive.
    NAIVE = "naive"

    #: Claim with INSERT ... ON CONFLICT, gated by a non-blocking advisory lock.
    #: The shipped path.
    LOCKED = "locked"


class WithdrawalGuard(StrEnum):
    """How a withdrawal stops itself from overdrawing an account."""

    #: Read the balance, decide, then write. Two concurrent withdrawals both read
    #: the same balance, both decide there is enough, and both write.
    NAIVE = "naive"

    #: Take SELECT ... FOR UPDATE on the account row before reading the balance,
    #: so the read and the write are one atomic unit. The shipped path.
    ROW_LOCK = "row_lock"


class ChargeDurability(StrEnum):
    """When a charge attempt becomes durable, relative to the processor call.

    This is the Phase 5a fork, and unlike the two above it is not about
    concurrency at all. It is about what survives a process death.
    """

    #: Phases 2-4. Claim, payment, processor call, posting and commit are one
    #: transaction. Nothing about the attempt exists in committed data until the
    #: whole thing succeeds, so a crash after the processor said yes leaves the
    #: card charged and Ledgerline with **no row to find**. Preserved runnable so
    #: the gap can be reproduced rather than described.
    SINGLE_TXN = "single_txn"

    #: Phase 5a. The attempt is committed as 'processing' *before* the processor
    #: is called, so a crash afterwards leaves a durable record that the sweep in
    #: app/reconcile.py can find and settle against the processor's own books.
    #: The shipped path.
    DURABLE_INTENT = "durable_intent"
