"""PostgreSQL 中可重算校验的聚合评估报告产物。"""

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError
from invoice_intelligence.domain.evaluation import EvaluationRun, EvaluationRunStatus
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    EvaluationReportArtifactRow,
    EvaluationRunRow,
)
from invoice_intelligence.infrastructure.reporting.evaluation import EvaluationReportSerializer


class PostgreSQLEvaluationArtifactPublisher:
    """保存聚合 JSON/Markdown，返回绑定内容摘要的不可变引用。"""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def publish(self, run: EvaluationRun) -> tuple[str, ...]:
        return await asyncio.to_thread(self._publish_sync, run)

    def _publish_sync(self, run: EvaluationRun) -> tuple[str, ...]:
        if run.status is not EvaluationRunStatus.COMPLETED:
            raise ValueError("Only completed evaluation reports can be published")
        contents = _report_contents(run)
        with self._sessions.begin() as session:
            run_row = session.scalar(select(EvaluationRunRow).where(
                EvaluationRunRow.tenant_id == run.tenant_id,
                EvaluationRunRow.evaluation_run_id == run.evaluation_run_id,
            ).with_for_update())
            if (
                run_row is None
                or run_row.dataset_id != run.dataset_id
                or run_row.dataset_version != run.dataset_version
                or run_row.schema_version != run.schema_version
                or run_row.status not in {
                    EvaluationRunStatus.RUNNING.value,
                    EvaluationRunStatus.COMPLETED.value,
                }
            ):
                raise ResourceConflictError("Evaluation report has no matching persisted Run")
            references: list[str] = []
            for kind, content in contents:
                artifact_id = _artifact_id(run_row.evaluation_run_key, kind)
                digest = sha256(content.encode("utf-8")).hexdigest()
                row = session.get(EvaluationReportArtifactRow, artifact_id)
                if row is None:
                    session.add(EvaluationReportArtifactRow(
                        artifact_id=artifact_id,
                        evaluation_run_key=run_row.evaluation_run_key,
                        tenant_id=run.tenant_id,
                        report_kind=kind,
                        report_schema_version=run.report_schema_version,
                        content_sha256=digest,
                        content_text=content,
                        created_at=datetime.now(UTC),
                    ))
                elif (
                    row.evaluation_run_key != run_row.evaluation_run_key
                    or row.tenant_id != run.tenant_id
                    or row.report_kind != kind
                    or row.report_schema_version != run.report_schema_version
                    or row.content_sha256 != digest
                    or row.content_text != content
                ):
                    raise ResourceConflictError("Evaluation report artifact is immutable")
                references.append(_reference(artifact_id, digest))
            return tuple(references)


def report_artifacts_match(session: Session, run_row: EvaluationRunRow, run: EvaluationRun) -> bool:
    """在 Job 确认和晋升时，以事实内容重算两种报告的完整性。"""

    if run.status is not EvaluationRunStatus.COMPLETED:
        return False
    contents = _report_contents(run)
    expected: list[str] = []
    for kind, content in contents:
        artifact_id = _artifact_id(run_row.evaluation_run_key, kind)
        row = session.get(EvaluationReportArtifactRow, artifact_id)
        digest = sha256(content.encode("utf-8")).hexdigest()
        if (
            row is None
            or row.evaluation_run_key != run_row.evaluation_run_key
            or row.tenant_id != run.tenant_id
            or row.report_kind != kind
            or row.report_schema_version != run.report_schema_version
            or row.content_sha256 != digest
            or row.content_text != content
        ):
            return False
        expected.append(_reference(artifact_id, digest))
    return run.artifact_references == tuple(expected)


def _report_contents(run: EvaluationRun) -> tuple[tuple[str, str], ...]:
    payload = EvaluationReportSerializer.to_json_payload(run)
    return (
        ("json", json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        ("markdown", EvaluationReportSerializer.to_markdown(run)),
    )


def _artifact_id(run_key: str, kind: str) -> str:
    return sha256(f"evaluation-report:{run_key}:{kind}".encode("utf-8")).hexdigest()


def _reference(artifact_id: str, digest: str) -> str:
    return f"evaluation-report:{artifact_id}:{digest}"
