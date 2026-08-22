"""The transition helper, tested directly and without a database.

These are the only tests in the suite that need no Postgres, which is the point:
the legality of a state change is decided by the transition table in
``app/payments.py`` and by nothing else. If this logic were spread across the
charge route, testing it would require standing up a charge to reach each state.
"""

import pytest

from app.models import Payment, PaymentStatus
from app.payments import ALLOWED_TRANSITIONS, IllegalTransitionError, can_transition, transition


def make_payment(status: PaymentStatus) -> Payment:
    """A detached Payment parked in ``status``. Never added to a session."""
    return Payment(amount=250000, currency="INR", status=status)


def test_transition_map_covers_every_status() -> None:
    """A new enum member must not silently become a state with undefined rules.

    Without this, adding a status to the enum and forgetting the transition table
    would make ``transition()`` raise KeyError -- a crash that looks like a bug in
    the caller rather than an unfinished state machine.
    """
    assert set(ALLOWED_TRANSITIONS) == set(PaymentStatus)


def test_the_happy_path_is_legal() -> None:
    payment = make_payment(PaymentStatus.CREATED)

    transition(payment, PaymentStatus.PROCESSING)
    assert payment.status is PaymentStatus.PROCESSING

    transition(payment, PaymentStatus.SUCCEEDED)
    assert payment.status is PaymentStatus.SUCCEEDED


def test_the_failure_path_is_legal() -> None:
    payment = make_payment(PaymentStatus.CREATED)

    transition(payment, PaymentStatus.PROCESSING)
    transition(payment, PaymentStatus.FAILED)
    assert payment.status is PaymentStatus.FAILED


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        # A settled payment does not go back to work.
        (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING),
        # The one that actually costs money if it ever succeeds silently.
        (PaymentStatus.FAILED, PaymentStatus.SUCCEEDED),
        (PaymentStatus.SUCCEEDED, PaymentStatus.FAILED),
        # Skipping 'processing' would mean a payment that succeeded without any
        # record of a processor ever having been called.
        (PaymentStatus.CREATED, PaymentStatus.SUCCEEDED),
        (PaymentStatus.CREATED, PaymentStatus.FAILED),
        # Retrying a failure is a new payment, not a rewind of the old one.
        (PaymentStatus.FAILED, PaymentStatus.PROCESSING),
        # Self-transitions are moves too, and none of them are legal.
        (PaymentStatus.PROCESSING, PaymentStatus.PROCESSING),
        (PaymentStatus.SUCCEEDED, PaymentStatus.SUCCEEDED),
    ],
)
def test_illegal_transitions_raise(current: PaymentStatus, requested: PaymentStatus) -> None:
    payment = make_payment(current)

    with pytest.raises(IllegalTransitionError) as excinfo:
        transition(payment, requested)

    assert excinfo.value.current is current
    assert excinfo.value.requested is requested


def test_an_illegal_transition_leaves_the_payment_untouched() -> None:
    """Raising is only half of it -- the row must not be half-moved on the way out."""
    payment = make_payment(PaymentStatus.SUCCEEDED)
    payment.updated_at = None

    with pytest.raises(IllegalTransitionError):
        transition(payment, PaymentStatus.PROCESSING)

    assert payment.status is PaymentStatus.SUCCEEDED
    # The timestamp is stamped by transition(); an untouched payment has no new one.
    assert payment.updated_at is None


def test_terminal_states_have_no_way_out() -> None:
    """Phase 6 removed SUCCEEDED from this list, and only SUCCEEDED.

    A failed charge is not retried in place -- a retry is a new payment. A fully
    refunded one does not move either: reversing a reversal is a new charge, not a
    state change.
    """
    for terminal in (PaymentStatus.FAILED, PaymentStatus.REFUNDED):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_refunded_is_reachable_from_succeeded_and_from_nowhere_else() -> None:
    """The Phase 6 edge, and the only one.

    This test is the inverse of the one it replaced. Through Phases 2-5 it asserted
    that nothing transitioned into 'refunded', so that shipping a refund flow
    without updating the table would fail loudly. Now it asserts the edge exists and
    that exactly one state has it -- so that a later phase cannot quietly make a
    failed or still-processing payment refundable by widening the table instead of
    thinking about it.
    """
    assert PaymentStatus.REFUNDED in ALLOWED_TRANSITIONS[PaymentStatus.SUCCEEDED]

    sources = {
        source
        for source, allowed in ALLOWED_TRANSITIONS.items()
        if PaymentStatus.REFUNDED in allowed
    }
    assert sources == {PaymentStatus.SUCCEEDED}


def test_a_failed_payment_cannot_be_refunded() -> None:
    """There was never any money to send back, and the graph is where that lives.

    The refund route rejects this before the state machine is reached, so the caller
    gets a sentence rather than an exception -- but the rule itself is here, in the
    empty transition set, rather than duplicated as a condition in a route.
    """
    payment = make_payment(PaymentStatus.FAILED)
    with pytest.raises(IllegalTransitionError):
        transition(payment, PaymentStatus.REFUNDED)
    assert payment.status is PaymentStatus.FAILED


def test_can_transition_agrees_with_transition() -> None:
    for current in PaymentStatus:
        for requested in PaymentStatus:
            payment = make_payment(current)
            if can_transition(current, requested):
                transition(payment, requested)
                assert payment.status is requested
            else:
                with pytest.raises(IllegalTransitionError):
                    transition(payment, requested)


def test_transition_stamps_updated_at() -> None:
    payment = make_payment(PaymentStatus.CREATED)
    payment.updated_at = None

    transition(payment, PaymentStatus.PROCESSING)

    assert payment.updated_at is not None
    assert payment.updated_at.tzinfo is not None
