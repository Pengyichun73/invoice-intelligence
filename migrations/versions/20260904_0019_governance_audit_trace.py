"""Persist governance resource versions and request trace identifiers.

Revision ID: 20260904_0019
Revises: 20260904_0018
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0019"
down_revision: str | None = "20260904_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = (
    "'approve_admission', 'reject_admission', 'quarantine_admission', "
    "'approve_field_alias', 'disable_field_alias', 'resolve_conflict', "
    "'dismiss_conflict', 'disable_example', 'invalidate_schema', "
    "'rebuild_index', 'submit_feedback'"
)
_LEGACY_ACTIONS = (
    "'disable_example', 'invalidate_schema', 'rebuild_index', 'submit_feedback'"
)


def upgrade() -> None:
    with op.batch_alter_table("memory_governance_audits") as batch:
        batch.add_column(
            sa.Column("resource_version", sa.String(length=256), nullable=True)
        )
        batch.add_column(sa.Column("trace_id", sa.String(length=64), nullable=True))
        batch.drop_constraint(
            "ck_memory_governance_audits_action",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_memory_governance_audits_action",
            f"action IN ({_ACTIONS})",
        )
        batch.create_index(
            "ix_memory_governance_audits_trace",
            ["tenant_id", "trace_id", "created_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("memory_governance_audits") as batch:
        batch.drop_index("ix_memory_governance_audits_trace")
        batch.drop_constraint(
            "ck_memory_governance_audits_action",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_memory_governance_audits_action",
            f"action IN ({_LEGACY_ACTIONS})",
        )
        batch.drop_column("trace_id")
        batch.drop_column("resource_version")
