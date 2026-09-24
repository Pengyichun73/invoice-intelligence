"""Tenant-scoped SQLAlchemy repository with revision and lease fencing."""

import asyncio
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

from sqlalchemy import Engine, and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.execution import ExecutionBudget, HarnessAttempt, HarnessTask, HarnessTaskStatus
from ...domain.versions import ExecutionVersionBinding
from ...application.ports.task_repository import ClaimedHarnessTask, HarnessTaskRepository
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CodeHarnessAttemptRow,
    CodeHarnessTaskRow,
)


class SQLAlchemyHarnessTaskRepository(HarnessTaskRepository):
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def create_task(self, task: HarnessTask, *, idempotency_key_hash: str) -> HarnessTask:
        return await asyncio.to_thread(self._create_task, task, idempotency_key_hash)

    def _create_task(self, task: HarnessTask, idempotency_key_hash: str) -> HarnessTask:
        values = _task_values(task, idempotency_key_hash)
        try:
            with self._sessions.begin() as session:
                session.add(CodeHarnessTaskRow(**values))
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(CodeHarnessTaskRow).where(
                        CodeHarnessTaskRow.tenant_id == task.tenant_id,
                        CodeHarnessTaskRow.idempotency_key_hash == idempotency_key_hash,
                    )
                )
                if existing is None or _task_values(task, idempotency_key_hash)["request_fingerprint"] != existing.request_fingerprint:
                    raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT) from None
                return _to_task(existing)
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc
        return task

    async def get_task(self, *, tenant_id: str, task_id: str) -> HarnessTask | None:
        return await asyncio.to_thread(self._get_task, tenant_id, task_id)

    def _get_task(self, tenant_id: str, task_id: str) -> HarnessTask | None:
        with self._sessions() as session:
            row = session.scalar(
                select(CodeHarnessTaskRow).where(
                    CodeHarnessTaskRow.tenant_id == tenant_id,
                    CodeHarnessTaskRow.task_id == task_id,
                )
            )
            return _to_task(row) if row else None

    async def save_attempt(self, attempt: HarnessAttempt) -> None:
        await asyncio.to_thread(self._save_attempt, attempt)

    async def complete_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        revision: int,
        worker_id: str,
        lease_token: str,
        completed_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._complete_attempt,
            attempt_id,
            task_id,
            revision,
            worker_id,
            lease_token,
            completed_at,
        )

    def _complete_attempt(
        self,
        attempt_id: str,
        task_id: str,
        revision: int,
        worker_id: str,
        lease_token: str,
        completed_at: datetime,
    ) -> None:
        if not attempt_id or not task_id or not worker_id or not lease_token:
            raise ValueError("attempt fencing fields must be valid")
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(CodeHarnessAttemptRow)
                    .where(
                        CodeHarnessAttemptRow.attempt_id == attempt_id,
                        CodeHarnessAttemptRow.task_id == task_id,
                        CodeHarnessAttemptRow.revision == revision,
                        CodeHarnessAttemptRow.worker_id == worker_id,
                        CodeHarnessAttemptRow.lease_token == lease_token,
                        CodeHarnessAttemptRow.completed_at.is_(None),
                    )
                    .values(completed_at=completed_at)
                )
                if result.rowcount != 1:
                    raise HarnessError(HarnessErrorCode.LEASE_FENCING_REJECTED)
        except HarnessError:
            raise
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> ClaimedHarnessTask | None:
        return await asyncio.to_thread(
            self._claim_next,
            worker_id,
            lease_seconds,
            now,
        )

    def _claim_next(
        self,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> ClaimedHarnessTask | None:
        if not worker_id or lease_seconds <= 0:
            raise ValueError("worker_id and lease_seconds must be valid")
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(CodeHarnessTaskRow)
                    .where(
                        CodeHarnessTaskRow.status.in_(
                            (
                                HarnessTaskStatus.PENDING.value,
                                HarnessTaskStatus.REPAIR_PENDING.value,
                            )
                        ),
                        or_(
                            CodeHarnessTaskRow.lease_expires_at.is_(None),
                            CodeHarnessTaskRow.lease_expires_at <= now,
                        ),
                        or_(
                            CodeHarnessTaskRow.next_attempt_at.is_(None),
                            CodeHarnessTaskRow.next_attempt_at <= now,
                        ),
                    )
                    .order_by(CodeHarnessTaskRow.created_at)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                revision = row.revision + 1
                attempt_number = row.attempt_count + 1
                lease_token = uuid4().hex
                row.status = HarnessTaskStatus.RUNNING.value
                row.revision = revision
                row.attempt_count = attempt_number
                row.worker_id = worker_id
                row.lease_token = lease_token
                row.lease_expires_at = lease_expires_at
                row.next_attempt_at = None
                row.updated_at = now
                attempt = HarnessAttempt(
                    attempt_id=uuid4().hex,
                    task_id=row.task_id,
                    attempt_number=attempt_number,
                    revision=revision,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    started_at=now,
                )
                session.add(
                    CodeHarnessAttemptRow(
                        attempt_id=attempt.attempt_id,
                        task_id=attempt.task_id,
                        attempt_number=attempt.attempt_number,
                        revision=attempt.revision,
                        worker_id=attempt.worker_id,
                        lease_token=attempt.lease_token,
                        started_at=attempt.started_at,
                    )
                )
                session.flush()
                return ClaimedHarnessTask(task=_to_task(row), attempt=attempt)
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT) from exc
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

    async def renew_claim(
        self,
        *,
        task_id: str,
        expected_revision: int,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime,
    ) -> HarnessTask:
        return await asyncio.to_thread(
            self._renew_claim,
            task_id,
            expected_revision,
            worker_id,
            lease_token,
            lease_seconds,
            now,
        )

    def _renew_claim(
        self,
        task_id: str,
        expected_revision: int,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime,
    ) -> HarnessTask:
        if not worker_id or not lease_token or lease_seconds <= 0:
            raise ValueError("worker_id, lease_token and lease_seconds must be valid")
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(CodeHarnessTaskRow)
                    .where(
                        CodeHarnessTaskRow.task_id == task_id,
                        CodeHarnessTaskRow.revision == expected_revision,
                        CodeHarnessTaskRow.status == HarnessTaskStatus.RUNNING.value,
                        CodeHarnessTaskRow.worker_id == worker_id,
                        CodeHarnessTaskRow.lease_token == lease_token,
                        CodeHarnessTaskRow.lease_expires_at > now,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise HarnessError(HarnessErrorCode.LEASE_FENCING_REJECTED)
                row = session.scalar(
                    select(CodeHarnessTaskRow).where(
                        CodeHarnessTaskRow.task_id == task_id,
                    )
                )
                if row is None:
                    raise HarnessError(HarnessErrorCode.TASK_NOT_FOUND)
                return _to_task(row)
        except HarnessError:
            raise
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

    def _save_attempt(self, attempt: HarnessAttempt) -> None:
        try:
            with self._sessions.begin() as session:
                session.add(
                    CodeHarnessAttemptRow(
                        attempt_id=attempt.attempt_id,
                        task_id=attempt.task_id,
                        attempt_number=attempt.attempt_number,
                        revision=attempt.revision,
                        worker_id=attempt.worker_id,
                        lease_token=attempt.lease_token,
                        started_at=attempt.started_at,
                        completed_at=attempt.completed_at,
                    )
                )
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT) from exc

    async def update_task(
        self,
        task: HarnessTask,
        *,
        expected_revision: int,
        lease_token: str | None,
    ) -> HarnessTask:
        return await asyncio.to_thread(self._update_task, task, expected_revision, lease_token)

    def _update_task(
        self,
        task: HarnessTask,
        expected_revision: int,
        lease_token: str | None,
    ) -> HarnessTask:
        conditions = [
            CodeHarnessTaskRow.task_id == task.task_id,
            CodeHarnessTaskRow.tenant_id == task.tenant_id,
            CodeHarnessTaskRow.revision == expected_revision,
        ]
        if lease_token is not None:
            conditions.append(CodeHarnessTaskRow.lease_token == lease_token)
        values = _task_values(task, None)
        values.pop("task_id", None)
        values.pop("idempotency_key_hash", None)
        values.pop("worker_id", None)
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(CodeHarnessTaskRow)
                    .where(*conditions)
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise HarnessError(HarnessErrorCode.LEASE_FENCING_REJECTED)
        except HarnessError:
            raise
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc
        return task


def _task_values(task: HarnessTask, idempotency_key_hash: str | None) -> dict[str, object]:
    values: dict[str, object] = {
        "task_id": task.task_id,
        "tenant_id": task.tenant_id,
        "trace_id": task.trace_id,
        "repository_id": task.repository_id,
        "request_fingerprint": task.request_fingerprint,
        "status": task.status.value,
        "revision": task.revision,
        "attempt_count": task.attempt_count,
        "versions_json": task.versions.as_dict(),
        "budget_json": {
            name: getattr(task.budget, name) for name in task.budget.__dataclass_fields__
        },
        "failure_code": task.failure_code.value if task.failure_code else None,
        "next_attempt_at": task.next_attempt_at,
        "worker_id": None,
        "lease_token": task.lease_token,
        "lease_expires_at": task.lease_expires_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    if idempotency_key_hash is not None:
        values["idempotency_key_hash"] = idempotency_key_hash
    return values


def _to_task(row: CodeHarnessTaskRow) -> HarnessTask:
    return HarnessTask(
        task_id=row.task_id,
        tenant_id=row.tenant_id,
        trace_id=row.trace_id,
        repository_id=row.repository_id,
        request_fingerprint=row.request_fingerprint,
        status=HarnessTaskStatus(row.status),
        revision=row.revision,
        attempt_count=row.attempt_count,
        versions=ExecutionVersionBinding(**cast(dict[str, str], row.versions_json)),
        budget=ExecutionBudget(**cast(dict[str, int], row.budget_json)),
        failure_code=HarnessErrorCode(row.failure_code) if row.failure_code else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        next_attempt_at=row.next_attempt_at,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
    )
