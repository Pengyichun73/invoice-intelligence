"""Idempotent PostgreSQL postmortem facts and admission transitions."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ...application.ports.postmortem import PostmortemRepository
from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.postmortem import AdmissionStatus, Postmortem, PostmortemSourceEvent
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CodeHarnessAuditRow,
    CodeHarnessPostmortemRow,
    CodeHarnessPostmortemSourceEventRow,
)


class SQLAlchemyPostmortemRepository(PostmortemRepository):
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def record_source_event(self, event: PostmortemSourceEvent) -> Postmortem:
        return await asyncio.to_thread(self._record_source_event, event)

    def _record_source_event(self, event: PostmortemSourceEvent) -> Postmortem:
        try:
            with self._sessions.begin() as session:
                existing_event = session.get(
                    CodeHarnessPostmortemSourceEventRow,
                    event.event_id,
                )
                if existing_event is not None:
                    row = session.get(CodeHarnessPostmortemRow, existing_event.postmortem_id)
                    if row is None:
                        raise HarnessError(HarnessErrorCode.HARD_FAILURE)
                    return _to_postmortem(row)
                row = session.scalar(
                    select(CodeHarnessPostmortemRow)
                    .where(
                        CodeHarnessPostmortemRow.tenant_id == event.tenant_id,
                        CodeHarnessPostmortemRow.fingerprint == event.fingerprint,
                        CodeHarnessPostmortemRow.version_scope == event.version_scope,
                    )
                    .with_for_update()
                )
                now = event.created_at
                if row is None:
                    row = CodeHarnessPostmortemRow(
                        postmortem_id=str(uuid4()),
                        tenant_id=event.tenant_id,
                        fingerprint=event.fingerprint,
                        version_scope=event.version_scope,
                        occurrence_count=1,
                        admission_status=AdmissionStatus.PENDING.value,
                        error_signature=event.error_signature,
                        root_cause=event.root_cause,
                        solution_pattern=event.solution_pattern,
                        affected_language=event.affected_language,
                        affected_symbol_kind=event.affected_symbol_kind,
                        patch_shape=event.patch_shape,
                        source_trace_id=event.source_trace_id,
                        source_event_ids_json=[event.event_id],
                        revision=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    event_ids = list(row.source_event_ids_json)
                    event_ids.append(event.event_id)
                    row.source_event_ids_json = event_ids
                    row.occurrence_count += 1
                    row.revision += 1
                    row.updated_at = now
                session.add(
                    CodeHarnessPostmortemSourceEventRow(
                        event_id=event.event_id,
                        tenant_id=event.tenant_id,
                        postmortem_id=row.postmortem_id,
                        fingerprint=event.fingerprint,
                        version_scope=event.version_scope,
                        source_type=event.source_type,
                        payload_summary=event.payload_summary,
                        payload_checksum_sha256=event.payload_checksum_sha256,
                        source_repository_id=event.source_repository_id,
                        source_snapshot_id=event.source_snapshot_id,
                        source_revision=event.source_revision,
                        source_task_id=event.source_task_id,
                        source_patch_id=event.source_patch_id,
                        source_execution_id=event.source_execution_id,
                        created_at=event.created_at,
                    )
                )
                return _to_postmortem(row)
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT) from exc
        except HarnessError:
            raise
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

    async def get(self, *, tenant_id: str, postmortem_id: str) -> Postmortem | None:
        return await asyncio.to_thread(self._get, tenant_id, postmortem_id)

    def _get(self, tenant_id: str, postmortem_id: str) -> Postmortem | None:
        with self._sessions() as session:
            row = session.scalar(
                select(CodeHarnessPostmortemRow).where(
                    CodeHarnessPostmortemRow.tenant_id == tenant_id,
                    CodeHarnessPostmortemRow.postmortem_id == postmortem_id,
                )
            )
            return _to_postmortem(row) if row else None

    async def set_admission_status(
        self,
        *,
        postmortem_id: str,
        expected_revision: int,
        status: AdmissionStatus,
        actor_id: str,
        reason_code: str,
    ) -> Postmortem:
        return await asyncio.to_thread(
            self._set_admission_status,
            postmortem_id,
            expected_revision,
            status,
            actor_id,
            reason_code,
        )

    def _set_admission_status(
        self,
        postmortem_id: str,
        expected_revision: int,
        status: AdmissionStatus,
        actor_id: str,
        reason_code: str,
    ) -> Postmortem:
        with self._sessions.begin() as session:
            row = session.scalar(
                select(CodeHarnessPostmortemRow)
                .where(CodeHarnessPostmortemRow.postmortem_id == postmortem_id)
                .with_for_update()
            )
            if row is None:
                raise HarnessError(HarnessErrorCode.TASK_NOT_FOUND)
            if row.revision != expected_revision:
                raise HarnessError(HarnessErrorCode.REVISION_CONFLICT)
            row.admission_status = status.value
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.add(
                CodeHarnessAuditRow(
                    audit_id=str(uuid4()),
                    tenant_id=row.tenant_id,
                    resource_type="postmortem",
                    resource_id=row.postmortem_id,
                    actor_id=actor_id,
                    action="admission_status_changed",
                    reason_code=reason_code,
                    revision=row.revision,
                    created_at=row.updated_at,
                )
            )
            return _to_postmortem(row)


def _to_postmortem(row: CodeHarnessPostmortemRow) -> Postmortem:
    return Postmortem(
        postmortem_id=row.postmortem_id,
        tenant_id=row.tenant_id,
        fingerprint=row.fingerprint,
        version_scope=row.version_scope,
        occurrence_count=row.occurrence_count,
        admission_status=AdmissionStatus(row.admission_status),
        source_event_ids=tuple(row.source_event_ids_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
        error_signature=row.error_signature,
        root_cause=row.root_cause,
        solution_pattern=row.solution_pattern,
        affected_language=row.affected_language,
        affected_symbol_kind=row.affected_symbol_kind,
        patch_shape=row.patch_shape,
        source_trace_id=row.source_trace_id,
        revision=row.revision,
    )
