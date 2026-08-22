"""Request/response models for the API boundary.

Amounts cross the wire as **integer minor units** and nothing else. ``StrictInt``
is doing real work here: without it pydantic would happily coerce the JSON value
``1000.0`` into the integer ``1000``, which is exactly the kind of quiet
float-to-money conversion this project exists to prevent. ``1000.0`` and
``"1000"`` are rejected with a 422; only ``1000`` is money.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.models import EntryDirection, PaymentStatus
from app.processor import ProcessorOutcome
from app.webhooks import MAX_EVENT_ID_LENGTH


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    currency: str
    created_at: datetime


class BalanceOut(BaseModel):
    account_id: uuid.UUID
    currency: str
    # Derived from a SQL SUM over ledger_entries -- never read from a column.
    balance: int


class EntryIn(BaseModel):
    account_id: uuid.UUID
    direction: EntryDirection
    # Positive minor units; the sign of the posting lives in `direction`.
    amount: StrictInt = Field(gt=0)


class TransactionCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    # An empty posting cannot balance in any meaningful sense, so reject it here
    # rather than letting a no-op through. Single-sided postings get rejected a
    # layer down by the sum-to-zero invariant.
    entries: list[EntryIn] = Field(min_length=1)


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    direction: EntryDirection
    amount: int


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str
    created_at: datetime
    entries: list[EntryOut]


class ChargeCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "account_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "amount": 250000,
                    "currency": "INR",
                }
            ]
        }
    )

    account_id: uuid.UUID
    # Minor units, StrictInt for the same reason as EntryIn.amount: 2500.0 is not
    # money, and coercing it into 2500 is how rounding bugs are born.
    amount: StrictInt = Field(gt=0)
    # Optional. Omitted means "whatever currency the account is in"; supplied means
    # "and I assert it is this", which is checked rather than trusted.
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    # --- Test affordances -------------------------------------------------------
    # These override the configured fake processor for this one request. They exist
    # so a smoke script or a test can produce a real processor failure on demand
    # without restarting the server under different environment variables, which is
    # the only way to exercise the guarantee this phase is about. They are
    # unmistakably named, and they are the first thing to delete the day a real
    # processor adapter lands.
    force_outcome: ProcessorOutcome | None = None
    force_latency_ms: StrictInt | None = Field(default=None, ge=0, le=30_000)
    # Phase 5a. Abandons the request immediately after the processor answers and
    # before anything is committed on the settlement side -- the exact instant the
    # Phase 2 docstring named as unrecoverable. It is a raised exception rather
    # than an os._exit(), and from Postgres's point of view those are the same
    # event: the transaction is abandoned without committing. What it cannot
    # simulate is the process not coming back, which is why the sweep is a separate
    # process and not a `finally:` block.
    force_crash_after_processor: bool = False


class WithdrawalCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"account_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "amount": 50000}
            ]
        }
    )

    account_id: uuid.UUID
    # Minor units, StrictInt for the same reason as everywhere else in this file.
    amount: StrictInt = Field(gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class WithdrawalOut(BaseModel):
    ledger_transaction_id: uuid.UUID
    account_id: uuid.UUID
    amount: int
    currency: str
    # The balance this withdrawal left behind. Under the row-lock guard this is
    # authoritative at the moment of commit; it is returned mainly so a caller (or
    # a concurrency harness) can see the serialisation actually happening.
    balance_after: int


class WebhookEventType(StrEnum):
    """The processor callbacks this receiver understands.

    Typed as an enum so an unrecognised event type is a 422 at the door rather than
    a silently ignored body. That is the conservative direction: a provider sending
    us something new should be a loud failure we notice and add support for, not a
    200 that quietly discards it while telling them it was handled.

    Note that neither value decides anything. See app/routers/webhooks.py -- the
    type is recorded, and the processor's books are what settle the payment.
    """

    CHARGE_SUCCEEDED = "charge.succeeded"
    CHARGE_FAILED = "charge.failed"


class WebhookData(BaseModel):
    """The event's subject: which charge attempt it is about.

    ``attempt_ref`` is the processor-side idempotency key Ledgerline sent with the
    charge, which is the payment id. Typed as a UUID so a malformed reference is a
    422 rather than a lookup that mysteriously finds nothing.
    """

    attempt_ref: uuid.UUID


class WebhookIn(BaseModel):
    """One processor callback, in the shape providers actually send.

    ``id`` is the *provider's* event id and is a string, not a UUID: real providers
    send things like ``evt_1a2b3c``. It is the deduplication key, so the only thing
    that genuinely matters about it is that the provider repeats it verbatim on a
    redelivery -- which is exactly what makes redelivery detectable.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "evt_1a2b3c4d5e6f",
                    "type": "charge.succeeded",
                    "data": {"attempt_ref": "3fa85f64-5717-4562-b3fc-2c963f66afa6"},
                }
            ]
        }
    )

    id: str = Field(min_length=1, max_length=MAX_EVENT_ID_LENGTH)
    type: WebhookEventType
    data: WebhookData


class WebhookOut(BaseModel):
    event_id: str
    # False on the delivery that did the work, True on every copy after it. The
    # provider does not care; an operator reading logs after an incident does, and
    # it is what the tests and the smoke script assert to show the second delivery
    # was a no-op.
    duplicate: bool
    # What handling the event did, as an app.reconcile.ReconcileOutcome value. A
    # duplicate quotes the *original* delivery's outcome rather than recomputing
    # one, so replaying an event gives a consistent answer every time.
    outcome: str


class RefundCreate(BaseModel):
    """The body of ``POST /charges/{id}/refund``.

    ``amount`` is optional, and omitting it means **refund whatever is left** --
    the whole charge for an untouched payment, the remainder for a partly refunded
    one. That default is deliberate: refunding in full is the common case, and a
    caller who does not have to compute an amount cannot compute it wrongly.

    There is no ``currency`` field. A refund is denominated in the charge's
    currency by definition, and offering the caller somewhere to type a different
    one would only create a mismatch to validate. Phase 2 accepts a currency on a
    charge because the caller is asserting something checkable about an account;
    here there is nothing to assert.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                # A partial refund. The payment stays 'succeeded'.
                {"amount": 100000},
                # And the full-refund form: no body at all means "whatever is left".
                {},
            ]
        }
    )

    # Minor units, StrictInt for the same reason as everywhere else in this file:
    # 2500.0 is not money, and coercing it into 2500 is how rounding bugs are born.
    amount: StrictInt | None = Field(default=None, gt=0)

    # --- Test affordance ----------------------------------------------------------
    # Forces the fake processor to decline this reversal, exactly as `force_outcome`
    # does for a charge. Real processors refuse refunds -- a charge too old, a closed
    # card, an open dispute -- and a flow that cannot be shown handling a "no" has
    # only ever been tested on the happy path.
    force_outcome: ProcessorOutcome | None = None


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payment_id: uuid.UUID
    amount: int
    currency: str
    # 'succeeded' | 'failed' -- the refund's own outcome, not the payment's.
    status: str
    processor_ref: str | None
    failure_reason: str | None
    # The reversing posting. NULL unless the refund succeeded; this is the field
    # that says whether any money actually went back.
    ledger_transaction_id: uuid.UUID | None
    created_at: datetime

    # --- Where the payment now stands ---------------------------------------------
    # Derived, never stored: `total_refunded` is a SUM over the refunds table and
    # `payment_status` comes off the payment. They are returned together because a
    # caller that has just refunded almost always wants to know whether anything is
    # left, and making them issue a second request to find out would invite them to
    # cache the answer instead.
    total_refunded: int
    remaining_refundable: int
    payment_status: PaymentStatus


class ChargeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    amount: int
    currency: str
    status: PaymentStatus
    processor_ref: str | None
    failure_reason: str | None
    # NULL unless the charge succeeded. This is the field that says whether any
    # money moved, and on the failure path it is the assertion that matters.
    ledger_transaction_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
