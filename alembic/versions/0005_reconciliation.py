"""reconciliation: the processor's own books, and a key that knows its payment

Phase 5a closes the crash window named in Phase 2. Two schema changes, and they
do different jobs:

* ``processor_charges`` -- **not Ledgerline's data**. It stands in for the payment
  processor's own database, so that "the card was charged" is a fact recorded
  somewhere that survives Ledgerline rolling back. Without it there is nothing to
  reconcile *against*, and the sweep would be reduced to guessing.
* ``idempotency_keys.payment_id`` -- so a sweep that settles an abandoned payment
  can also finalise the key its request claimed, rather than leaving the customer
  locked out of their own charge for 24 hours because of a crash on our side.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-12

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- The processor's books ---------------------------------------------------
    # Everything about this table is deliberately at arm's length from the money
    # model. No foreign key to payments, no foreign key to accounts, no shared enum
    # type. It is in this database because the project has one database, not
    # because it belongs to this service, and the moment anything joins against it
    # the demonstration it exists for has been broken.
    #
    # The primary key is the attempt reference Ledgerline sends -- the payment id,
    # used as the processor-side idempotency key. A second charge on the same
    # reference returns the first outcome instead of taking the money again, which
    # is both what a real processor does and what makes the sweep's lookup safe.
    op.create_table(
        "processor_charges",
        sa.Column("attempt_ref", sa.Uuid(), primary_key=True),
        sa.Column("processor_ref", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # The processor's vocabulary, kept as TEXT rather than reusing
        # payment_status. Sharing the enum would imply the two systems agree on a
        # type, and they do not: 'processing' is meaningless here and 'success' is
        # meaningless there.
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure')", name="ck_processor_charges_outcome"
        ),
        sa.CheckConstraint("amount > 0", name="ck_processor_charges_amount_positive"),
    )

    # --- The key -> payment link -------------------------------------------------
    # Nullable, and permanently so. There are two legitimate ways to hold a claim
    # with no payment behind it: a claim taken microseconds ago whose payment row
    # is not inserted yet, and the naive claim path, which never writes a key until
    # the charge is already over. A NOT NULL here would forbid both.
    op.add_column("idempotency_keys", sa.Column("payment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_idempotency_keys_payment_id",
        "idempotency_keys",
        "payments",
        ["payment_id"],
        ["id"],
    )

    # The sweep's second query is "which key claimed this payment?". Partial,
    # because the rows that matter are the bound ones and an index over a column
    # that is NULL for every expired and every naive-path key would mostly be
    # storing nothing.
    op.create_index(
        "ix_idempotency_keys_payment_id",
        "idempotency_keys",
        ["payment_id"],
        postgresql_where=sa.text("payment_id IS NOT NULL"),
    )

    # --- Finding abandoned payments ----------------------------------------------
    # The sweep looks for payments in 'processing' older than a threshold. A
    # partial index over exactly that predicate keeps the scan proportional to the
    # number of *unsettled* payments rather than to the size of the payments table,
    # which is the difference between a sweep that stays cheap forever and one that
    # gets slower every day the service succeeds.
    op.execute(
        "CREATE INDEX ix_payments_processing_created_at ON payments (created_at) "
        "WHERE status = 'processing'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_payments_processing_created_at")

    op.drop_index("ix_idempotency_keys_payment_id", table_name="idempotency_keys")
    op.drop_constraint(
        "fk_idempotency_keys_payment_id", "idempotency_keys", type_="foreignkey"
    )
    op.drop_column("idempotency_keys", "payment_id")

    # Dropping the processor's books discards evidence of charges the processor
    # made. In this project that is fine -- they are fake and the table is a stand
    # in. Against a real processor there would be nothing to drop, because the
    # records would never have been ours to delete.
    op.drop_table("processor_charges")
