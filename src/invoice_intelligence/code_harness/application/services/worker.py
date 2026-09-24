"""PostgreSQL-backed worker for one bounded Harness execution."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.execution import HarnessTask, HarnessTaskStatus
from ...domain.postmortem import PostmortemSourceEvent
from ...domain.versions import ExecutionVersionBinding
from ...workflow.service import HarnessWorkflowService
from ...workflow.state import HarnessStage, HarnessState
from ..ports.postmortem import PostmortemRepository
from ..ports.task_repository import HarnessTaskRepository


@dataclass(frozen=True, slots=True)
class HarnessWorkerConfig:
    worker_id: str
    lease_seconds: int = 300
    backoff_base_seconds: int = 2
    backoff_max_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.worker_id or self.worker_id != self.worker_id.strip():
            raise ValueError("worker_id must be normalized")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.backoff_base_seconds <= 0 or self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff bounds are invalid")


class HarnessWorkerService:
    """Claims one task, runs the fixed workflow, and writes only fenced state."""

    def __init__(
        self,
        *,
        repository: HarnessTaskRepository,
        workflow: HarnessWorkflowService,
        config: HarnessWorkerConfig,
        clock: Callable[[], datetime] | None = None,
        postmortems: PostmortemRepository | None = None,
    ) -> None:
        self._repository = repository
        self._workflow = workflow
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._postmortems = postmortems

    async def execute_one(self) -> HarnessTask | None:
        claimed = await self._repository.claim_next(
            worker_id=self._config.worker_id,
            lease_seconds=self._config.lease_seconds,
            now=self._clock(),
        )
        if claimed is None:
            return None

        task = claimed.task
        try:
            result = await asyncio.wait_for(
                self._workflow.run(
                    HarnessState(
                        task_id=task.task_id,
                        tenant_id=task.tenant_id,
                        trace_id=task.trace_id,
                        repository_id=task.repository_id,
                        snapshot_id=None,
                        snapshot_revision=None,
                        versions=task.versions,
                        budget=task.budget,
                        attempt_count=task.attempt_count,
                        stage=HarnessStage.PREPARE_TASK,
                    )
                ),
                timeout=task.budget.max_wall_time_seconds,
            )
        except HarnessError as exc:
            result = _failed_state(task, exc.code)
        except (OSError, TimeoutError, ValueError):
            result = _failed_state(task, HarnessErrorCode.HARD_FAILURE)

        now = self._clock()
        status = result.status
        if status is HarnessTaskStatus.REPAIR_PENDING:
            status = HarnessTaskStatus.PENDING
        if status not in {
            HarnessTaskStatus.PENDING,
            HarnessTaskStatus.SUCCEEDED,
            HarnessTaskStatus.FAILED,
            HarnessTaskStatus.QUARANTINED,
        }:
            status = HarnessTaskStatus.QUARANTINED

        retry_at = None
        if status is HarnessTaskStatus.PENDING:
            retry_at = now + timedelta(seconds=self._backoff(task.attempt_count))

        updated = HarnessTask(
            task_id=task.task_id,
            tenant_id=task.tenant_id,
            trace_id=task.trace_id,
            repository_id=task.repository_id,
            request_fingerprint=task.request_fingerprint,
            status=status,
            revision=task.revision,
            attempt_count=task.attempt_count,
            versions=task.versions,
            budget=task.budget,
            failure_code=(
                None
                if status is HarnessTaskStatus.SUCCEEDED
                else result.error_code or HarnessErrorCode.HARD_FAILURE
            ),
            created_at=task.created_at,
            updated_at=now,
            lease_token=None,
            lease_expires_at=None,
            next_attempt_at=retry_at,
        )
        persisted = await self._repository.update_task(
            updated,
            expected_revision=task.revision,
            lease_token=claimed.attempt.lease_token,
        )
        try:
            await self._repository.complete_attempt(
                attempt_id=claimed.attempt.attempt_id,
                task_id=task.task_id,
                revision=claimed.attempt.revision,
                worker_id=claimed.attempt.worker_id,
                lease_token=claimed.attempt.lease_token,
                completed_at=now,
            )
        except (HarnessError, OSError, TimeoutError, ValueError):
            # Task fencing remains authoritative; an incomplete attempt is auditable.
            pass
        if persisted.status is HarnessTaskStatus.SUCCEEDED and result.error_signatures:
            await self._record_postmortem(task, result)
        return persisted

    def _backoff(self, attempt_count: int) -> int:
        exponent = max(attempt_count - 1, 0)
        return min(
            self._config.backoff_max_seconds,
            self._config.backoff_base_seconds * (2**exponent),
        )

    async def _record_postmortem(self, task: HarnessTask, state: HarnessState) -> None:
        if self._postmortems is None:
            return
        version_scope = _version_scope(task.versions)
        signature = state.error_signatures[-1]
        fingerprint = sha256(
            f"{task.request_fingerprint}|{signature}|{version_scope}".encode("utf-8")
        ).hexdigest()
        event = PostmortemSourceEvent(
            event_id=f"harness:{task.task_id}:{task.attempt_count}",
            tenant_id=task.tenant_id,
            fingerprint=fingerprint,
            version_scope=version_scope,
            source_type="harness_success",
            payload_summary="bounded self-healing execution completed",
            payload_checksum_sha256=sha256(
                f"{task.task_id}|{signature}".encode("utf-8")
            ).hexdigest(),
            created_at=self._clock(),
            error_signature=signature,
            root_cause=_root_cause(state.error_code),
            solution_pattern=_solution_pattern(state),
            affected_language=state.affected_language,
            affected_symbol_kind=state.affected_symbol_kind,
            patch_shape=state.patch_shape,
            source_trace_id=state.trace_id,
            source_repository_id=state.repository_id,
            source_snapshot_id=state.snapshot_id,
            source_revision=state.source_revision,
            source_task_id=state.task_id,
            source_patch_id=state.patch_id,
            source_execution_id=_execution_id(task.task_id, task.attempt_count),
        )
        try:
            await self._postmortems.record_source_event(event)
        except (HarnessError, OSError, TimeoutError, ValueError):
            return


def _failed_state(task: HarnessTask, code: HarnessErrorCode) -> HarnessState:
    return HarnessState(
            task_id=task.task_id,
            tenant_id=task.tenant_id,
            trace_id=task.trace_id,
            repository_id=task.repository_id,
        snapshot_id=None,
        snapshot_revision=None,
        versions=task.versions,
        budget=task.budget,
        attempt_count=task.attempt_count,
        stage=HarnessStage.REPAIR_OR_FINISH,
        status=HarnessTaskStatus.QUARANTINED,
        error_code=code,
    )


def _version_scope(versions: ExecutionVersionBinding) -> str:
    return "|".join(
        f"{name}={getattr(versions, name)}"
        for name in versions.__dataclass_fields__
    )


def _root_cause(code: HarnessErrorCode | None) -> str:
    if code is None:
        return "execution_failure_signature"
    return f"deterministic_failure:{code.value}"


def _solution_pattern(state: HarnessState) -> str:
    if state.status is HarnessTaskStatus.SUCCEEDED:
        return "validated_patch_then_sandbox_success"
    return "bounded_retry_with_watchdog"
