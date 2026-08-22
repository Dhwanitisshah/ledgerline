"""Refund arithmetic: how much has come back, and how much still can.

This module owns the three questions a refund has to answer before any money
moves, and it answers all of them **against the database inside the caller's
transaction** rather than against Python state:

1. How much of this payment has already been refunded? (:func:`refunded_total`)
2. Is this payment refundable at all, and is this amount allowed?
   (:func:`assess_refund`)
3. Does this refund complete the charge, so the payment should move to
   ``refunded``? (:attr:`RefundAssessment.is_final`)

## The invariant

    **The succeeded refunds for one payment never total more than it was charged.**

Enforced in three places, which is not belt-and-braces so much as three mechanisms
that fail differently:

* here, inside the transaction, so the route can return a clean 4xx with a message
  that says how much room is left;
* by ``lock_payment_for_refund`` in app/locking.py, taken **before** the sum is
  read, so two simultaneous refunds cannot both see a total that omits the other;
* by the ``refunds_never_exceed_the_charge`` trigger from migration 0007, which
  takes the same lock itself and repeats the same arithmetic at the database.

Only the third survives a caller that is psql, or a future endpoint that forgets
the second. That is why it exists despite looking redundant, and it is the reason
this invariant is genuinely *unstorable* rather than merely well-guarded.

## Why the ordering of the lock matters, again

This is the Phase 4 overdraw race wearing different clothes, and it is worth
seeing that they are the same shape::

    withdrawal:  read balance   -> decide -> write entry    (overdraws)
    refund:      read refunded  -> decide -> write refund   (over-refunds)

Both read a total, apply a rule to it, and write something that changes the total.
Both are correct in isolation and wrong under concurrency, because the value they
read stops being true the instant another transaction commits. The fix is the same
fix: take the lock before the read, not after the decision.

## The attempt reference is derived, not generated

:func:`refund_attempt_ref` is ``uuid5`` over the payment id and the caller's
``Idempotency-Key``. That is a small function with a large consequence, and it is
the reason a refund does **not** need the two-transaction split Phase 5a built for
charges.

The charge needed that split because its attempt reference existed only in a
Python variable until the payment row committed. A crash in the gap destroyed the
reference along with everything else, so the processor could no longer be asked
about the attempt -- hence: commit the intent first, then call the processor, then
reconcile whatever was stranded.

A refund's reference is **recomputable from the retry itself**. The client resends
the same ``Idempotency-Key`` against the same payment, this function returns the
same uuid, the processor recognises it as one it has already reversed, and returns
the original outcome instead of sending the money twice. The reference survives the
crash without ever having been stored, so there is nothing to make durable in
advance.

What this does not cover, stated plainly: a client that retries a refund with a
*different* key gets a different reference and a second reversal. That is the same
contract charges have -- one key per logical operation -- and it is the client's
half of it. The drift job in app/drift.py is what notices when the two sides stop
agreeing.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, PaymentStatus

#: Namespace for derived refund attempt references. A fixed uuid rather than one
#: of the standard namespaces so that the derivation is unambiguous and stable
#: across processes, machines and restarts -- which is the entire property being
#: relied on. Changing this constant would silently make every in-flight retry a
#: new reversal, so it does not change.
REFUND_ATTEMPT_NAMESPACE = uuid.UUID("6f2b1e2a-9c47-5d38-a0b1-0e7c5f3d9a41")

# Only succeeded refunds count. A failed refund is a record that the processor
# declined an attempt; it reserved nothing and returned nothing, and summing it
# would refuse a customer money they are owed.
_REFUNDED_TOTAL_SQL = text(
    """
    SELECT COALESCE(SUM(amount), 0)::bigint AS refunded
    FROM refunds
    WHERE payment_id = :payment_id AND status = 'succeeded'
    """
)


class RefundNotAllowedError(Exception):
    """A refund this service must refuse, with a reason fit to return to a caller.

    Carries an HTTP status alongside the message because the distinction between
    "this payment cannot be refunded at all" and "this amount is too much" is a
    distinction the caller can act on, and collapsing both into a bare 400 would
    throw that away. The route maps it straight through; nothing here imports
    FastAPI.
    """

    def __init__(self, detail: str, *, status_code: int) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RefundAssessment:
    """What a proposed refund would do, decided before anything is written."""

    #: Minor units this refund would return.
    amount: int
    #: Minor units already returned by earlier succeeded refunds.
    already_refunded: int
    #: What the payment was charged.
    charged: int

    @property
    def total_after(self) -> int:
        return self.already_refunded + self.amount

    @property
    def is_final(self) -> bool:
        """True when this refund returns the last of the charge.

        The whole partial-versus-full decision reduces to this one boolean. When it
        is true the payment transitions to ``refunded``; when it is false the
        payment is left exactly as it was, because it is still partly live.

        It is ``==`` rather than ``>=`` deliberately: the over-refund check has
        already rejected anything greater, so a total above the charge is not a
        case to be tolerated here but a bug that should be impossible -- and if the
        invariant were ever breached, this reading it as "final" would quietly
        paper over it.
        """
        return self.total_after == self.charged

    @property
    def remaining_after(self) -> int:
        return self.charged - self.total_after


async def refunded_total(session: AsyncSession, payment_id: uuid.UUID) -> int:
    """Minor units already refunded against this payment.

    Derived with a SUM, never read from a column -- there is no
    ``payments.refunded_amount`` and there is deliberately nowhere to put one. This
    is the same argument as ``account_balance``: a stored total is a cached number
    that has to be kept in step with the rows that justify it, and the two drift the
    moment anything goes wrong.

    ``SUM()`` over ``bigint`` returns ``numeric`` in Postgres, which asyncpg hands
    back as a ``Decimal``, so the SQL casts to ``::bigint`` and this returns a plain
    ``int``. Money does not become a Decimal on its way out of the database.
    """
    result = await session.execute(_REFUNDED_TOTAL_SQL, {"payment_id": payment_id})
    return int(result.scalar_one())


def refund_attempt_ref(payment_id: uuid.UUID, idempotency_key: str) -> uuid.UUID:
    """The processor-side reference for this refund. Derived, so it survives a crash.

    See the module docstring. Two calls with the same payment and the same key
    return the same uuid, in this process or any other, today or after a restart --
    which is what turns a retried refund into a replay on the processor's side
    rather than a second reversal.
    """
    return uuid.uuid5(REFUND_ATTEMPT_NAMESPACE, f"{payment_id}:{idempotency_key}")


async def assess_refund(
    session: AsyncSession, payment: Payment, requested_amount: int | None
) -> RefundAssessment:
    """Decide whether this refund may happen, and what it would mean.

    **Call this with the payment row already locked.** Everything it reads is a
    total that another transaction can change, so an assessment made without the
    lock is a statement about the past. ``lock_payment_for_refund`` is that lock;
    the route takes it first and the migration 0007 trigger takes it again.

    ``requested_amount`` of ``None`` means "the rest of it" -- a full refund of
    whatever remains, which for an untouched payment is the entire charge and for a
    partly refunded one is the remainder. That is friendlier than it looks: the
    common case, refunding a charge in full, needs no amount and therefore cannot
    get it wrong.

    Raises :class:`RefundNotAllowedError` and writes nothing.
    """
    status = PaymentStatus(payment.status)

    # A payment that never moved money has nothing to send back. This covers
    # 'failed', 'created' and 'processing' in one test, and reads as the reason
    # rather than as a list of statuses to keep in step with the state machine.
    if status is PaymentStatus.REFUNDED:
        raise RefundNotAllowedError(
            f"payment {payment.id} has already been refunded in full",
            status_code=409,
        )
    if status is not PaymentStatus.SUCCEEDED:
        raise RefundNotAllowedError(
            f"payment {payment.id} is {status} and cannot be refunded; only a "
            "succeeded charge has money to return",
            status_code=409,
        )

    already = await refunded_total(session, payment.id)
    remaining = payment.amount - already

    if remaining <= 0:  # pragma: no cover - the status check above gets here first
        # Unreachable through the API: a payment refunded to its limit is 'refunded'
        # and was rejected above. Kept because "the status says succeeded but the
        # arithmetic says nothing is left" is a real inconsistency, and discovering
        # it by writing a refund for zero would be worse than saying so.
        raise RefundNotAllowedError(
            f"payment {payment.id} has no refundable amount remaining",
            status_code=409,
        )

    amount = remaining if requested_amount is None else requested_amount

    if amount > remaining:
        raise RefundNotAllowedError(
            f"refund of {amount} exceeds the {remaining} still refundable on "
            f"payment {payment.id} ({payment.amount} charged, {already} already "
            "refunded)",
            status_code=422,
        )

    return RefundAssessment(
        amount=amount, already_refunded=already, charged=payment.amount
    )
