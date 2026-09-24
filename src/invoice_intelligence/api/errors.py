"""Stable FastAPI exception-to-status mapping."""

import logging

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


async def _request_validation(_: Request, error: Exception) -> JSONResponse:
    diagnostics: list[str] = []
    if isinstance(error, RequestValidationError):
        diagnostics = [
            f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}"
            for item in error.errors()[:10]
        ]
    if diagnostics:
        _LOGGER.warning(
            "request_validation_error",
            extra={"schema_diagnostics": diagnostics},
        )
    message = "Request validation failed"
    if diagnostics:
        message += ": " + "; ".join(diagnostics)
    return _response(422, "request_validation_error", message)


async def _rate_limited(_: Request, error: Exception) -> JSONResponse:
    return _response(429, "rate_limit_exceeded", str(error))


async def _unavailable(_: Request, error: Exception) -> JSONResponse:
    return _response(503, "service_unavailable", str(error))


async def _internal(request: Request, error: Exception) -> JSONResponse:
    _LOGGER.error(
        "Unhandled API failure",
        extra={
            "path": request.url.path,
            "error_type": type(error).__name__,
        },
    )
    return _response(500, "internal_server_error", "Internal server error")


def _response(status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(code=code, message=message)
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
