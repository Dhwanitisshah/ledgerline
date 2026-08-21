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
from sqlalchemy.dialects.postgresql import JSONB
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

    Phase 5a gives ``processing`` a second meaning it did not have before. Under
    ``CHARGE_DURABILITY=durable_intent`` this row is **committed** before the
    processor is called, so a payment sitting in ``processing`` is no longer
    necessarily a charge in flight -- it may be a charge whose request died. The
    two are indistinguishable from the row alone, which is exactly why the sweep
    in ``app/reconcile.py`` resolves them against the processor's records rather
    than by guessing from a timestamp.

    Still absent, on purpose: any column linking a payment to the outbox events it
    produced. Phase 5b emits those events and deliberately does not point back
    here -- see :class:`OutboxEvent`.
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


class IdempotencyKey(Base):
    """One claimed ``Idempotency-Key`` and the response it owes to any retry.

    The primary key *is* the idempotency key, so "who owns this key" is answered
    by the same mechanism that answers "does this row exist" -- there is no
    application-level ownership check that could disagree with the database.

    Rows are written by ``app/idempotency.py``, never by the ORM: the claim needs
    ``INSERT ... ON CONFLICT`` semantics that a session flush cannot express. This
    class exists so the table is part of ``Base.metadata`` (Alembic autogenerate,
    and a single place to read the schema) rather than to be queried through.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Which payment this key claimed, bound in the same transaction that commits
    # the payment (Phase 5a). NULL while a claim exists but no payment does yet,
    # and NULL forever on the naive claim path, which writes its key only at the
    # end. The sweep needs this: when it settles a payment whose request crashed,
    # it has to finalise that request's key too, or the customer is locked out of
    # their own charge for the full 24h TTL over a failure that was ours.
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("payments.id"), nullable=True
    )
    # NULL until the charge finishes. The CHECK constraint in migration 0004 ties
    # these two to `status`, so a 'completed' key with nothing to replay cannot be
    # stored.
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # 'in_progress' | 'completed'. In Phase 3 a committed row is always
    # 'completed', because the claim and the charge share one transaction: if the
    # charge does not commit, the claim does not either. Phase 4 is what makes
    # 'in_progress' observable, by committing the claim separately so a second
    # request can see that one is already running.
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'in_progress'")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')", name="ck_idempotency_keys_status"
        ),
        sa.CheckConstraint(
            "(status = 'completed') = "
            "(response_snapshot IS NOT NULL AND response_status IS NOT NULL)",
            name="ck_idempotency_keys_snapshot_matches_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_idempotency_keys_expiry_after_creation"
        ),
    )


class ProcessorCharge(Base):
    """**Not Ledgerline's data.** The fake processor's own books, standing in for
    a third party's database.

    Read the rest of this module as the money model; read this class as somebody
    else's system that happens to be hosted in the same Postgres because there is
    no second server in this project. The distinction is not decoration -- it is
    the entire mechanism of Phase 5a:

    * It is written by ``app/processor.py`` through **its own session, on its own
      connection, in its own transaction**, which commits independently of
      whatever Ledgerline is doing. That is what makes the crash real: when the
      charge transaction rolls back, this row stays. The card was charged.
    * Nothing else in the codebase may query it, join against it, or add a foreign
      key to it. The only sanctioned reader is ``ProcessorAdapter.lookup``, which
      is the seam a real processor's "retrieve charge by idempotency key" API
      would sit behind.

    It lives in ``Base.metadata`` for one reason only: so Alembic autogenerate
    knows the table exists and does not propose dropping it. Do not read that as
    ownership.

    ``attempt_ref`` is the primary key and is the payment id Ledgerline sends as
    the processor-side idempotency key. Charging the same ``attempt_ref`` twice
    returns the first outcome rather than charging again -- which is what a real
    processor does with an idempotency key, and what makes it safe for the sweep
    to ask about an attempt whose fate it does not know.
    """

    __tablename__ = "processor_charges"

    attempt_ref: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    processor_ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    # 'success' | 'failure', matching app.processor.ProcessorOutcome. Stored as
    # TEXT rather than sharing the payment_status enum: this is the processor's
    # vocabulary, not ours, and coupling the two types would be a lie about who
    # owns the values.
    outcome: Mapped[str] = mapped_column(sa.Text, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.CheckConstraint(
            "outcome IN ('success', 'failure')", name="ck_processor_charges_outcome"
        ),
        sa.CheckConstraint("amount > 0", name="ck_processor_charges_amount_positive"),
    )


class OutboxEvent(Base):
    """One thing that happened, written in the transaction that made it happen.

    This is the whole of the transactional outbox, and everything interesting about
    it is *where the row is written from* rather than what the row contains. A
    successful charge writes its ledger entries, moves the payment to ``succeeded``
    and inserts one of these, in a single transaction with a single commit. So:

        **the event exists if and only if the money moved.**

    Not "usually", not "unless the broker was down". The two cannot disagree
    because there is only one commit, and a commit is the smallest thing Postgres
    will do halfway through -- which is to say, not at all.

    Contrast the dual write this replaces: post the ledger entries, commit, then
    publish to a broker. Two systems, two writes, and a gap between them. Crash in
    the gap and the money moved with nobody told. Reverse the order and the broker
    hears about a charge that never committed. There is no ordering of two writes
    to two systems that is safe, which is why the second write is removed entirely
    and replaced by a row in the same database.

    Rows are written by ``app/outbox.py`` and consumed by ``app/publisher.py``,
    never through the ORM: the claim needs ``FOR UPDATE SKIP LOCKED`` semantics
    that a session flush cannot express. This class exists so the table is part of
    ``Base.metadata`` rather than to be queried through.

    **No foreign key to ``payments``**, deliberately. An event is a statement about
    something that happened, and it has to stay readable and publishable on its own
    terms -- an outbox that joins back into the live money model is one that cannot
    be drained, archived, or moved to another store without dragging the ledger
    behind it. The payment id travels *inside* ``payload``, where a consumer can
    read it, rather than as a constraint.
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=sa.text("gen_random_uuid()"),
    )
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 'pending' | 'published'. There is no 'publishing' state, and its absence is
    # the design: an in-flight publish is represented by a row lock held for the
    # length of one transaction, not by a status somebody has to clean up after a
    # worker dies mid-flight. A lock is released by the backend dying; a status is
    # not.
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    published_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # Committed attempts only; see migration 0006 and app/publisher.py.
    attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )

    __table_args__ = (
        sa.CheckConstraint("status IN ('pending', 'published')", name="ck_outbox_events_status"),
        sa.CheckConstraint(
            "(status = 'published') = (published_at IS NOT NULL)",
            name="ck_outbox_events_published_at_matches_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_positive"),
    )


class EventDelivery(Base):
    """**Not Ledgerline's data.** The downstream consumer's record of what it has
    already handled.

    Read this exactly as :class:`ProcessorCharge` asks to be read: somebody else's
    system, hosted in this Postgres because the project has one database. It is
    written by ``app/publisher.py``'s sink through **its own session and its own
    transaction**, and that independence is doing the same job it does for the
    processor's books -- when the publisher's transaction rolls back, this row
    stays, which is precisely why a redelivery can find it and decline to act twice.

    The primary key **is** the outbox event id, and that single fact is the entire
    exactly-once mechanism. The publisher promises at-least-once delivery, because
    that is the strongest promise any sender can keep across a process that may
    die. Turning that into an exactly-once *effect* is the receiver's job, and this
    is how a receiver does it: ``INSERT ... ON CONFLICT (event_id) DO NOTHING``.
    The second delivery is not detected and rejected by application logic; it
    collides with a unique index and does nothing.

    A real consumer would keep this table in its own database, and nothing about
    the argument changes -- which is the point. No foreign key to ``outbox_events``
    for exactly that reason: a consumer in another process could not declare one.
    """

    __tablename__ = "event_deliveries"

    event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class WebhookEvent(Base):
    """One processor callback, recorded so the next copy of it does nothing.

    Providers deliver webhooks **at least once**. Not "occasionally twice under
    load" -- at least once is the contract, because the provider cannot know
    whether a response it never received meant the event was handled. A receiver
    that assumes single delivery is a receiver that will eventually settle one
    payment twice.

    The primary key is the *provider's* event id, stored as TEXT because that is
    what providers send. Deduplication is therefore the same mechanism as
    everywhere else in this project: a unique index, not an application check that
    could be raced.

    There is no ``in_progress`` status here, unlike :class:`IdempotencyKey`, and
    the difference is worth understanding rather than smoothing over. This row and
    the work it describes are written in **one** transaction: if the settlement
    does not commit, neither does this row, so a redelivery correctly re-processes
    an event whose handling was lost. That is exactly the property Phase 3's
    idempotency claim had before Phase 5a had to give it up -- and this table gets
    to keep it because, unlike a charge, handling a webhook never calls out to a
    third party mid-transaction.

    ``payment_id`` carries **no foreign key**; see migration 0006. The id in a
    webhook is a third party's claim about what exists, not a reference this
    service controls.
    """

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    # What the handler did, written before the same transaction commits. A row
    # visible to anyone else has always been processed.
    outcome: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'received'")
    )
    received_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
