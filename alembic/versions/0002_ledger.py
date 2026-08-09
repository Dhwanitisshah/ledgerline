"""ledger: accounts, ledger_transactions, ledger_entries, payments placeholder

Also installs the append-only triggers that make the ledger immutable at the
database level rather than by convention.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


entry_direction = postgresql.ENUM("debit", "credit", name="entry_direction", create_type=False)


# One shared trigger function, attached to both ledger tables. Statement-level
# (rather than row-level) on purpose: a FOR EACH ROW trigger never fires when the
# statement matches zero rows, so `DELETE FROM ledger_entries WHERE false` would
# quietly "succeed". FOR EACH STATEMENT rejects the operation unconditionally.
FORBID_MUTATION_FN = """
CREATE OR REPLACE FUNCTION ledgerline_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'ledgerline: % on % is not permitted -- the ledger is append-only',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""


def upgrade() -> None:
    op.execute("CREATE TYPE entry_direction AS ENUM ('debit', 'credit')")

    op.create_table(
        "accounts",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default=sa.text("'INR'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # No balance column. Balances are derived: see app/ledger.py.
    )

    op.create_table(
        "ledger_transactions",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ledger_entries",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "transaction_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_transactions.id"),
            nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("direction", entry_direction, nullable=False),
        # BIGINT minor units (paise/cents). Never float, never numeric.
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
    )
    op.create_index("ix_ledger_entries_transaction_id", "ledger_entries", ["transaction_id"])
    op.create_index("ix_ledger_entries_account_id", "ledger_entries", ["account_id"])

    # Phase 2 owns the payment lifecycle. This table exists only so the schema is
    # complete; nothing in Phase 1 reads or writes it.
    op.create_table(
        "payments",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'created'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- Immutability, enforced by Postgres -------------------------------------
    # An append-only ledger that is only append-only because the application never
    # writes an UPDATE is not append-only; it is a convention waiting to be broken
    # by a migration, a psql session, or a future teammate. These triggers make the
    # database refuse.
    #
    # Note TRUNCATE is intentionally *not* blocked: TRUNCATE does not fire UPDATE or
    # DELETE triggers, and the test suite relies on it to reset between tests.
    op.execute(FORBID_MUTATION_FN)
    op.execute(
        """
        CREATE TRIGGER ledger_entries_immutable
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH STATEMENT EXECUTE FUNCTION ledgerline_forbid_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_transactions_immutable
        BEFORE UPDATE OR DELETE ON ledger_transactions
        FOR EACH STATEMENT EXECUTE FUNCTION ledgerline_forbid_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_transactions_immutable ON ledger_transactions")
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS ledgerline_forbid_mutation()")

    op.drop_table("payments")
    op.drop_index("ix_ledger_entries_account_id", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_transaction_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("accounts")

    op.execute("DROP TYPE IF EXISTS entry_direction")
