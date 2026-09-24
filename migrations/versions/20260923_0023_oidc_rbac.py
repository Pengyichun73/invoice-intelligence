"""Add OIDC identity, RBAC, and security audit facts.

Revision ID: 20260923_0023
Revises: 20260909_0022
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0023"
down_revision: str | None = "20260909_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS = (
    "document:read",
    "document:extract",
    "review:read",
    "review:submit",
    "memory:read",
    "memory:admit",
    "index:rebuild",
    "index:activate",
    "evaluation:run",
    "training:submit",
    "model:promote",
    "admin:manage",
)
_ROLE_PERMISSIONS = {
    "invoice-reader": ("document:read", "review:read"),
    "invoice-extractor": ("document:read", "document:extract"),
    "invoice-reviewer": ("document:read", "review:read", "review:submit"),
    "memory-governor": ("memory:read", "memory:admit"),
    "index-operator": ("memory:read", "index:rebuild", "index:activate"),
    "evaluator": ("memory:read", "evaluation:run"),
    "trainer": ("training:submit",),
    "model-governor": ("model:promote",),
    "invoice-admin": _PERMISSIONS,
}


def upgrade() -> None:
    op.create_table(
        "auth_tenants",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_auth_tenants"),
    )
    op.create_table(
        "auth_users",
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("subject", name="pk_auth_users"),
        sa.UniqueConstraint("reviewer_id", name="uq_auth_users_reviewer_id"),
    )
    op.create_table(
        "auth_roles",
        sa.Column("role_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.PrimaryKeyConstraint("role_name", name="pk_auth_roles"),
    )
    op.create_table(
        "auth_permissions",
        sa.Column("permission_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.PrimaryKeyConstraint("permission_name", name="pk_auth_permissions"),
    )
    permissions = sa.table(
        "auth_permissions",
        sa.column("permission_name", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        permissions,
        [
            {"permission_name": permission, "description": permission}
            for permission in _PERMISSIONS
        ],
    )
    roles = sa.table(
        "auth_roles",
        sa.column("role_name", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        roles,
        [
            {"role_name": role_name, "description": role_name}
            for role_name in _ROLE_PERMISSIONS
        ],
    )
    op.create_table(
        "auth_user_tenants",
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["subject"], ["auth_users.subject"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["auth_tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("subject", "tenant_id", name="pk_auth_user_tenants"),
    )
    op.create_table(
        "auth_role_permissions",
        sa.Column("role_name", sa.String(length=128), nullable=False),
        sa.Column("permission_name", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_name"], ["auth_roles.role_name"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["permission_name"],
            ["auth_permissions.permission_name"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "role_name", "permission_name", name="pk_auth_role_permissions"
        ),
    )
    role_permissions = sa.table(
        "auth_role_permissions",
        sa.column("role_name", sa.String()),
        sa.column("permission_name", sa.String()),
    )
    op.bulk_insert(
        role_permissions,
        [
            {"role_name": role_name, "permission_name": permission}
            for role_name, permissions_for_role in _ROLE_PERMISSIONS.items()
            for permission in permissions_for_role
        ],
    )
    op.create_table(
        "auth_user_roles",
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("role_name", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["subject"], ["auth_users.subject"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["auth_tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_name"], ["auth_roles.role_name"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "subject", "tenant_id", "role_name", name="pk_auth_user_roles"
        ),
    )
    op.create_table(
        "security_audit_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("required_permission", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("request_path", sa.String(length=512), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('authentication_failed', 'authorization_denied', "
            "'identity_override_rejected', 'resource_not_found_or_cross_tenant')",
            name="ck_security_audit_events_type",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_security_audit_events"),
    )
    op.create_index(
        "ix_security_audit_events_tenant_time",
        "security_audit_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_security_audit_events_trace",
        "security_audit_events",
        ["trace_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_security_audit_events_trace", table_name="security_audit_events")
    op.drop_index("ix_security_audit_events_tenant_time", table_name="security_audit_events")
    op.drop_table("security_audit_events")
    op.drop_table("auth_user_roles")
    op.drop_table("auth_role_permissions")
    op.drop_table("auth_user_tenants")
    op.drop_table("auth_permissions")
    op.drop_table("auth_roles")
    op.drop_table("auth_users")
    op.drop_table("auth_tenants")
