"""Application entrypoint for creating and resuming Harness tasks."""

from hashlib import sha256
from datetime import UTC, datetime
from uuid import uuid4

from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.execution import ExecutionBudget, HarnessTask, HarnessTaskStatus
from ...domain.tenant_boundary import HarnessTenantContext
from ...domain.versions import ExecutionVersionBinding
from ..ports.task_repository import HarnessTaskRepository


class CodeHarnessService:
    def __init__(self, repository: HarnessTaskRepository) -> None:
        self._repository = repository

    async def create_task(
        self,
        *,
        context: HarnessTenantContext,
        repository_id: str,
        request_fingerprint: str,
        versions: ExecutionVersionBinding,
        budget: ExecutionBudget | None = None,
        idempotency_key: str,
    ) -> HarnessTask:
        if not idempotency_key or idempotency_key != idempotency_key.strip():
            raise ValueError("idempotency_key must be normalized")
        now = datetime.now(UTC)
        task = HarnessTask(
            task_id=str(uuid4()),
            tenant_id=context.tenant_id,
            trace_id=context.trace_id,
            repository_id=repository_id,
            request_fingerprint=request_fingerprint,
            status=HarnessTaskStatus.PENDING,
            revision=1,
            attempt_count=0,
            versions=versions,
            budget=budget or ExecutionBudget(),
            failure_code=None,
            created_at=now,
            updated_at=now,
        )
        key_hash = sha256(idempotency_key.encode("utf-8")).hexdigest()
        return await self._repository.create_task(task, idempotency_key_hash=key_hash)

    async def get_task(
        self,
        *,
        context: HarnessTenantContext,
        task_id: str,
    ) -> HarnessTask:
        task = await self._repository.get_task(tenant_id=context.tenant_id, task_id=task_id)
        if task is None:
            raise HarnessError(HarnessErrorCode.TASK_NOT_FOUND, "task not found")
        context.assert_tenant(task.tenant_id)
        return task

    async def resume_task(
        self,
        *,
        context: HarnessTenantContext,
        task_id: str,
        expected_revision: int,
    ) -> HarnessTask:
        task = await self.get_task(context=context, task_id=task_id)
        if task.revision != expected_revision:
            raise HarnessError(HarnessErrorCode.REVISION_CONFLICT)
        if task.status in {
            HarnessTaskStatus.SUCCEEDED,
            HarnessTaskStatus.FAILED,
            HarnessTaskStatus.QUARANTINED,
        }:
            return task
        now = datetime.now(UTC)
        resumed = HarnessTask(
            task_id=task.task_id,
            tenant_id=task.tenant_id,
            trace_id=task.trace_id,
            repository_id=task.repository_id,
            request_fingerprint=task.request_fingerprint,
            status=HarnessTaskStatus.PENDING,
            revision=task.revision + 1,
            attempt_count=task.attempt_count,
            versions=task.versions,
            budget=task.budget,
            failure_code=None,
            created_at=task.created_at,
            updated_at=now,
        )
        return await self._repository.update_task(
            resumed,
            expected_revision=expected_revision,
            lease_token=task.lease_token,
        )
