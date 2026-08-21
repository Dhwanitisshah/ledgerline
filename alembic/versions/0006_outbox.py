"""outbox: events that commit with the money, and events that arrive twice

Phase 5b is about the two directions of "telling somebody else what happened",
and each one gets a table plus the table that makes it idempotent:

* ``outbox_events`` -- **outbound**. A row written in the same transaction as the
  ledger postings, so "the money moved" and "an event says the money moved" are
  one commit rather than two writes that can disagree. Nothing here is published
  by the request that writes it; a separate worker does that afterwards.
* ``event_deliveries`` -- **not Ledgerline's data**, in the same sense as
  ``processor_charges`` in migration 0005. It stands in for whatever the
  downstream consumer keeps, and it is what turns at-least-once delivery into an
  exactly-once *effect*: the publisher may deliver an event twice, and the second
  delivery collides with this table's primary key and does nothing.
* ``webhook_events`` -- **inbound**. Processor callbacks, deduplicated by the
  provider's own event id. Providers deliver at least once and make no promise
  about how many times; the primary key is what makes a redelivery a no-op.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12

"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- The outbox --------------------------------------------------------------
    # The entire point of this table is *where it is written from*: inside the same
    # transaction as the ledger entries and the payment update. It is therefore
    # deliberately plain -- no foreign key to payments, no trigger, no cleverness.
    # A queue table earns its keep by being boring; the guarantee comes from the
    # transaction it is written in, not from anything declared here.
    op.create_table(
        "outbox_events",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Counts attempts that reached a COMMIT -- a successful publish, or a
        # delivery that raised and was recorded as having been tried. An attempt
        # lost to the process dying increments nothing, because its transaction is
        # rolled back by the same death. See app/publisher.py; that undercount is
        # the honest consequence of the arrangement that makes redelivery correct.
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "status IN ('pending', 'published')", name="ck_outbox_events_status"
        ),
        # Exactly the published rows carry a publication timestamp. The same shape
        # of constraint as ck_payments_posting_matches_status in migration 0003, and
        # for the same reason: the invariant the worker maintains is stated where
        # the database can refuse to break it, rather than left to the worker to
        # remember.
        sa.CheckConstraint(
            "(status = 'published') = (published_at IS NOT NULL)",
            name="ck_outbox_events_published_at_matches_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_positive"),
    )

    # The publisher's only query is "the oldest pending event". A partial index over
    # exactly that predicate keeps the scan proportional to the size of the *backlog*
    # rather than to the number of events ever emitted -- the difference between a
    # worker that stays fast forever and one that gets slower every day the service
    # succeeds. It is also, deliberately, the same trick as
    # ix_payments_processing_created_at in migration 0005.
    op.execute(
        "CREATE INDEX ix_outbox_events_pending ON outbox_events (created_at) "
        "WHERE status = 'pending'"
    )

    # --- The consumer's delivery log ---------------------------------------------
    # Read this table the way migration 0005 asks you to read processor_charges: it
    # is somebody else's, hosted here because the project has one database. It is
    # the downstream consumer's record of what it has already handled.
    #
    # No foreign key to outbox_events, and that absence is load-bearing rather than
    # an oversight. A real consumer is a different service with a different
    # database; it could not declare such a key, and giving it one here would
    # quietly make the demonstration depend on the two systems sharing a
    # transaction, which is the exact thing this phase exists to avoid needing.
    #
    # The primary key IS the event id. That is the whole dedupe mechanism: a second
    # delivery of one event collides and does nothing, so at-least-once delivery
    # produces an exactly-once effect.
    op.create_table(
        "event_deliveries",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "delivered_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- Inbound webhooks ---------------------------------------------------------
    # Keyed by the *provider's* event id, which is a string rather than a uuid
    # because that is what providers send ('evt_1a2b...'). Storing it as TEXT means
    # a malformed or unfamiliar id is a row we can record and reason about rather
    # than a parse error at the door.
    #
    # `payment_id` deliberately has NO foreign key to payments. The id in a webhook
    # is a claim made by a third party, not a reference this service controls; a
    # foreign key would turn "we do not recognise this attempt" into a constraint
    # violation deep in a flush, instead of a 404 the receiver answers on purpose so
    # the provider retries. See app/routers/webhooks.py.
    op.create_table(
        "webhook_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        # Defaulted rather than nullable because the row and the work it describes
        # are written in one transaction: by the time anyone else can see this row,
        # the handler has already replaced 'received' with what it did. A committed
        # row is always a processed row, which is why there is no 'in_progress'
        # status here of the kind idempotency_keys needs.
        sa.Column(
            "outcome", sa.Text(), nullable=False, server_default=sa.text("'received'")
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # "Which webhooks were about this payment?" -- the question asked during an
    # incident, and the only non-primary-key access pattern the table has. Partial,
    # because an event that names no attempt is not one anybody looks up this way.
    op.create_index(
        "ix_webhook_events_payment_id",
        "webhook_events",
        ["payment_id"],
        postgresql_where=sa.text("payment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_payment_id", table_name="webhook_events")
    op.drop_table("webhook_events")

    # Dropping the consumer's delivery log discards its dedupe state, which against
    # a real consumer would not be ours to drop -- it would live in their database
    # and survive our migrations entirely. Here it is a stand-in and goes with the
    # rest.
    op.drop_table("event_deliveries")

    op.execute("DROP INDEX IF EXISTS ix_outbox_events_pending")
    op.drop_table("outbox_events")
