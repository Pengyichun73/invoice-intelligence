"""Persist the tenant-scoped Harness repository source registry.

Revision ID: 20260924_0041_code_harness_sources
Revises: 20260924_0040_code_harness
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0041_code_harness_sources"
down_revision: str | None = "20260924_0040_code_harness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "code_harness_sources",
        sa.Column("repository_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.String(256), nullable=False),
        sa.Column("repository_version", sa.String(256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_code_harness_sources_revision"),
    )
    op.create_index(
        "ix_code_harness_sources_tenant_id",
        "code_harness_sources",
        ["tenant_id"],
    )
    op.create_index(
        "ix_code_harness_sources_tenant_enabled",
        "code_harness_sources",
        ["tenant_id", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_code_harness_sources_tenant_enabled", table_name="code_harness_sources")
    op.drop_index("ix_code_harness_sources_tenant_id", table_name="code_harness_sources")
    op.drop_table("code_harness_sources")
