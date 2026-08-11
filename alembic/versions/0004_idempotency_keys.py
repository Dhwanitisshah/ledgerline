"""idempotency keys: claim-and-replay for POST /charges

Adds the table that makes a retried charge safe. One row per
``Idempotency-Key``, holding the fingerprint of the request that claimed it and
the exact response to replay.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-10

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        # The key is the primary key, so the uniqueness that the whole feature
        # rests on is the table's identity rather than a constraint bolted beside
        # it. Two requests cannot both own a key because two rows cannot share a
        # primary key -- that is the entire claim mechanism.
        sa.Column("key", sa.Text(), primary_key=True),
        # SHA-256 hex of the semantically-relevant request fields. See
        # app/idempotency.py for what is deliberately excluded and why.
        sa.Column("request_hash", sa.Text(), nullable=False),
        # The response to replay, stored exactly as it was sent. JSONB rather than
        # TEXT so it stays queryable during an incident ("which key produced this
        # payment?") instead of being an opaque blob.
        sa.Column("response_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'in_progress'")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')", name="ck_idempotency_keys_status"
        ),
        # A completed key must carry a replayable response, and an unfinished one
        # must not pretend to. Without this, a bug that finalises the status but
        # forgets the snapshot would produce a key that replays `null` forever --
        # silently, and only for the customer unlucky enough to retry.
        sa.CheckConstraint(
            "(status = 'completed') = "
            "(response_snapshot IS NOT NULL AND response_status IS NOT NULL)",
            name="ck_idempotency_keys_snapshot_matches_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_idempotency_keys_expiry_after_creation"
        ),
    )

    # Supports expiry sweeps. Nothing sweeps yet -- expired keys are reclaimed
    # lazily when the same key is presented again -- but a TTL with no way to find
    # expired rows is a table that only grows.
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
