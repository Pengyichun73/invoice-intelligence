"""Add an audited path for quarantined admission reassessment.

Revision ID: 20260909_0021
Revises: 20260909_0020
Create Date: 2026-09-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260909_0021"
down_revision: str | None = "20260909_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = (
    "'approve_admission', 'reject_admission', 'quarantine_admission', "
    "'requeue_admission', 'approve_field_alias', 'disable_field_alias', "
    "'resolve_conflict', 'dismiss_conflict', 'disable_example', "
    "'invalidate_schema', 'rebuild_index', 'submit_feedback'"
)
_PREVIOUS_ACTIONS = (
    "'approve_admission', 'reject_admission', 'quarantine_admission', "
    "'approve_field_alias', 'disable_field_alias', 'resolve_conflict', "
    "'dismiss_conflict', 'disable_example', 'invalidate_schema', "
    "'rebuild_index', 'submit_feedback'"
)


def upgrade() -> None:
    with op.batch_alter_table("memory_governance_audits") as batch:
        batch.drop_constraint("ck_memory_governance_audits_action", type_="check")
        batch.create_check_constraint(
            "ck_memory_governance_audits_action",
            f"action IN ({_ACTIONS})",
        )


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.exec_driver_sql(
        "SELECT 1 FROM memory_governance_audits "
        "WHERE action = 'requeue_admission' LIMIT 1"
    ).first()
    if used is not None:
        raise RuntimeError(
            "Cannot remove requeue_admission while its immutable audits exist"
        )
    with op.batch_alter_table("memory_governance_audits") as batch:
        batch.drop_constraint("ck_memory_governance_audits_action", type_="check")
        batch.create_check_constraint(
            "ck_memory_governance_audits_action",
            f"action IN ({_PREVIOUS_ACTIONS})",
        )
