"""Deterministic application authorization policy."""

from enum import StrEnum

from invoice_intelligence.application.errors import ForbiddenError
from invoice_intelligence.application.ports.auth import AuthContext
from invoice_intelligence.domain.governance import MemoryPermission, TrustedTenantContext


class Permission(StrEnum):
    DOCUMENT_READ = "document:read"
    DOCUMENT_EXTRACT = "document:extract"
    REVIEW_READ = "review:read"
    REVIEW_SUBMIT = "review:submit"
    MEMORY_READ = "memory:read"
    MEMORY_ADMIT = "memory:admit"
    INDEX_REBUILD = "index:rebuild"
    INDEX_ACTIVATE = "index:activate"
    EVALUATION_RUN = "evaluation:run"
    TRAINING_SUBMIT = "training:submit"
    MODEL_PROMOTE = "model:promote"
    TRANSACTION_ANALYZE = "transaction:analyze"
    TRANSACTION_REVIEW = "transaction:review"
    ADMIN_MANAGE = "admin:manage"
    ACCOUNTING_READ = "accounting:read"
    ACCOUNTING_GOVERN = "accounting:govern"
    ACCOUNTING_POST = "accounting:post"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "invoice-reader": frozenset({Permission.DOCUMENT_READ, Permission.REVIEW_READ}),
    "invoice-extractor": frozenset(
        {
            Permission.DOCUMENT_READ,
            Permission.DOCUMENT_EXTRACT,
            Permission.TRANSACTION_ANALYZE,
        }
    ),
    "invoice-reviewer": frozenset(
        {
            Permission.DOCUMENT_READ,
            Permission.REVIEW_READ,
            Permission.REVIEW_SUBMIT,
            Permission.TRANSACTION_REVIEW,
        }
    ),
    "memory-governor": frozenset({Permission.MEMORY_READ, Permission.MEMORY_ADMIT}),
    "index-operator": frozenset(
        {Permission.MEMORY_READ, Permission.INDEX_REBUILD, Permission.INDEX_ACTIVATE}
    ),
    "evaluator": frozenset({Permission.MEMORY_READ, Permission.EVALUATION_RUN}),
    "trainer": frozenset({Permission.TRAINING_SUBMIT}),
    "model-governor": frozenset({Permission.MODEL_PROMOTE}),
    "accounting-reader": frozenset({Permission.ACCOUNTING_READ}),
    "accounting-reviewer": frozenset({Permission.ACCOUNTING_READ, Permission.ACCOUNTING_GOVERN}),
    "accounting-poster": frozenset({Permission.ACCOUNTING_READ, Permission.ACCOUNTING_POST}),
    "invoice-admin": frozenset(Permission),
}


class AuthorizationPolicy:
    """Resolve signed roles/scopes and enforce one explicit permission."""

    def effective_permissions(self, context: AuthContext) -> frozenset[Permission]:
        permissions: set[Permission] = set()
        for role in context.roles:
            permissions.update(ROLE_PERMISSIONS.get(role, ()))
            try:
                permissions.add(Permission(role))
            except ValueError:
                pass
        for scope in context.scopes:
            try:
                permissions.add(Permission(scope))
            except ValueError:
                pass
        if Permission.ADMIN_MANAGE in permissions:
            permissions.update(Permission)
        return frozenset(permissions)

    def require(self, context: AuthContext, permission: Permission) -> None:
        if permission not in self.effective_permissions(context):
            raise ForbiddenError("The authenticated identity lacks the required permission")

    def trusted_tenant_context(
        self,
        context: AuthContext,
        *,
        trace_id: str,
    ) -> TrustedTenantContext:
        effective = self.effective_permissions(context)
        memory: set[MemoryPermission] = set()
        if Permission.MEMORY_READ in effective:
            memory.add(MemoryPermission.READ)
        if Permission.MEMORY_ADMIT in effective:
            memory.update(
                {
                    MemoryPermission.GOVERN,
                    MemoryPermission.SUBMIT_FEEDBACK,
                    MemoryPermission.GOVERN_FIELD_ALIAS,
                    MemoryPermission.GOVERN_GLOBAL_FIELD_ALIAS,
                }
            )
        if Permission.INDEX_REBUILD in effective or Permission.INDEX_ACTIVATE in effective:
            memory.add(MemoryPermission.REBUILD_INDEX)
        if Permission.EVALUATION_RUN in effective:
            memory.add(MemoryPermission.READ_EVALUATION)
        return TrustedTenantContext(
            tenant_id=context.tenant_id,
            actor_id=context.reviewer_id,
            permissions=frozenset(memory),
            trace_id=trace_id,
        )
