"""Privacy and scoping checks for local AI diagnostic packets."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from invoice_intelligence.diagnostics.facts import load_facts
from invoice_intelligence.diagnostics.report import build_packet, collect_trace_events
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    CodeHarnessAttemptRow,
    CodeHarnessPostmortemRow,
    CodeHarnessPostmortemSourceEventRow,
    CodeHarnessTaskRow,
    MemoryGovernanceAuditRow,
)


def test_packet_filters_tenant_and_never_echoes_log_body(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    rows = [
        {
            "timestamp": "2026-09-24T09:00:00+00:00",
            "trace_id": "trace-a", "tenant_id": "tenant-a", "stage": "vision",
            "operation": "extract", "outcome": "success",
            "message": "invoice secret should never appear",
        },
        {
            "timestamp": "2026-09-24T09:00:01+00:00",
            "trace_id": "trace-a", "tenant_id": "tenant-b", "stage": "vision",
            "outcome": "failed", "error_code": "other_tenant_error",
        },
        {
            "timestamp": "2026-09-24T09:00:02+00:00",
            "trace_id": "trace-a", "tenant_id": "tenant-a", "stage": "validation",
            "outcome": "failed", "error_code": "schema_invalid",
            "exception": "API_KEY=secret", "error_type": "ValueError",
        },
    ]
    log.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    events, matched, truncated = collect_trace_events(
        log, trace_id="trace-a", tenant_id="tenant-a"
    )
    packet = build_packet(
        trace_id="trace-a", tenant_id="tenant-a", events=events,
        matched=matched, events_omitted=truncated,
    )
    rendered = json.dumps(packet)
    assert matched == 2
    assert packet["first_failure"]["error_code"] == "schema_invalid"
    assert packet["source_hint"].endswith("extraction_validation.py")
    assert "invoice secret" not in rendered
    assert "API_KEY" not in rendered
    assert "other_tenant_error" not in rendered
    narrowed, narrowed_count, _ = collect_trace_events(
        log, trace_id="trace-a", tenant_id="tenant-a",
        since=datetime.fromisoformat("2026-09-24T09:00:02+00:00"),
    )
    assert narrowed_count == 1
    assert narrowed[0]["error_code"] == "schema_invalid"


def test_packet_budget_drops_context_before_failure() -> None:
    events = [
        {"timestamp": "2026-09-24T09:00:00+00:00", "stage": "vision", "operation": "x" * 100}
        for _ in range(100)
    ]
    events[5] = {
        "timestamp": "2026-09-24T09:00:05+00:00",
        "stage": "vision", "outcome": "failed", "error_code": "timeout",
    }
    packet = build_packet(
        trace_id="trace-a", tenant_id="tenant-a", events=events,
        matched=100, events_omitted=False, max_chars=1000,
    )
    assert len(json.dumps(packet, ensure_ascii=False)) <= 1000
    assert packet["first_failure"]["error_code"] == "timeout"


def test_requested_error_after_early_failure_is_selected(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    rows = [
        {
            "timestamp": f"2026-09-24T09:00:{second:02d}+00:00",
            "trace_id": "trace-a", "tenant_id": "tenant-a",
            "stage": "vision", "error_code": (
                "target_error" if second == 40 else "early_error" if second == 2 else None
            ),
        }
        for second in range(50)
    ]
    log.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    events, matched, omitted = collect_trace_events(
        log, trace_id="trace-a", tenant_id="tenant-a", error_code="target_error"
    )
    packet = build_packet(
        trace_id="trace-a", tenant_id="tenant-a", events=events,
        matched=matched, events_omitted=omitted, error_code="target_error",
    )
    assert packet["first_failure"]["error_code"] == "target_error"
    assert packet["requested_error_code_found"] is True
    assert len(events) == 11
    assert len(json.dumps(packet)) < len(log.read_text(encoding="utf-8"))


def test_log_directory_orders_api_worker_and_rotated_events(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    api_dir = log_root / "api"
    worker_dir = log_root / "memory-admission"
    api_dir.mkdir(parents=True)
    worker_dir.mkdir()
    (api_dir / "api-101.jsonl.1").write_text(json.dumps({
        "timestamp": "2026-09-24T09:00:00+00:00", "trace_id": "trace-a",
        "tenant_id": "tenant-a", "stage": "ingestion", "outcome": "success",
    }), encoding="utf-8")
    (worker_dir / "memory-admission-202.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-24T09:00:02+00:00", "trace_id": "trace-a",
        "tenant_id": "tenant-a", "stage": "memory_admission",
        "outcome": "failed", "error_code": "retry_exhausted",
    }), encoding="utf-8")
    (api_dir / "api-101.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-24T09:00:01+00:00", "trace_id": "trace-a",
        "tenant_id": "tenant-a", "stage": "validation", "outcome": "success",
    }), encoding="utf-8")
    events, matched, omitted = collect_trace_events(
        log_root, trace_id="trace-a", tenant_id="tenant-a"
    )
    assert matched == 3
    assert omitted is False
    assert [event["stage"] for event in events] == [
        "ingestion", "validation", "memory_admission",
    ]


def test_cli_emits_only_bounded_json(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps({
        "timestamp": "2026-09-24T09:00:00+00:00",
        "trace_id": "trace-a", "tenant_id": "tenant-a",
        "stage": "ocr", "outcome": "failed", "error_code": "ocr_timeout",
        "message": "private invoice value",
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "invoice_intelligence.diagnostics", "--log", str(log),
         "--trace-id", "trace-a", "--tenant-id", "tenant-a", "--max-chars", "1000"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert len(result.stdout) <= 1001
    assert json.loads(result.stdout)["first_failure"]["error_code"] == "ocr_timeout"
    assert "private invoice value" not in result.stdout


def test_database_facts_are_tenant_scoped_and_omit_free_text() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            CodeHarnessTaskRow.__table__, CodeHarnessAttemptRow.__table__,
            CodeHarnessPostmortemRow.__table__, CodeHarnessPostmortemSourceEventRow.__table__,
            MemoryGovernanceAuditRow.__table__,
        ],
    )
    now = datetime.now(UTC)
    with Session(engine) as session:
        for tenant in ("tenant-a", "tenant-b"):
            session.add(CodeHarnessTaskRow(
                task_id=f"task-{tenant}", tenant_id=tenant, trace_id="trace-a",
                repository_id="repo-1", request_fingerprint="fingerprint",
                idempotency_key_hash=f"hash-{tenant}", status="failed", revision=1,
                attempt_count=1, versions_json={"model_version": "model-1"},
                budget_json={}, failure_code="timeout", created_at=now, updated_at=now,
            ))
        session.add(CodeHarnessAttemptRow(
            attempt_id="attempt-a", task_id="task-tenant-a", attempt_number=1,
            revision=1, worker_id="worker", lease_token="lease", started_at=now,
        ))
        session.add(CodeHarnessPostmortemRow(
            postmortem_id="postmortem-a", tenant_id="tenant-a", fingerprint="fingerprint",
            version_scope="scope", occurrence_count=1, admission_status="pending",
            error_signature="timeout", root_cause="private free text",
            source_trace_id="older-trace", source_event_ids_json=["event-a"], revision=1,
            created_at=now, updated_at=now,
        ))
        session.add(CodeHarnessPostmortemSourceEventRow(
            event_id="event-a", tenant_id="tenant-a", postmortem_id="postmortem-a",
            fingerprint="fingerprint", version_scope="scope", source_type="task",
            payload_summary="private source text", payload_checksum_sha256="0" * 64,
            source_task_id="task-tenant-a", created_at=now,
        ))
        session.add(MemoryGovernanceAuditRow(
            audit_id="audit-a", tenant_id="tenant-a", action="approve_admission",
            resource_type="example", resource_id="example-a", reviewer_id="reviewer",
            reason="private audit reason", idempotency_key_hash="hash-a",
            trace_id="trace-a", created_at=now,
        ))
        session.commit()
    with patch("invoice_intelligence.diagnostics.facts.create_engine", return_value=engine):
        result = load_facts("unused", tenant_id="tenant-a", trace_id="trace-a")
    rendered = json.dumps(result)
    assert result["harness_tasks"][0]["task_id"] == "task-tenant-a"
    assert result["postmortems"][0]["postmortem_id"] == "postmortem-a"
    assert "task-tenant-b" not in rendered
    assert "private free text" not in rendered
    assert "private audit reason" not in rendered
    assert "private source text" not in rendered
