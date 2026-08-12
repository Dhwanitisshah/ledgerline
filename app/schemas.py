"""Request/response models for the API boundary.

Amounts cross the wire as **integer minor units** and nothing else. ``StrictInt``
is doing real work here: without it pydantic would happily coerce the JSON value
``1000.0`` into the integer ``1000``, which is exactly the kind of quiet
float-to-money conversion this project exists to prevent. ``1000.0`` and
``"1000"`` are rejected with a 422; only ``1000`` is money.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.models import EntryDirection, PaymentStatus
from app.processor import ProcessorOutcome


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
