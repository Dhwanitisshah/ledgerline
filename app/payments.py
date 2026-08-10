"""The payment lifecycle state machine.

A payment's status is not a string that code assigns. It is a position in a graph
with a fixed set of legal moves::

    created ──▶ processing ──┬──▶ succeeded
                             └──▶ failed

Every other move is illegal, and illegal means :class:`IllegalTransitionError` --
never a quietly rewritten row. That distinction is the reason this module exists
at all: ``payment.status = "succeeded"`` is one keystroke, can be written from
anywhere, and looks entirely reasonable at a glance. Routing every change through
:func:`transition` makes the graph the only way to move.

``refunded`` appears in the enum because the column type has to know about it
before Phase 6 can use it, but nothing transitions into it yet. Its row in
:data:`ALLOWED_TRANSITIONS` is deliberately unreachable, and there is a test that
says so, so "we forgot to wire up refunds" cannot pass for "refunds work".
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType

from app.models import Payment, PaymentStatus

# The transition table. This is the specification, not a helper -- if a move is
# not written here it does not exist. MappingProxyType because a mutable module
# global holding the rules of a state machine is an invitation.
ALLOWED_TRANSITIONS: Mapping[PaymentStatus, frozenset[PaymentStatus]] = MappingProxyType(
    {
        PaymentStatus.CREATED: frozenset({PaymentStatus.PROCESSING}),
        PaymentStatus.PROCESSING: frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}),
        # Terminal states. Phase 6 will add SUCCEEDED -> REFUNDED; until then a
        # settled payment does not move, and neither does a failed one. Note that
        # this makes retrying a failed charge impossible by design: a retry is a
        # new payment, not a resurrection of the old one.
        PaymentStatus.SUCCEEDED: frozenset(),
        PaymentStatus.FAILED: frozenset(),
        PaymentStatus.REFUNDED: frozenset(),
    }
)


class IllegalTransitionError(Exception):
    """Raised when a caller asks for a move the graph does not contain."""

    def __init__(self, current: PaymentStatus, requested: PaymentStatus) -> None:
        self.current = current
        self.requested = requested

        legal = sorted(ALLOWED_TRANSITIONS[current])
        permitted = ", ".join(legal) if legal else "nothing -- it is a terminal state"
        super().__init__(
            f"illegal payment transition {current} -> {requested}; "
            f"from {current} the legal moves are: {permitted}"
        )


def can_transition(current: PaymentStatus, requested: PaymentStatus) -> bool:
    """True when ``current -> requested`` is a legal move."""
    return requested in ALLOWED_TRANSITIONS[current]


def transition(payment: Payment, new_status: PaymentStatus) -> Payment:
    """Move ``payment`` to ``new_status``, or raise and leave it exactly as it was.

    The validity check runs *before* the assignment, and that ordering is the
    property worth being deliberate about. A rejected transition must not leave
    the object half-moved: a caller that catches the error still holds a payment
    in precisely the state it had before it asked. "Raises but mutated anyway" is
    the worst of both designs.
    """
    current = PaymentStatus(payment.status)
    if new_status not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(current, new_status)

    payment.status = new_status
    # Stamped here rather than by a database trigger because this function is the
    # only thing in the codebase that changes a payment's status. A second
    # mechanism would only be a second thing to keep in step.
    payment.updated_at = datetime.now(UTC)
    return payment
