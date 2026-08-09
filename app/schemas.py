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

from app.models import EntryDirection


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
