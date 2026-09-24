"""Context-local, content-free trace spans emitted through structured logging."""

import logging
import math
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import uuid4

from invoice_intelligence.application.ports.observability import (
    SafeTraceValue,
    TraceStage,
)

_LOGGER = logging.getLogger(__name__)
_TRACE_ID: ContextVar[str | None] = ContextVar("invoice_trace_id", default=None)
_SPAN_ID: ContextVar[str | None] = ContextVar("invoice_span_id", default=None)
_STAGE: ContextVar[str | None] = ContextVar("invoice_trace_stage", default=None)

_SAFE_ATTRIBUTE_NAMES = frozenset(
    {
        "attempt_count",
        "candidate_count",
        "document_id",
        "example_id",
        "index_version",
        "page_count",
        "projection_status",
        "recovery_id",
        "resource_id",
        "resource_type",
        "run_id",
        "status_code",
        "worker_id",
    }
)


def current_trace_fields() -> dict[str, str]:
    """Return context metadata for the logging formatter."""

    return {
        name: value
        for name, value in (
            ("trace_id", _TRACE_ID.get()),
            ("span_id", _SPAN_ID.get()),
            ("stage", _STAGE.get()),
        )
        if value is not None
    }


class ContextVarPrivacyTelemetry:
    """Create nested metadata-only spans without a remote telemetry dependency."""

    @contextmanager
    def span(
        self,
        *,
        trace_id: str,
        tenant_id: str,
        stage: TraceStage,
        operation: str,
        attributes: Mapping[str, SafeTraceValue] | None = None,
    ) -> Iterator[None]:
        self._require_identifier("trace_id", trace_id, 64)
        self._require_identifier("tenant_id", tenant_id, 128)
        self._require_identifier("operation", operation, 128)
        safe_attributes = self._safe_attributes(attributes or {})
        span_id = uuid4().hex[:16]
        parent_span_id = _SPAN_ID.get()
        tokens: tuple[tuple[ContextVar[str | None], Token[str | None]], ...] = (
            (_TRACE_ID, _TRACE_ID.set(trace_id)),
            (_SPAN_ID, _SPAN_ID.set(span_id)),
            (_STAGE, _STAGE.set(stage.value)),
        )
        started_at = time.perf_counter()
        base = {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "stage": stage.value,
            "operation": operation,
            "tenant_id": tenant_id,
            **safe_attributes,
        }
        _LOGGER.info("telemetry_span_started", extra=base)
        try:
            yield
        except BaseException as exc:
            _LOGGER.warning(
                "telemetry_span_finished",
                extra={
                    **base,
                    "outcome": "failed",
                    "duration_ms": self._elapsed_ms(started_at),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        else:
            _LOGGER.info(
                "telemetry_span_finished",
                extra={
                    **base,
                    "outcome": "success",
                    "duration_ms": self._elapsed_ms(started_at),
                },
            )
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    @staticmethod
    def _safe_attributes(
        attributes: Mapping[str, SafeTraceValue],
    ) -> dict[str, SafeTraceValue]:
        unknown = set(attributes).difference(_SAFE_ATTRIBUTE_NAMES)
        if unknown:
            raise ValueError("Trace attributes contain non-whitelisted names")
        result: dict[str, SafeTraceValue] = {}
        for name, value in attributes.items():
            if isinstance(value, str):
                if len(value) > 256 or value != value.strip():
                    raise ValueError(f"Trace attribute {name} is not bounded and normalized")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"Trace attribute {name} must be finite")
            result[name] = value
        return result

    @staticmethod
    def _require_identifier(name: str, value: str, maximum: int) -> None:
        if not 1 <= len(value) <= maximum or value != value.strip():
            raise ValueError(f"{name} must be bounded and normalized")
        allowed = frozenset(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-/"
        )
        if any(character not in allowed for character in value):
            raise ValueError(f"{name} contains unsupported characters")

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return max(0.0, (time.perf_counter() - started_at) * 1000.0)
