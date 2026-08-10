"""payment lifecycle: payment_status enum, money columns, atomicity constraint

Turns the Phase 1 ``payments`` placeholder into a real table:

* ``status`` becomes a Postgres ``payment_status`` enum instead of free TEXT
* ``currency``, ``processor_ref``, ``failure_reason``, ``updated_at`` are added
* ``ledger_transaction_id`` links a payment to the postings that justify it
* a CHECK constraint makes a half-charge unrepresentable at the database level
* a partial UNIQUE index guarantees one house settlement account per currency

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


PAYMENT_STATUSES = ("created", "processing", "succeeded", "failed", "refunded")

# 'refunded' is created here but unreachable in Phase 2: no transition in
# app/payments.py leads to it and no endpoint produces one. It is in the type now
# because widening a Postgres enum later is a migration and a deploy-ordering
# problem, and this is the cheap moment to avoid both. Phase 6 fills it in.
_CREATE_STATUS_TYPE = "CREATE TYPE payment_status AS ENUM ({})".format(
    ", ".join(f"'{value}'" for value in PAYMENT_STATUSES)
)


def upgrade() -> None:
    op.execute(_CREATE_STATUS_TYPE)

    # TEXT -> enum. The DEFAULT has to come off first: Postgres will not cast an
    # existing default expression along with the column type, and leaving it in
    # place makes the ALTER fail rather than silently do the wrong thing.
    op.execute("ALTER TABLE payments ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE payments ALTER COLUMN status TYPE payment_status "
        "USING status::payment_status"
    )
    op.execute("ALTER TABLE payments ALTER COLUMN status SET DEFAULT 'created'::payment_status")

    op.add_column(
        "payments",
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default=sa.text("'INR'")),
    )
    op.add_column("payments", sa.Column("processor_ref", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("ledger_transaction_id", sa.Uuid(), nullable=True))
    op.add_column(
        "payments",
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_foreign_key(
        "fk_payments_ledger_transaction_id",
        "payments",
        "ledger_transactions",
        ["ledger_transaction_id"],
        ["id"],
    )
    op.create_index("ix_payments_account_id", "payments", ["account_id"])

    # Same money rule as the ledger: minor units, and a charge for nothing is not a
    # charge.
    op.create_check_constraint("ck_payments_amount_positive", "payments", "amount > 0")

    # --- The atomicity guarantee, enforced by Postgres ---------------------------
    # Exactly the succeeded payments have a posting:
    #
    #   succeeded  =>  ledger_transaction_id IS NOT NULL
    #   anything else => ledger_transaction_id IS NULL
    #
    # The charge route already arranges this, but "the route is careful" is the
    # same class of promise as "the application never UPDATEs the ledger" -- true
    # until someone writes a second route. This constraint means a payment marked
    # succeeded with no money behind it, or marked failed while pointing at a
    # posting, cannot be stored at all. It is the headline property of Phase 2
    # written where it cannot be forgotten.
    #
    # Phase 6 will need to revisit this when 'refunded' becomes reachable, since a
    # refunded payment keeps the posting from its original charge.
    op.create_check_constraint(
        "ck_payments_posting_matches_status",
        "payments",
        "(status = 'succeeded') = (ledger_transaction_id IS NOT NULL)",
    )

    # --- One house settlement account per currency -------------------------------
    # Charges credit a customer and debit a house account (see app/ledger.py). A
    # partial UNIQUE index over just the 'house:' namespace makes the get-or-create
    # in settlement_account_id() race-free without constraining customer account
    # names, which are free text and may legitimately repeat.
    op.execute(
        "CREATE UNIQUE INDEX uq_accounts_house_name ON accounts (name) WHERE name LIKE 'house:%'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_accounts_house_name")

    op.drop_constraint("ck_payments_posting_matches_status", "payments", type_="check")
    op.drop_constraint("ck_payments_amount_positive", "payments", type_="check")
    op.drop_index("ix_payments_account_id", table_name="payments")
    op.drop_constraint("fk_payments_ledger_transaction_id", "payments", type_="foreignkey")

    op.drop_column("payments", "updated_at")
    op.drop_column("payments", "ledger_transaction_id")
    op.drop_column("payments", "failure_reason")
    op.drop_column("payments", "processor_ref")
    op.drop_column("payments", "currency")

    # enum -> TEXT, mirroring the upgrade: default off, retype, default back on.
    # Any row sitting in 'refunded' survives as the text 'refunded'; nothing is
    # lost going down, which is the property that makes this migration safe to
    # reverse on a database that has real payments in it.
    op.execute("ALTER TABLE payments ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE payments ALTER COLUMN status TYPE text USING status::text")
    op.execute("ALTER TABLE payments ALTER COLUMN status SET DEFAULT 'created'")

    op.execute("DROP TYPE IF EXISTS payment_status")
