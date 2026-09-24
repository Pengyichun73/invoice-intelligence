"""Stable shared API response schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ErrorResponse(BaseModel):
    """Stable error envelope for protocol and application failures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


STANDARD_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed request semantics"},
    403: {"model": ErrorResponse, "description": "Trusted tenant permission denied"},
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Resource state conflict"},
    422: {"model": ErrorResponse, "description": "Schema or document validation failed"},
    429: {"model": ErrorResponse, "description": "Configured request quota exceeded"},
    500: {"model": ErrorResponse, "description": "Internal persistence or server failure"},
    503: {"model": ErrorResponse, "description": "Extraction workflow is not configured"},
}
