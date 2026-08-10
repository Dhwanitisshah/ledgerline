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


class PaymentStatus(StrEnum):
    """Where a payment sits in its lifecycle.

    ``REFUNDED`` is defined but unreachable: the legal-transition table in
    ``app/payments.py`` has no move into it, and no endpoint produces one. It
    exists here because the Postgres enum type has to carry every value the
    column will ever hold, and widening an enum later is a migration this project
    would rather write once. Phase 6 owns the refund flow.
    """

    CREATED = "created"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


# Both Postgres enums store the lowercase *values* ('debit'/'credit',
# 'created'/'processing'/...), not the Python member names, so the database is
# readable without knowing the Python enum.
entry_direction_enum = sa.Enum(
    EntryDirection,
    name="entry_direction",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)

payment_status_enum = sa.Enum(
    PaymentStatus,
    name="payment_status",
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
    """One charge attempt and where it got to. Phase 2 gives this table its life.

    Unlike the ledger tables, ``payments`` is **mutable on purpose**. A payment
    moves ``created -> processing -> succeeded|failed``, and those moves are
    UPDATEs. Only the money is append-only; the record of what a processor said
    about a charge is a status that legitimately changes. Every change goes
    through ``app.payments.transition``, which is the only sanctioned writer of
    this column.

    ``ledger_transaction_id`` is the join between the lifecycle and the money, and
    migration ``0003`` puts a CHECK constraint across it: a ``succeeded`` payment
    must point at a posting, and a payment in any other state must not. That is
    the atomicity guarantee of this phase written down where the database can
    enforce it rather than left to the route to remember.

    Still absent, on purpose: an idempotency key (Phase 3), any locking or version
    column (Phase 4), and any outbox linkage (Phase 5).
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("accounts.id"), nullable=False, index=True
    )
    # BIGINT minor units, same rule as the ledger. Never a float, never a Decimal.
    amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        sa.CHAR(3), nullable=False, server_default=sa.text("'INR'")
    )
    status: Mapped[PaymentStatus] = mapped_column(
        payment_status_enum,
        nullable=False,
        default=PaymentStatus.CREATED,
        server_default=sa.text("'created'"),
    )
    # The processor's handle on this attempt. Set for failures too -- a declined
    # charge has a reference, and it is what you quote when someone asks why.
    processor_ref: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # NULL until (and unless) the charge succeeds and its postings are written.
    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("ledger_transactions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        # Exactly the succeeded payments have a posting. See migration 0003 for
        # why this lives in the database rather than in the route.
        sa.CheckConstraint(
            "(status = 'succeeded') = (ledger_transaction_id IS NOT NULL)",
            name="ck_payments_posting_matches_status",
        ),
    )
