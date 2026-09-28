"""Stable FastAPI exception-to-status mapping."""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from invoice_intelligence.api.schemas.common import ErrorResponse
from invoice_intelligence.application.errors import (
    BadRequestError,
    DocumentError,
    ForbiddenError,
    HumanCorrectionError,
    RateLimitExceededError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    StorageError,
    UnauthorizedError,
    UnprocessableEntityError,
    WorkflowIdentityError,
    WorkflowPersistenceError,
)
from invoice_intelligence.code_harness.domain.errors import HarnessError, HarnessErrorCode
from invoice_intelligence.config.logging import safe_failure_fields
from invoice_intelligence.domain.governance import TrustedTenantContext

_LOGGER = logging.getLogger(__name__)


def install_exception_handlers(app: FastAPI) -> None:
    """Install stable mappings for protocol, permission, quota, and service failures."""

    app.add_exception_handler(BadRequestError, _bad_request)
    app.add_exception_handler(UnprocessableEntityError, _unprocessable)
    app.add_exception_handler(ForbiddenError, _forbidden)
    app.add_exception_handler(UnauthorizedError, _unauthorized)
    app.add_exception_handler(ResourceNotFoundError, _not_found)
    app.add_exception_handler(ResourceConflictError, _conflict)
    app.add_exception_handler(WorkflowIdentityError, _conflict)
    app.add_exception_handler(DocumentError, _unprocessable)
    app.add_exception_handler(HumanCorrectionError, _unprocessable)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(RateLimitExceededError, _rate_limited)
    app.add_exception_handler(ServiceUnavailableError, _unavailable)
    app.add_exception_handler(WorkflowPersistenceError, _internal)
    app.add_exception_handler(StorageError, _internal)
    app.add_exception_handler(Exception, _internal)
    app.add_exception_handler(HarnessError, _harness_error)


async def _bad_request(_: Request, error: Exception) -> JSONResponse:
    return _response(400, "bad_request", str(error))


async def _not_found(_: Request, error: Exception) -> JSONResponse:
    return _response(404, "not_found", str(error))


async def _forbidden(_: Request, error: Exception) -> JSONResponse:
    return _response(403, "forbidden", str(error))


async def _unauthorized(_: Request, error: Exception) -> JSONResponse:
    response = _response(401, "unauthorized", str(error))
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


async def _conflict(_: Request, error: Exception) -> JSONResponse:
    return _response(409, "conflict", str(error))


async def _unprocessable(_: Request, error: Exception) -> JSONResponse:
    return _response(422, "unprocessable_entity", str(error))


async def _request_validation(request: Request, error: Exception) -> JSONResponse:
    diagnostics: list[str] = []
    if isinstance(error, RequestValidationError):
        issues = error.errors()
        diagnostics = [
            f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}"
            for item in issues[:10]
        ]
    else:
        issues = []
    if issues:
        _LOGGER.warning(
            "request_validation_error",
            extra={
                **_failure_log_fields(request, error, 422),
                "validation_error_type": str(issues[0]["type"])[:64],
                "validation_error_count": len(issues),
            },
        )
    message = "Request validation failed"
    if diagnostics:
        message += ": " + "; ".join(diagnostics)
    return _response(422, "request_validation_error", message)


async def _rate_limited(_: Request, error: Exception) -> JSONResponse:
    return _response(429, "rate_limit_exceeded", str(error))


async def _unavailable(request: Request, error: Exception) -> JSONResponse:
    _LOGGER.warning("API dependency unavailable", extra=_failure_log_fields(request, error, 503))
    return _response(503, "service_unavailable", str(error))


async def _internal(request: Request, error: Exception) -> JSONResponse:
    status_code = 503 if isinstance(error, WorkflowPersistenceError) else 500
    _LOGGER.error(
        "Unhandled API failure",
        extra=_failure_log_fields(request, error, status_code),
    )
    if isinstance(error, WorkflowPersistenceError):
        return _response(
            503,
            "service_unavailable",
            "数据库服务暂时不可用，请稍后重试",
        )
    return _response(500, "internal_server_error", "Internal server error")


def _failure_log_fields(
    request: Request, error: Exception, status_code: int
) -> dict[str, object]:
    context = getattr(request.state, "trusted_tenant_context", None)
    route = getattr(request.scope.get("route"), "path", None)
    started_at = getattr(request.state, "request_started_at", None)
    duration_ms = (
        max(0.0, (time.perf_counter() - started_at) * 1000)
        if isinstance(started_at, float)
        else None
    )
    return {
        "error_type": type(error).__name__,
        "http_method": request.method,
        "http_route": route if isinstance(route, str) else None,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "trace_id": getattr(request.state, "server_trace_id", None),
        "tenant_id": context.tenant_id if isinstance(context, TrustedTenantContext) else None,
        **safe_failure_fields(error),
    }


async def _harness_error(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, HarnessError):
        return _response(500, "internal_server_error", "Internal server error")
    status = 500
    if error.code is HarnessErrorCode.TASK_NOT_FOUND:
        status = 404
    elif error.code in {
        HarnessErrorCode.REVISION_CONFLICT,
        HarnessErrorCode.LEASE_FENCING_REJECTED,
        HarnessErrorCode.IDEMPOTENCY_CONFLICT,
    }:
        status = 409
    elif error.code in {
        HarnessErrorCode.PARSER_NOT_CONFIGURED,
        HarnessErrorCode.INDEX_PROVIDER_NOT_CONFIGURED,
        HarnessErrorCode.MODEL_PROVIDER_NOT_CONFIGURED,
        HarnessErrorCode.SANDBOX_NOT_CONFIGURED,
    }:
        status = 503
    return _response(status, error.code.value, error.code.value)


def _response(status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(code=code, message=message)
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
