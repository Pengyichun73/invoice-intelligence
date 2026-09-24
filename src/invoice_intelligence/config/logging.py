"""Central logging configuration."""

import json
import logging
from datetime import UTC, datetime
from logging.config import dictConfig
from typing import Any

from invoice_intelligence.config.settings import Settings

_SAFE_EXTRA_FIELDS = (
    "provider",
    "provider_name",
    "provider_version",
    "model_version",
    "config_version",
    "trace_id",
    "trace_ids",
    "span_id",
    "parent_span_id",
    "stage",
    "operation",
    "model",
    "outcome",
    "latency_ms",
    "duration_ms",
    "request_id",
    "error_type",
    "remote_status_code",
    "remote_error_code",
    "remote_error_type",
    "remote_error_param",
    "remote_request_id",
    "status_code",
    "candidate_count",
    "document_id",
    "run_id",
    "recovery_id",
    "page_count",
    "page_number",
    "page_quality",
    "ocr_call_count",
    "ocr_total_latency_ms",
    "ocr_page_latency_ms",
    "ocr_success_count",
    "ocr_timeout_count",
    "ocr_circuit_open_count",
    "ocr_schema_error_count",
    "ocr_other_error_count",
    "text_box_count",
    "empty_ocr_rate_numerator",
    "empty_ocr_rate_denominator",
    "raw_observation_count",
    "bound_field_count",
    "corroborated_count",
    "conflicting_count",
    "ocr_only_count",
    "vision_only_count",
    "unresolved_count",
    "unavailable_count",
    "ocr_conflict_review_rate_numerator",
    "ocr_conflict_review_rate_denominator",
    "attempt",
    "max_attempts",
    "worker_id",
    "tenant_id",
    "example_id",
    "attempt_count",
    "admission_status",
    "lease_matched",
    "retry_delay_seconds",
    "retry_exhausted",
    "error_code",
    "resource_id",
    "resource_type",
    "index_version",
    "projection_status",
)


def _safe_message(record: logging.LogRecord) -> str:
    if record.name.startswith("invoice_intelligence"):
        return record.getMessage()
    return "external_component_log"


class JsonFormatter(logging.Formatter):
    """Render structured application logs without external dependencies."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record as one JSON object."""

        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _safe_message(record),
        }
        for field_name in _SAFE_EXTRA_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        try:
            from invoice_intelligence.infrastructure.observability.trace import (
                current_trace_fields,
            )

            for field_name, value in current_trace_fields().items():
                payload.setdefault(field_name, value)
        except ImportError:
            pass
        return json.dumps(payload, ensure_ascii=False, default=str)


class SafeTextFormatter(logging.Formatter):
    """Render metadata-only text logs without exception bodies or tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        fields = ""
        try:
            from invoice_intelligence.infrastructure.observability.trace import (
                current_trace_fields,
            )

            context = current_trace_fields()
            fields = " ".join(f"{key}={value}" for key, value in context.items())
        except ImportError:
            pass
        suffix = f" {fields}" if fields else ""
        if record.exc_info:
            suffix += f" exception_type={record.exc_info[0].__name__}"
        return (
            f"{datetime.now(UTC).isoformat()} {record.levelname} "
            f"{record.name} {_safe_message(record)}{suffix}"
        )


def configure_logging(settings: Settings) -> None:
    """Configure process logging from immutable settings."""

    formatter_name = "json" if settings.log_json else "text"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": JsonFormatter},
                "text": {"()": SafeTextFormatter},
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter_name,
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "handlers": ["default"],
                "level": settings.log_level,
            },
        }
    )
