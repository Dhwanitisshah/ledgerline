"""refunds: money going back, and the invariant that bounds it

Phase 6 makes ``refunded`` reachable. Four things change, and only one of them is
a new table in the ordinary sense:

* ``refunds`` -- one row per refund attempt against a payment. A payment may have
  many; their amounts sum to at most what was charged.
* ``processor_refunds`` -- **not Ledgerline's data**, in exactly the sense
  ``processor_charges`` is not (migration 0005). A refund is a real reversing
  operation on the processor's side too, and it keeps its own record of it.
* ``ck_payments_posting_matches_status`` is **replaced**. Migration 0003 wrote it
  as ``(status = 'succeeded') = (ledger_transaction_id IS NOT NULL)`` and left a
  note saying Phase 6 would have to revisit it. This is that: a refunded payment
  keeps the posting from its original charge, so the old constraint makes the
  ``refunded`` status literally unstorable.
* A trigger enforcing that refunds never exceed the charge. See below -- it is the
  most interesting thing in this file.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-22

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- The payment constraint that Phase 2 knew it would have to widen ---------
    #
    # Old: exactly the *succeeded* payments have a posting.
    # New: exactly the payments that *moved money* have a posting.
    #
    # A refunded payment moved money -- it was charged, and then reversed by a
    # second posting of its own. Its original ledger_transaction_id stays, because
    # removing it would be editing history, which the ledger tables forbid anyway.
    #
    # Note what is deliberately NOT relaxed: a 'failed' payment still may not have
    # a posting, and neither may a 'processing' one. The constraint got wider by
    # exactly one status and not one inch more.
    op.drop_constraint("ck_payments_posting_matches_status", "payments", type_="check")
    op.create_check_constraint(
        "ck_payments_posting_matches_status",
        "payments",
        "(status IN ('succeeded', 'refunded')) = (ledger_transaction_id IS NOT NULL)",
    )

    # --- Refunds ------------------------------------------------------------------
    # Shaped deliberately like ``payments``: an amount in minor units, a status, the
    # processor's reference, and a nullable link to the posting that justifies it.
    # A reader who understands the payments table understands this one, which is
    # worth more than any cleverness available here.
    op.create_table(
        "refunds",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_id",
            sa.Uuid(),
            sa.ForeignKey("payments.id"),
            nullable=False,
            index=True,
        ),
        # BIGINT minor units, same rule as everywhere. A partial refund is just a
        # smaller number here; there is no separate "partial" flag, because
        # "partial" is a comparison against the charge rather than a property of
        # the refund itself.
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # 'succeeded' | 'failed'. Deliberately NOT the payment_status enum: a refund
        # has no 'created'/'processing' lifecycle to model, because it is decided
        # inside one transaction. Sharing the type would advertise states this
        # table can never hold.
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("processor_ref", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        # The reversing posting. NULL exactly when the refund did not succeed.
        sa.Column(
            "ledger_transaction_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_transactions.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        sa.CheckConstraint("status IN ('succeeded', 'failed')", name="ck_refunds_status"),
        # The same shape of constraint as payments, for the same reason: exactly the
        # refunds that moved money have a posting.
        sa.CheckConstraint(
            "(status = 'succeeded') = (ledger_transaction_id IS NOT NULL)",
            name="ck_refunds_posting_matches_status",
        ),
    )

    # "What has already been refunded against this payment?" is the only question
    # asked of this table on the hot path, and it is asked inside a lock on every
    # refund. Partial over the succeeded rows, because a failed refund reserves
    # nothing and summing it would be wrong as well as wasteful.
    op.execute(
        "CREATE INDEX ix_refunds_payment_id_succeeded ON refunds (payment_id) "
        "WHERE status = 'succeeded'"
    )

    # --- The over-refund invariant, in the database -------------------------------
    #
    # The rule: the succeeded refunds for one payment may never total more than that
    # payment was charged.
    #
    # A CHECK constraint cannot express this, for exactly the reason the README's
    # Phase 4 section gives for why ``CHECK (balance >= 0)`` cannot be written: a
    # CHECK sees only the row being written, and this rule is about a SUM over
    # *other* rows. So it is a trigger.
    #
    # But a trigger that merely sums is not enough either, and this is the subtle
    # part. Under READ COMMITTED two concurrent inserts each read a sum that does
    # not include the other, both conclude there is room, and both commit -- the
    # Phase 4 overdraw race, reproduced inside a trigger that looks like it is
    # preventing exactly that.
    #
    # So the trigger takes the lock itself: SELECT ... FROM payments FOR UPDATE,
    # before it sums. That serialises every refund against one payment at the
    # database, regardless of what the caller did or forgot to do. The application
    # takes this lock too (app/locking.py) so it can answer with a clean 4xx rather
    # than an exception -- but the guarantee does not depend on the application
    # remembering, which is what "unstorable, even from psql" has to mean.
    #
    # FOR EACH ROW rather than the FOR EACH STATEMENT used by the append-only
    # triggers in migration 0002: those reject an operation outright and need no
    # row, while this one has to inspect the values being written.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledgerline_enforce_refund_total()
        RETURNS trigger AS $function$
        DECLARE
            charged bigint;
            already bigint;
        BEGIN
            -- The lock, before the read. Everything below is serialised per
            -- payment because of this line, and would be racy without it.
            SELECT amount INTO charged
            FROM payments
            WHERE id = NEW.payment_id
            FOR UPDATE;

            IF charged IS NULL THEN
                RAISE EXCEPTION
                    'ledgerline: refund references unknown payment %',
                    NEW.payment_id;
            END IF;

            SELECT COALESCE(SUM(amount), 0) INTO already
            FROM refunds
            WHERE payment_id = NEW.payment_id
              AND status = 'succeeded'
              AND id <> NEW.id;

            IF already + NEW.amount > charged THEN
                RAISE EXCEPTION
                    'ledgerline: refunds for payment % would total % minor '
                    'units, which exceeds the % charged',
                    NEW.payment_id, already + NEW.amount, charged;
            END IF;

            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql;
        """
    )

    # Only succeeded refunds are constrained, because only they move money. A failed
    # refund is an audit record of an attempt the processor declined, and refusing
    # to store one because the payment is already fully refunded would discard
    # evidence rather than protect anything.
    op.execute(
        """
        CREATE TRIGGER refunds_never_exceed_the_charge
        BEFORE INSERT OR UPDATE ON refunds
        FOR EACH ROW
        WHEN (NEW.status = 'succeeded')
        EXECUTE FUNCTION ledgerline_enforce_refund_total();
        """
    )

    # --- The processor's refund books ---------------------------------------------
    # Everything migration 0005 says about processor_charges applies here unchanged:
    # not our data, no foreign keys into the money model, written on its own
    # connection in its own transaction, and read only through ProcessorAdapter.
    #
    # ``attempt_ref`` is the processor-side idempotency key for the refund, and
    # Phase 6 makes it a *derived* value -- uuid5 over (payment id, idempotency key)
    # -- rather than a fresh uuid. See app/refunds.py: that is what lets a refund
    # survive a crash without the two-transaction split Phase 5a needed for charges,
    # because a reference that can be recomputed does not have to be stored to be
    # recoverable.
    op.create_table(
        "processor_refunds",
        sa.Column("attempt_ref", sa.Uuid(), primary_key=True),
        # Which charge this reverses, in the processor's own terms (the charge's
        # attempt_ref). Not a foreign key -- see migration 0005; the processor's
        # tables do not reference ours, and do not reference each other on our
        # behalf either.
        sa.Column("charge_ref", sa.Uuid(), nullable=False, index=True),
        sa.Column("processor_ref", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure')", name="ck_processor_refunds_outcome"
        ),
        sa.CheckConstraint("amount > 0", name="ck_processor_refunds_amount_positive"),
    )


def downgrade() -> None:
    op.drop_table("processor_refunds")

    op.execute("DROP TRIGGER IF EXISTS refunds_never_exceed_the_charge ON refunds")
    op.execute("DROP FUNCTION IF EXISTS ledgerline_enforce_refund_total()")

    op.execute("DROP INDEX IF EXISTS ix_refunds_payment_id_succeeded")
    op.drop_table("refunds")

    # Put migration 0003's constraint back exactly as it was. Note that this fails
    # if any payment is currently 'refunded' -- correctly so. The downgrade would be
    # restoring a rule the data violates, and dropping the constraint instead would
    # leave the schema claiming a guarantee it no longer has.
    op.drop_constraint("ck_payments_posting_matches_status", "payments", type_="check")
    op.create_check_constraint(
        "ck_payments_posting_matches_status",
        "payments",
        "(status = 'succeeded') = (ledger_transaction_id IS NOT NULL)",
    )
