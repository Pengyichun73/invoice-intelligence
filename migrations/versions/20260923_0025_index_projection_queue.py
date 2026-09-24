"""Add retry scheduling and worker leases to index projection queues."""

from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision = "20260923_0025_index"
down_revision = "20260923_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _alter(table: str, prefix: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("worker_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
        batch.create_index(f"ix_{prefix}_retry_queue", ["tenant_id", "index_version_id", "status", "next_attempt_at", "updated_at"])

def upgrade() -> None:
    _alter("example_index_projections", "example_index_projections")
    _alter("field_semantic_index_projections", "field_semantic_index_projections")

def downgrade() -> None:
    for table, prefix in (("field_semantic_index_projections", "field_semantic_index_projections"), ("example_index_projections", "example_index_projections")):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{prefix}_retry_queue")
            batch.drop_column("lease_token")
            batch.drop_column("worker_id")
            batch.drop_column("next_attempt_at")
