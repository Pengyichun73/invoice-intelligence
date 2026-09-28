"""FastAPI authentication, trusted-context installation, and route RBAC."""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from invoice_intelligence.api.dependencies import ApiDependencies
from invoice_intelligence.api.schemas.common import ErrorResponse
from invoice_intelligence.application.errors import ForbiddenError, UnauthorizedError
from invoice_intelligence.application.ports.auth import AuthContext, SecurityAuditEvent
from invoice_intelligence.application.services.authorization import (
    AuthorizationPolicy,
    Permission,
)
from invoice_intelligence.config.logging import safe_failure_fields

_LOGGER = logging.getLogger(__name__)
_IDENTITY_HEADERS = frozenset({"x-tenant-id", "tenant-id", "x-reviewer-id", "reviewer-id"})
_RULES: tuple[tuple[str, re.Pattern[str], Permission], ...] = (
    ("GET", re.compile(r"^/accounting(?:/.*)?$"), Permission.ACCOUNTING_READ),
    ("POST", re.compile(r"^/accounting/candidates/[^/]+/post$"), Permission.ACCOUNTING_POST),
    ("POST", re.compile(r"^/accounting(?:/.*)?$"), Permission.ACCOUNTING_GOVERN),
    (
        "POST",
        re.compile(r"^/documents(?:/[^/]+/extract)?$"),
        Permission.DOCUMENT_EXTRACT,
    ),
    (
        "GET",
        re.compile(r"^/documents/[^/]+/(?:download-url|content)$"),
        Permission.DOCUMENT_READ,
    ),
    ("GET", re.compile(r"^/runs/[^/]+(?:/result)?$"), Permission.DOCUMENT_READ),
    ("GET", re.compile(r"^/reviews(?:/[^/]+)?$"), Permission.REVIEW_READ),
    (
        "POST",
        re.compile(
            r"^/reviews/(?:recover-expired|[^/]+(?:/(?:claim|release|reassign|cancel|submit))?)$"
        ),
        Permission.REVIEW_SUBMIT,
    ),
    ("GET", re.compile(r"^/(?:memory|field-semantics)(?:/.*)?$"), Permission.MEMORY_READ),
    ("POST", re.compile(r"^/memory/indexes/[^/]+/(?:activate|rollback)$"), Permission.INDEX_ACTIVATE),
    ("POST", re.compile(r"^/field-semantics/indexes/[^/]+/(?:activate|rollback)$"), Permission.INDEX_ACTIVATE),
    (
        "POST",
        re.compile(r"^/transactions/candidates/analyze$"),
        Permission.TRANSACTION_ANALYZE,
    ),
    (
        "POST",
        re.compile(r"^/transactions/candidates/[^/]+/review$"),
        Permission.TRANSACTION_REVIEW,
    ),
    ("POST", re.compile(r"^/training/jobs$"), Permission.TRAINING_SUBMIT),
    ("GET", re.compile(r"^/training/jobs/[^/]+$"), Permission.TRAINING_SUBMIT),
    ("GET", re.compile(r"^/code-harness/tasks/[^/]+$"), Permission.CODE_HARNESS_READ),
    ("POST", re.compile(r"^/code-harness/tasks$"), Permission.CODE_HARNESS_EXECUTE),
    (
        "POST",
        re.compile(r"^/code-harness/tasks/[^/]+/resume$"),
        Permission.CODE_HARNESS_EXECUTE,
    ),
    (
        "GET",
        re.compile(r"^/code-harness/postmortems/[^/]+$"),
        Permission.CODE_HARNESS_READ,
    ),
    (
        "POST",
        re.compile(r"^/code-harness/postmortems/[^/]+/admission$"),
        Permission.CODE_HARNESS_GOVERN,
    ),
    ("GET", re.compile(r"^/evaluations/(?:snapshots/[^/]+|jobs(?:/[^/]+)?|schedules)$"), Permission.EVALUATION_RUN),
    ("POST", re.compile(r"^/evaluations/(?:snapshots|jobs|suite-jobs|schedules|schedules/[^/]+/disable)$"), Permission.EVALUATION_RUN),
    (
        "POST",
        re.compile(r"^/training/jobs/[^/]+/(?:cancel|retry)$"),
        Permission.TRAINING_SUBMIT,
    ),
    ("POST", re.compile(r"^/model-promotion/[^/]+/(?:approve|reject|rollback)$"), Permission.MODEL_PROMOTE),
    ("POST", re.compile(r"^/model-promotion$"), Permission.MODEL_PROMOTE),
    ("POST", re.compile(r"^/memory/indexes(?:/.*)?$"), Permission.INDEX_REBUILD),
    ("POST", re.compile(r"^/field-semantics/indexes(?:/.*)?$"), Permission.INDEX_REBUILD),
    ("POST", re.compile(r"^/(?:memory|field-semantics)(?:/.*)?$"), Permission.MEMORY_ADMIT),
)


async def enforce_request_security(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    dependencies: ApiDependencies,
) -> Response:
    settings = dependencies.settings
    path = request.url.path
    if not path.startswith(settings.api_prefix) or path == f"{settings.api_prefix}/health":
        return await call_next(request)

    trace_id = getattr(request.state, "server_trace_id", None) or uuid4().hex
    request.state.server_trace_id = trace_id
    permission = permission_for_request(request.method, path, settings.api_prefix)
    if _has_identity_override(request):
        await _audit(
            dependencies,
            "identity_override_rejected",
            "denied",
            "request.identity_override",
            trace_id,
            path,
            required_permission=permission,
        )
        return _error(400, "identity_override_rejected", "Request identity overrides are forbidden")

    try:
        context = await _authenticate(request, dependencies)
        policy: AuthorizationPolicy = dependencies.authorization_policy
        if permission is not None:
            policy.require(context, permission)
        request.state.auth_context = context
        request.state.trusted_tenant_context = policy.trusted_tenant_context(
            context,
            trace_id=trace_id,
        )
    except UnauthorizedError:
        await _audit(
            dependencies,
            "authentication_failed",
            "denied",
            "authentication.invalid",
            trace_id,
            path,
            required_permission=permission,
        )
        response = _error(401, "unauthorized", "Authentication is required")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    except ForbiddenError:
        await _audit(
            dependencies,
            "authorization_denied",
            "denied",
            "authorization.permission_denied",
            trace_id,
            path,
            context=context,
            required_permission=permission,
        )
        return _error(403, "forbidden", "Permission denied")

    downstream_response = await call_next(request)
    if downstream_response.status_code == 404 and permission is not None:
        await _audit(
            dependencies,
            "resource_not_found_or_cross_tenant",
            "not_found",
            "resource.not_found_or_cross_tenant",
            trace_id,
            path,
            context=context,
            required_permission=permission,
        )
    return downstream_response


def permission_for_request(method: str, path: str, api_prefix: str) -> Permission | None:
    relative = path.removeprefix(api_prefix)
    for expected_method, pattern, permission in _RULES:
        if method == expected_method and pattern.fullmatch(relative):
            return permission
    return None


async def _authenticate(request: Request, dependencies: ApiDependencies) -> AuthContext:
    gateway_context = getattr(request.state, "auth_context", None)
    if isinstance(gateway_context, AuthContext):
        return gateway_context
    authorization = request.headers.get("Authorization")
    if authorization is None:
        settings = dependencies.settings
        if (
            settings.auth_mode == "development"
            and settings.environment.value == "development"
            and settings.dev_tenant_id
        ):
            return AuthContext(
                subject="local-developer",
                tenant_id=settings.dev_tenant_id,
                reviewer_id="local-developer",
                roles=frozenset({"invoice-admin"}),
                scopes=frozenset(),
            )
        raise UnauthorizedError("Bearer token is missing")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token.strip():
        raise UnauthorizedError("Authorization must contain one Bearer token")
    if len(token) > 16_384:
        raise UnauthorizedError("Bearer token exceeds the configured safety limit")
    provider = dependencies.auth_context_provider
    if provider is None:
        raise UnauthorizedError("OIDC authentication is not configured")
    return await provider.authenticate(token.strip())


def _has_identity_override(request: Request) -> bool:
    if any(name.casefold() in _IDENTITY_HEADERS for name in request.headers):
        return True
    return any(name.casefold() in {"tenant_id", "reviewer_id"} for name in request.query_params)


async def _audit(
    dependencies: ApiDependencies,
    event_type: str,
    outcome: str,
    reason_code: str,
    trace_id: str,
    path: str,
    *,
    context: AuthContext | None = None,
    required_permission: Permission | None = None,
) -> None:
    sink = dependencies.security_audit_sink
    if sink is None:
        return
    try:
        await sink.record(
            SecurityAuditEvent(
                event_id=uuid4().hex,
                event_type=event_type,
                outcome=outcome,
                reason_code=reason_code,
                subject=context.subject if context else None,
                tenant_id=context.tenant_id if context else None,
                required_permission=(required_permission.value if required_permission else None),
                trace_id=trace_id,
                request_path=path[:512],
                occurred_at=datetime.now(UTC),
            )
        )
    except Exception as exc:
        _LOGGER.error(
            "security_audit_persistence_failed",
            extra={
                "error_type": type(exc).__name__,
                "trace_id": trace_id,
                "tenant_id": context.tenant_id if context else None,
                "operation": "security_audit",
                **safe_failure_fields(exc),
            },
        )


def _error(status: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(code=code, message=message)
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))
