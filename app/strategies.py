"""The broken and the fixed implementations, selectable at runtime.

Phase 4 is a before/after, and a before/after is worth nothing if the "before" is
a paragraph of prose. Both versions of each race are real, runnable code, chosen
by configuration:

* ``IDEMPOTENCY_CLAIM_STRATEGY`` -- how ``POST /charges`` claims its key
* ``WITHDRAWAL_GUARD`` -- how ``POST /withdrawals`` guards the balance

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
