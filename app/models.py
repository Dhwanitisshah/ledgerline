"""SQLAlchemy models for the Ledgerline money model (Phase 1).

Money rules that apply to every table in this module:

* Amounts are BIGINT **minor units** (paise for INR, cents for USD). There is no
  float and no Decimal anywhere in the stack -- not in the column type, not in
  Python, not on the wire.
* There is deliberately **no balance column** on ``accounts``. A balance is always
  derived with a SQL SUM over ``ledger_entries``; see ``app/ledger.py``.
* ``ledger_transactions`` and ``ledger_entries`` are append-only. Postgres
  triggers installed by migration ``0002`` reject UPDATE and DELETE outright, so
  immutability is enforced by the database rather than by convention.
"""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EntryDirection(StrEnum):
    """The two sides of a double-entry posting."""

    DEBIT = "debit"
    CREDIT = "credit"


# The Postgres enum stores the lowercase *values* ('debit'/'credit'), not the
# Python member names, so the DB is readable without knowing the Python enum.
entry_direction_enum = sa.Enum(
    EntryDirection,
    name="entry_direction",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class Account(Base):
    """An account money can be posted against.

    Note what is *absent*: there is no ``balance`` column. Storing a balance means
    keeping a running total in sync with the entries that justify it, and the two
    drift the moment anything goes wrong. Ledgerline derives the balance instead.
    """

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    currency: Mapped[str] = mapped_column(
        sa.CHAR(3), nullable=False, server_default=sa.text("'INR'")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class LedgerTransaction(Base):
    """A group of ledger entries that must balance to zero as a unit.

    Append-only: guarded by the ``ledger_transactions_immutable`` trigger.
    """

    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", cascade="all, save-update"
    )


class LedgerEntry(Base):
    """One side of one posting: an amount, a direction, and an account.

    ``amount`` is always positive; the sign lives in ``direction``. This keeps the
    CHECK constraint simple (``amount > 0``) and makes an accidental negative
    credit impossible to represent.

    Append-only: guarded by the ``ledger_entries_immutable`` trigger.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("ledger_transactions.id"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("accounts.id"), nullable=False, index=True
    )
    direction: Mapped[EntryDirection] = mapped_column(entry_direction_enum, nullable=False)
    # BIGINT minor units. Never a float, never a Decimal.
    amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    transaction: Mapped[LedgerTransaction] = relationship(back_populates="entries")

    __table_args__ = (sa.CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),)


class Payment(Base):
    """Placeholder so the schema is complete. Phase 2 owns the payment lifecycle.

    Deliberately empty of behaviour: no status machine, no processor reference, no
    idempotency key, no transitions. Nothing in Phase 1 reads or writes this table.
    Do not add payment logic here -- it belongs to Phase 2 (the charge flow) and
    Phase 3 (idempotency).
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("accounts.id"), nullable=False
    )
    amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'created'")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
