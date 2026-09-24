"""Add optimistic revisions to field alias candidate governance.

Revision ID: 20260904_0018
Revises: 20260904_0017
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0018"
down_revision: str | None = "20260904_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("field_alias_candidates") as batch:
        batch.add_column(
            sa.Column(
                "revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch.create_check_constraint(
            "ck_field_alias_candidates_revision",
            "revision > 0",
        )
    with op.batch_alter_table("field_alias_candidates") as batch:
        batch.alter_column("revision", server_default=None)

    with op.batch_alter_table("field_alias_candidate_decisions") as batch:
        batch.add_column(
            sa.Column(
                "previous_revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch.add_column(
            sa.Column(
                "revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch.add_column(
            sa.Column(
                "policy_version",
                sa.String(length=128),
                nullable=False,
                server_default=sa.text("'legacy-field-alias-policy'"),
            )
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE field_alias_candidate_decisions "
            "SET policy_version = ("
            "SELECT candidate.policy_version "
            "FROM field_alias_candidates AS candidate "
            "WHERE candidate.candidate_id = "
            "field_alias_candidate_decisions.candidate_id)"
        )
    )

    with op.batch_alter_table("field_alias_candidate_decisions") as batch:
        batch.create_check_constraint(
            "ck_field_alias_candidate_decisions_revisions",
            "previous_revision > 0 AND revision >= previous_revision",
        )
        batch.alter_column("previous_revision", server_default=None)
        batch.alter_column("revision", server_default=None)
        batch.alter_column("policy_version", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("field_alias_candidate_decisions") as batch:
        batch.drop_constraint(
            "ck_field_alias_candidate_decisions_revisions",
            type_="check",
        )
        batch.drop_column("policy_version")
        batch.drop_column("revision")
        batch.drop_column("previous_revision")

    with op.batch_alter_table("field_alias_candidates") as batch:
        batch.drop_constraint(
            "ck_field_alias_candidates_revision",
            type_="check",
        )
        batch.drop_column("revision")
