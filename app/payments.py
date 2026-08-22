"""The payment lifecycle state machine.

A payment's status is not a string that code assigns. It is a position in a graph
with a fixed set of legal moves::

    created ──▶ processing ──┬──▶ succeeded ──▶ refunded
                             └──▶ failed

Every other move is illegal, and illegal means :class:`IllegalTransitionError` --
never a quietly rewritten row. That distinction is the reason this module exists
at all: ``payment.status = "succeeded"`` is one keystroke, can be written from
anywhere, and looks entirely reasonable at a glance. Routing every change through
:func:`transition` makes the graph the only way to move.

## What Phase 6 added, and what it deliberately did not

``succeeded -> refunded`` is now legal, and that is the *only* new edge. Read the
graph carefully, because the interesting content is in what is missing from it:

* **There is no ``partially_refunded`` state.** A payment with some of its money
  returned is still partly live, so it stays ``succeeded``. Only a refund that
  brings the total to the full charge moves it to ``refunded``. The alternative --
  a status per degree of refundedness -- would encode an *amount* in a state
  machine, and the amount already has a home: a SUM over the ``refunds`` table.
  This is the same decision as having no ``balance`` column, applied a third time.
* **``refunded`` is terminal.** A fully refunded payment does not move again. There
  is no un-refund, because reversing a reversal is a new charge and not a state
  change.
* **``failed`` is still terminal**, so a failed payment cannot be refunded -- which
  falls out of the graph rather than needing a rule of its own. There was never
  any money to send back.

The practical effect: a partial refund does not call :func:`transition` at all. It
writes a ``refunds`` row and a reversing posting, and leaves this column exactly as
it found it. Only the final one transitions.
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
        # Phase 6. A settled charge can be sent back -- but only when the refunds
        # total the WHOLE amount; a partial refund leaves the payment here. The
        # decision of when to make this move lives in app/refunds.py, because it is
        # arithmetic against the refunds table rather than a property of the graph.
        PaymentStatus.SUCCEEDED: frozenset({PaymentStatus.REFUNDED}),
        # Terminal. A retry of a failed charge is a new payment, not a resurrection
        # of the old one -- and a failed charge cannot be refunded, which falls out
        # of this empty set rather than needing a rule written somewhere else.
        PaymentStatus.FAILED: frozenset(),
        # Terminal. Reversing a reversal is a new charge, not a state change.
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
