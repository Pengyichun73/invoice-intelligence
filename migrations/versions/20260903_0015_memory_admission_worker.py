"""Add PostgreSQL leases and retry state for memory admission work.

Revision ID: 20260903_0015
Revises: 20260902_0014
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0015"
down_revision: str | None = "20260902_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("memory_admission_records") as batch:
        batch.add_column(sa.Column("worker_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("lease_token", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("last_error_code", sa.String(length=128), nullable=True)
        )
        batch.add_column(
            sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            "ck_memory_admission_records_attempt_count",
            "attempt_count >= 0",
        )
        batch.create_check_constraint(
            "ck_memory_admission_records_lease",
            "(worker_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_memory_admission_records_last_error",
            "(last_error_code IS NULL AND last_error_at IS NULL) "
            "OR (last_error_code IS NOT NULL AND last_error_at IS NOT NULL)",
        )
        batch.create_index(
            "ix_memory_admission_records_worker_queue",
            ["next_attempt_at", "lease_expires_at", "example_id"],
            unique=False,
        )

    op.execute(
        sa.text(
            "UPDATE memory_admission_records "
            "SET next_attempt_at = updated_at "
            "WHERE next_attempt_at IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("memory_admission_records") as batch:
        batch.drop_index("ix_memory_admission_records_worker_queue")
        batch.drop_constraint(
            "ck_memory_admission_records_last_error",
            type_="check",
        )
        batch.drop_constraint(
            "ck_memory_admission_records_lease",
            type_="check",
        )
        batch.drop_constraint(
            "ck_memory_admission_records_attempt_count",
            type_="check",
        )
        batch.drop_column("last_error_at")
        batch.drop_column("last_error_code")
        batch.drop_column("next_attempt_at")
        batch.drop_column("attempt_count")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
        batch.drop_column("worker_id")
