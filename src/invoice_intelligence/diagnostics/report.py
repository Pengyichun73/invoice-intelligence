"""Build a small diagnostic packet from privacy-safe structured logs."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*(?:Z|[+-]\d{2}:\d{2})$")
_ROTATED_LOG = re.compile(r"^[a-z][a-z0-9-]{0,63}-\d+\.jsonl(?:\.[1-9]\d?)?$")
_FIELDS = (
    "timestamp", "stage", "operation", "outcome", "error_code", "error_type",
    "resource_type", "resource_id", "run_id", "document_id", "worker_id",
    "status_code", "attempt_count", "duration_ms", "model_version",
    "provider_version", "config_version", "index_version",
)
_SOURCE_HINTS = {
    "ingestion": "src/invoice_intelligence/application/services/document_ingestion.py",
    "document_preprocessing": "src/invoice_intelligence/infrastructure/documents/processor.py",
    "vision": "src/invoice_intelligence/application/services/vision_extraction.py",
    "ocr": "src/invoice_intelligence/application/services/multi_source_ocr_comparison.py",
    "retrieval": "src/invoice_intelligence/application/services/example_retrieval.py",
    "validation": "src/invoice_intelligence/application/services/extraction_validation.py",
    "human_review": "src/invoice_intelligence/application/services/review_tasks.py",
    "memory_admission": "src/invoice_intelligence/application/services/memory_admission_worker.py",
    "field_semantic_binding": (
        "src/invoice_intelligence/application/services/field_semantic_binding.py"
    ),
    "index_projection": "src/invoice_intelligence/application/services/example_index_projection.py",
    "governance_operation": "src/invoice_intelligence/application/services/memory_governance.py",
    "background_recovery": "src/invoice_intelligence/workers/_queued_worker.py",
    "code_harness": "src/invoice_intelligence/code_harness/workflow/service.py",
}


def safe_identifier(value: object, *, limit: int = 128) -> str | None:
    if not isinstance(value, str) or len(value) > limit or not _IDENTIFIER.fullmatch(value):
        return None
    return value


def _safe_event(raw: dict[str, Any]) -> dict[str, str | int | float]:
    event: dict[str, str | int | float] = {}
    for name in _FIELDS:
        value = raw.get(name)
        if name == "timestamp":
            if isinstance(value, str) and _TIMESTAMP.fullmatch(value):
                event[name] = value
        elif name in {"status_code", "attempt_count"}:
            if type(value) is int and 0 <= value <= 100_000:
                event[name] = value
        elif name == "duration_ms":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and (
                0 <= value <= 86_400_000
            ):
                event[name] = value
        else:
            safe = safe_identifier(value)
            if safe is not None:
                event[name] = safe
    return event


def _failed(event: dict[str, str | int | float]) -> bool:
    status_code = event.get("status_code")
    return (
        event.get("outcome") in {"failed", "failure", "error"}
        or "error_code" in event
        or "error_type" in event
        or isinstance(status_code, int) and status_code >= 500
    )


def _log_files(path: Path, max_bytes: int) -> list[Path]:
    if path.is_file() and not path.is_symlink():
        files = [path]
    elif path.is_dir() and not path.is_symlink():
        files = [
            entry
            for component in path.iterdir()
            if component.is_dir() and not component.is_symlink()
            for entry in component.iterdir()
            if entry.is_file() and not entry.is_symlink()
            and entry.name.startswith(component.name + "-")
            and _ROTATED_LOG.fullmatch(entry.name)
        ]
    else:
        raise ValueError("log path is unavailable")
    if not files or len(files) > 256:
        raise ValueError("log file count is outside the allowed range")
    total_bytes = 0
    for file in files:
        size = file.stat().st_size
        if size > 100_000_000:
            raise ValueError("a log file exceeds the size limit")
        total_bytes += size
        if total_bytes > max_bytes:
            raise ValueError("log directory exceeds the size limit")
    return sorted(files)


def _event_time(event: dict[str, str | int | float]) -> float:
    value = event.get("timestamp")
    if not isinstance(value, str):
        return float("inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def collect_trace_events(
    path: Path, *, trace_id: str, tenant_id: str,
    since: datetime | None = None, until: datetime | None = None,
    error_code: str | None = None,
    max_bytes: int = 512_000_000,
) -> tuple[list[dict[str, str | int | float]], int, bool]:
    """Read one file or a bounded logs directory; return metadata only."""
    matches: list[dict[str, str | int | float]] = []
    for file in _log_files(path, max_bytes):
        with file.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if len(line) > 16_384:
                    continue
                try:
                    raw = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                if raw.get("trace_id") != trace_id or raw.get("tenant_id") != tenant_id:
                    continue
                event = _safe_event(raw)
                if since is not None or until is not None:
                    event_time = _event_time(event)
                    if event_time == float("inf"):
                        continue
                    if (since is not None and event_time < since.timestamp()) or (
                        until is not None and event_time > until.timestamp()
                    ):
                        continue
                matches.append(event)
                if len(matches) > 10_000:
                    raise ValueError("too many matching events; narrow the time range")
    matches.sort(key=_event_time)
    matched = len(matches)
    focus = next(
        (i for i, event in enumerate(matches) if event.get("error_code") == error_code),
        None,
    ) if error_code else None
    if focus is None:
        focus = next((i for i, event in enumerate(matches) if _failed(event)), None)
    events = matches[max(0, focus - 5):focus + 6] if focus is not None else matches[-10:]
    return events, matched, matched > len(events)


def build_packet(
    *, trace_id: str, tenant_id: str,
    events: list[dict[str, str | int | float]], matched: int,
    events_omitted: bool, facts: dict[str, object] | None = None,
    max_chars: int = 6000, error_code: str | None = None,
) -> dict[str, object]:
    """Select nearby evidence and enforce a serialized output budget."""
    if max_chars < 1000 or max_chars > 20_000:
        raise ValueError("max_chars must be between 1000 and 20000")
    failure_index = next(
        (i for i, event in enumerate(events) if event.get("error_code") == error_code),
        None,
    ) if error_code else None
    if failure_index is None:
        failure_index = next((i for i, event in enumerate(events) if _failed(event)), None)
    chosen = (
        events[max(0, failure_index - 5):failure_index + 6]
        if failure_index is not None else events[-10:]
    )
    failure = events[failure_index] if failure_index is not None else None
    stage = failure.get("stage") if failure else None
    hint = _SOURCE_HINTS.get(str(stage)) if stage else None
    packet: dict[str, object] = {
        "schema_version": "diagnostic-packet-v1",
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "matched_events": matched,
        "context_events_omitted": events_omitted,
        "requested_error_code_found": (
            any(event.get("error_code") == error_code for event in events)
            if error_code else None
        ),
        "first_failure": failure,
        "source_hint": hint,
        "events": chosen,
        "facts": facts or {},
        "limitations": [
            "source_hint is a static starting point, not proof of root cause",
            "absence of an event does not prove the stage did not run",
        ],
    }
    while len(json.dumps(packet, ensure_ascii=False)) > max_chars and chosen:
        chosen.pop(0)
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        packet["facts"] = {"omitted": "output_budget"}
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        packet["first_failure"] = None
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        raise ValueError("diagnostic packet cannot fit the output budget")
    return packet
