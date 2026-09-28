"""Central logging configuration."""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from logging.config import dictConfig
from pathlib import Path
from typing import Any

from invoice_intelligence.config.settings import Environment, Settings

_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
_SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _prune_stale_files(directory: Path, component: str, retention_days: int) -> None:
    """Remove only old files from stopped processes in this component directory."""
    filename = re.compile(rf"^{re.escape(component)}-(\d+)\.jsonl(?:\.\d+)?$")
    cutoff = time.time() - retention_days * 86_400
    for path in directory.iterdir():
        match = filename.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff and not _process_alive(int(match.group(1))):
                path.unlink()
        except OSError:
            continue

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
    "cause_type",
    "db_driver_error_type",
    "db_connection_invalidated",
    "db_sqlstate",
    "http_method",
    "http_route",
    "validation_error_type",
    "validation_error_count",
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


def safe_failure_fields(error: Exception) -> dict[str, str | bool]:
    """Select only diagnostic metadata from a wrapped exception."""

    fields: dict[str, str | bool] = {}
    cause = error.__cause__
    if cause is not None:
        fields["cause_type"] = type(cause).__name__
    database_error = cause if cause is not None else error
    original = getattr(database_error, "orig", None)
    if original is not None:
        fields["db_driver_error_type"] = type(original).__name__
    invalidated = getattr(database_error, "connection_invalidated", None)
    if isinstance(invalidated, bool):
        fields["db_connection_invalidated"] = invalidated
    sqlstate = getattr(original, "sqlstate", None)
    if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate):
        fields["db_sqlstate"] = sqlstate
    return fields


class JsonFormatter(logging.Formatter):
    """Render structured application logs without external dependencies."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record as one JSON object."""

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, _SHANGHAI_TIMEZONE
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _safe_message(record),
        }
        for field_name in _SAFE_EXTRA_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        if record.exc_info and record.exc_info[0] is not None:
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
        context: dict[str, object] = {}
        try:
            from invoice_intelligence.infrastructure.observability.trace import (
                current_trace_fields,
            )

            context = current_trace_fields()
        except ImportError:
            pass
        for name in (
            "http_method", "http_route", "status_code", "duration_ms", "error_type",
            "cause_type", "db_driver_error_type", "db_connection_invalidated",
            "db_sqlstate", "validation_error_type", "validation_error_count",
            "trace_id", "tenant_id",
        ):
            value = getattr(record, name, None)
            if value is not None:
                context.setdefault(name, value)
        fields = " ".join(f"{key}={value}" for key, value in context.items())
        suffix = f" {fields}" if fields else ""
        if record.exc_info and record.exc_info[0] is not None:
            suffix += f" exception_type={record.exc_info[0].__name__}"
        return (
            f"{datetime.fromtimestamp(record.created, _SHANGHAI_TIMEZONE).isoformat()} "
            f"{record.levelname} "
            f"{record.name} {_safe_message(record)}{suffix}"
        )


def configure_logging(settings: Settings, *, component: str = "api") -> None:
    """Configure process logging from immutable settings."""

    formatter_name = "json" if settings.log_json else "text"
    file_enabled = (
        settings.log_file_enabled
        if settings.log_file_enabled is not None
        else settings.environment is Environment.DEVELOPMENT
    )
    handlers: dict[str, dict[str, object]] = {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": formatter_name,
            "stream": "ext://sys.stdout",
        },
    }
    root_handlers = ["default"]
    if file_enabled:
        if not _COMPONENT_NAME.fullmatch(component):
            raise ValueError("Invalid logging component name")
        log_dir = settings.log_directory.expanduser().resolve() / component
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune_stale_files(log_dir, component, settings.log_file_retention_days)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "json",
            "filename": str(log_dir / f"{component}-{os.getpid()}.jsonl"),
            "maxBytes": settings.log_file_max_bytes,
            "backupCount": settings.log_file_backup_count,
            "encoding": "utf-8",
        }
        root_handlers.append("file")
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": JsonFormatter},
                "text": {"()": SafeTextFormatter},
            },
            "handlers": handlers,
            "root": {
                "handlers": root_handlers,
                "level": settings.log_level,
            },
        }
    )
