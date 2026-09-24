"""Add fenced, expiring leases to both projection queues.

Revision ID: 20260923_0034_index_lease
Revises: 20260923_0033_promotion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0034_index_lease"
down_revision: str | None = "20260923_0033_promotion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("example_index_projections", "field_semantic_index_projections"):
        op.add_column(
            table, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        # Old processing rows had no deadline and cannot prove ownership.
        op.execute(sa.text(
            f"UPDATE {table} SET status = 'pending', processing_started_at = NULL, "
            "worker_id = NULL, lease_token = NULL, next_attempt_at = NULL "
            "WHERE status = 'processing'"
        ))
        op.execute(sa.text(
            f"UPDATE {table} SET worker_id = NULL, lease_token = NULL "
            "WHERE status <> 'processing'"
        ))
        op.create_check_constraint(
            f"ck_{table}_lease", table,
            "(status = 'processing' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND worker_id IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
        )
        op.create_index(
            f"ix_{table}_lease", table,
            ["tenant_id", "index_version_id", "status", "lease_expires_at"],
        )


def downgrade() -> None:
    for table in ("field_semantic_index_projections", "example_index_projections"):
        op.drop_index(f"ix_{table}_lease", table_name=table)
        op.drop_constraint(f"ck_{table}_lease", table, type_="check")
        op.drop_column(table, "lease_expires_at")
