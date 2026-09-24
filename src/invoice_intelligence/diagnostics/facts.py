"""Tenant-scoped, read-only fact summary for a trusted local operator."""

from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session

from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CodeHarnessAttemptRow,
    CodeHarnessPostmortemRow,
    CodeHarnessPostmortemSourceEventRow,
    CodeHarnessTaskRow,
    MemoryGovernanceAuditRow,
)

from .report import safe_identifier


def _id(value: object) -> str | None:
    return safe_identifier(value)


def load_facts(database_url: str, *, tenant_id: str, trace_id: str) -> dict[str, object]:
    """Query only selected metadata; never read raw invoice, patch or exception payloads."""
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            tasks = session.execute(
                select(
                    CodeHarnessTaskRow.task_id,
                    CodeHarnessTaskRow.repository_id,
                    CodeHarnessTaskRow.status,
                    CodeHarnessTaskRow.revision,
                    CodeHarnessTaskRow.attempt_count,
                    CodeHarnessTaskRow.failure_code,
                    CodeHarnessTaskRow.versions_json,
                )
                .where(
                    CodeHarnessTaskRow.tenant_id == tenant_id,
                    CodeHarnessTaskRow.trace_id == trace_id,
                )
                .order_by(CodeHarnessTaskRow.updated_at.desc())
                .limit(3)
            ).mappings().all()
            task_summaries: list[dict[str, object]] = []
            task_ids = [task["task_id"] for task in tasks]
            for task in tasks:
                attempts = session.execute(
                    select(
                        CodeHarnessAttemptRow.attempt_number,
                        CodeHarnessAttemptRow.started_at,
                        CodeHarnessAttemptRow.completed_at,
                    )
                    .where(CodeHarnessAttemptRow.task_id == task["task_id"])
                    .order_by(CodeHarnessAttemptRow.attempt_number.desc())
                    .limit(2)
                ).mappings().all()
                task_summaries.append({
                    "task_id": _id(task["task_id"]),
                    "repository_id": _id(task["repository_id"]),
                    "status": _id(task["status"]),
                    "revision": task["revision"],
                    "attempt_count": task["attempt_count"],
                    "failure_code": _id(task["failure_code"]),
                    "versions": {
                        key: safe_identifier(value, limit=128)
                        for key, value in task["versions_json"].items()
                        if safe_identifier(key, limit=64) and safe_identifier(value, limit=128)
                    },
                    "recent_attempts": [
                        {
                            "attempt_number": attempt["attempt_number"],
                            "started_at": attempt["started_at"].isoformat(),
                            "completed_at": (
                                attempt["completed_at"].isoformat()
                                if attempt["completed_at"] else None
                            ),
                        }
                        for attempt in attempts
                    ],
                })
            linked_postmortem_ids: list[str] = []
            if task_ids:
                linked_postmortem_ids = list(session.scalars(
                    select(CodeHarnessPostmortemSourceEventRow.postmortem_id)
                    .where(
                        CodeHarnessPostmortemSourceEventRow.tenant_id == tenant_id,
                        CodeHarnessPostmortemSourceEventRow.source_task_id.in_(task_ids),
                    )
                    .order_by(CodeHarnessPostmortemSourceEventRow.created_at.desc())
                    .limit(2)
                ))
            postmortems = session.execute(
                select(
                    CodeHarnessPostmortemRow.postmortem_id,
                    CodeHarnessPostmortemRow.admission_status,
                    CodeHarnessPostmortemRow.error_signature,
                    CodeHarnessPostmortemRow.occurrence_count,
                )
                .where(
                    CodeHarnessPostmortemRow.tenant_id == tenant_id,
                    or_(
                        CodeHarnessPostmortemRow.source_trace_id == trace_id,
                        CodeHarnessPostmortemRow.postmortem_id.in_(linked_postmortem_ids),
                    ),
                )
                .order_by(CodeHarnessPostmortemRow.updated_at.desc())
                .limit(2)
            ).mappings().all()
            audits = session.execute(
                select(
                    MemoryGovernanceAuditRow.audit_id,
                    MemoryGovernanceAuditRow.action,
                    MemoryGovernanceAuditRow.resource_type,
                    MemoryGovernanceAuditRow.resource_id,
                    MemoryGovernanceAuditRow.resource_version,
                )
                .where(
                    MemoryGovernanceAuditRow.tenant_id == tenant_id,
                    MemoryGovernanceAuditRow.trace_id == trace_id,
                )
                .order_by(MemoryGovernanceAuditRow.created_at.desc())
                .limit(3)
            ).mappings().all()
            return {
                "harness_tasks": task_summaries,
                "postmortems": [
                    {
                        "postmortem_id": _id(row["postmortem_id"]),
                        "admission_status": _id(row["admission_status"]),
                        "error_signature": _id(row["error_signature"]),
                        "occurrence_count": row["occurrence_count"],
                    }
                    for row in postmortems
                ],
                "governance_audits": [
                    {
                        "audit_id": _id(row["audit_id"]),
                        "action": _id(row["action"]),
                        "resource_type": _id(row["resource_type"]),
                        "resource_id": _id(row["resource_id"]),
                        "resource_version": _id(row["resource_version"]),
                    }
                    for row in audits
                ],
            }
    finally:
        engine.dispose()
