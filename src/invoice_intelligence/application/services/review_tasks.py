"""Application use cases for lease-based human review tasks."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from uuid import uuid4

from invoice_intelligence.application.errors import (
    BadRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.ports.business_persistence import ExtractionRunRecord
from invoice_intelligence.application.ports.review_tasks import ReviewTaskRepository
from invoice_intelligence.application.services.extraction_workflow import (
    ExtractionWorkflowService,
)
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.review_tasks import (
    ReviewTask,
    ReviewTaskAudit,
    ReviewTaskAuditAction,
    ReviewTaskCursor,
    ReviewTaskFilter,
    ReviewTaskPage,
    ReviewTaskStatus,
)
from invoice_intelligence.domain.workflow import HumanCorrection


@dataclass(frozen=True, slots=True)
class ReviewTaskServiceConfig:
    default_lease_seconds: int = 900
    maximum_lease_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.default_lease_seconds <= 0:
            raise ValueError("Default review lease must be positive")
        if self.maximum_lease_seconds < self.default_lease_seconds:
            raise ValueError("Maximum review lease cannot be lower than default")


@dataclass(frozen=True, slots=True)
class ReviewSubmissionResult:
    task: ReviewTask
    run: ExtractionRunRecord


class ReviewTaskService:
    """Coordinate task ownership while delegating graph execution to its service."""

    def __init__(
        self,
        *,
        repository: ReviewTaskRepository,
        workflow_service: ExtractionWorkflowService,
        config: ReviewTaskServiceConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._workflow = workflow_service
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list_tasks(
        self,
        context: TrustedTenantContext,
        filters: ReviewTaskFilter,
        *,
        limit: int,
        cursor: ReviewTaskCursor | None,
    ) -> ReviewTaskPage:
        return await self._repository.list_review_tasks(
            context.tenant_id, filters, limit=limit, cursor=cursor
        )

    async def get_task(self, context: TrustedTenantContext, identifier: str) -> ReviewTask:
        task = await self._repository.get_review_task(context.tenant_id, identifier)
        if task is None:
            raise ResourceNotFoundError("Review task was not found")
        return task

    async def get_current_extraction(
        self,
        context: TrustedTenantContext,
        task: ReviewTask,
    ) -> dict[str, JsonValue] | None:
        if task.status not in {
            ReviewTaskStatus.PENDING_REVIEW,
            ReviewTaskStatus.CLAIMED,
            ReviewTaskStatus.EXPIRED,
        }:
            return None
        try:
            return await self._workflow.get_review_extraction(task.run_id, context.tenant_id)
        except ResourceConflictError:
            return None

    async def claim(
        self,
        context: TrustedTenantContext,
        identifier: str,
        *,
        expected_revision: int,
        lease_seconds: int | None = None,
    ) -> ReviewTask:
        task = await self.get_task(context, identifier)
        duration = lease_seconds or self._config.default_lease_seconds
        if duration <= 0 or duration > self._config.maximum_lease_seconds:
            raise BadRequestError("Review lease duration is outside configured limits")
        now = self._now()
        token = token_hex(32)
        return await self._repository.claim_review_task(
            context.tenant_id,
            task.review_id,
            reviewer_id=context.actor_id,
            expected_revision=expected_revision,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=duration),
            now=now,
            audit=self._audit(
                context,
                task,
                ReviewTaskAuditAction.CLAIM,
                ReviewTaskStatus.CLAIMED,
                expected_revision + 1,
                "review_claimed",
                target_reviewer_id=context.actor_id,
                now=now,
            ),
        )

    async def release(
        self,
        context: TrustedTenantContext,
        identifier: str,
        *,
        expected_revision: int,
        lease_token: str,
    ) -> ReviewTask:
        task = await self.get_task(context, identifier)
        now = self._now()
        return await self._repository.release_review_task(
            context.tenant_id,
            task.review_id,
            reviewer_id=context.actor_id,
            lease_token=self._normalized_token(lease_token),
            expected_revision=expected_revision,
            now=now,
            audit=self._audit(
                context,
                task,
                ReviewTaskAuditAction.RELEASE,
                ReviewTaskStatus.PENDING_REVIEW,
                expected_revision + 1,
                "review_released",
                now=now,
            ),
        )

    async def reassign(
        self,
        context: TrustedTenantContext,
        identifier: str,
        *,
        target_reviewer_id: str,
        expected_revision: int,
        lease_token: str,
    ) -> ReviewTask:
        task = await self.get_task(context, identifier)
        target = self._normalized_actor(target_reviewer_id)
        now = self._now()
        return await self._repository.reassign_review_task(
            context.tenant_id,
            task.review_id,
            actor_id=context.actor_id,
            target_reviewer_id=target,
            lease_token=self._normalized_token(lease_token),
            expected_revision=expected_revision,
            now=now,
            audit=self._audit(
                context,
                task,
                ReviewTaskAuditAction.REASSIGN,
                ReviewTaskStatus.PENDING_REVIEW,
                expected_revision + 1,
                "review_reassigned",
                target_reviewer_id=target,
                now=now,
            ),
        )

    async def cancel(
        self,
        context: TrustedTenantContext,
        identifier: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> ReviewTask:
        task = await self.get_task(context, identifier)
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 512:
            raise BadRequestError("Review cancellation reason is invalid")
        now = self._now()
        return await self._repository.cancel_review_task(
            context.tenant_id,
            task.review_id,
            expected_revision=expected_revision,
            reason=normalized_reason,
            now=now,
            audit=self._audit(
                context,
                task,
                ReviewTaskAuditAction.CANCEL,
                ReviewTaskStatus.CANCELLED,
                expected_revision + 1,
                "review_cancelled",
                now=now,
            ),
        )

    async def recover_expired(
        self,
        context: TrustedTenantContext,
        *,
        limit: int,
    ) -> tuple[ReviewTask, ...]:
        if not 1 <= limit <= 500:
            raise BadRequestError("Expired review recovery limit is invalid")
        return await self._repository.expire_due_review_tasks(
            context.tenant_id,
            now=self._now(),
            actor_id=context.actor_id,
            trace_id=context.trace_id,
            limit=limit,
        )

    async def submit(
        self,
        context: TrustedTenantContext,
        identifier: str,
        correction: HumanCorrection,
        *,
        expected_revision: int,
        lease_token: str,
        idempotency_key: str | None,
    ) -> ReviewSubmissionResult:
        task = await self.get_task(context, identifier)
        now = self._now()
        if correction.reviewer_id != context.actor_id:
            raise ResourceConflictError("Review actor does not match trusted context")
        if await self._workflow.is_review_submission_registered(
            task.run_id, correction, idempotency_key, context.tenant_id
        ):
            return ReviewSubmissionResult(
                task=task,
                run=await self._workflow.get_run(task.run_id, context.tenant_id),
            )
        if task.status is ReviewTaskStatus.SUBMITTED:
            run = await self._workflow.submit_review(
                task.run_id,
                correction,
                idempotency_key=idempotency_key,
                tenant_id=context.tenant_id,
                trace_id=context.trace_id,
            )
            return ReviewSubmissionResult(task=task, run=run)
        self._require_owned_lease(
            task,
            context.actor_id,
            expected_revision,
            self._normalized_token(lease_token),
            now,
        )
        if correction.reviewer_id != context.actor_id:
            raise ResourceConflictError("Review actor does not match trusted context")
        run = await self._workflow.submit_review(
            task.run_id,
            correction,
            idempotency_key=idempotency_key,
            tenant_id=context.tenant_id,
            trace_id=context.trace_id,
            review_id=task.review_id,
            expected_review_revision=expected_revision,
            review_lease_token=lease_token,
        )
        updated = await self.get_task(context, task.review_id)
        return ReviewSubmissionResult(task=updated, run=run)

    async def submit_legacy(
        self,
        context: TrustedTenantContext,
        run_id: str,
        correction: HumanCorrection,
        *,
        idempotency_key: str | None,
    ) -> ReviewSubmissionResult:
        task = await self.get_task(context, run_id)
        if task.status is ReviewTaskStatus.SUBMITTED:
            run = await self._workflow.submit_review(
                task.run_id,
                correction,
                idempotency_key=idempotency_key,
                tenant_id=context.tenant_id,
                trace_id=context.trace_id,
            )
            return ReviewSubmissionResult(task=task, run=run)
        if task.status in {ReviewTaskStatus.PENDING_REVIEW, ReviewTaskStatus.EXPIRED}:
            task = await self.claim(
                context,
                task.review_id,
                expected_revision=task.revision,
            )
        if task.status is not ReviewTaskStatus.CLAIMED:
            raise ResourceConflictError("Review task is not available for submission")
        if task.assigned_reviewer_id != context.actor_id or task.lease_token is None:
            raise ResourceConflictError("Review task is claimed by another reviewer")
        return await self.submit(
            context,
            task.review_id,
            correction,
            expected_revision=task.revision,
            lease_token=task.lease_token,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _require_owned_lease(
        task: ReviewTask,
        actor_id: str,
        expected_revision: int,
        lease_token: str,
        now: datetime,
    ) -> None:
        if task.revision != expected_revision:
            raise ResourceConflictError("Review task revision changed")
        if (
            task.status is not ReviewTaskStatus.CLAIMED
            or task.assigned_reviewer_id != actor_id
            or task.lease_token != lease_token
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            raise ResourceConflictError("Review task lease is not owned or has expired")

    @staticmethod
    def _audit(
        context: TrustedTenantContext,
        task: ReviewTask,
        action: ReviewTaskAuditAction,
        target_status: ReviewTaskStatus,
        revision: int,
        reason_code: str,
        *,
        target_reviewer_id: str | None = None,
        now: datetime,
    ) -> ReviewTaskAudit:
        return ReviewTaskAudit(
            audit_id=uuid4().hex,
            tenant_id=context.tenant_id,
            review_id=task.review_id,
            action=action,
            actor_id=context.actor_id,
            target_reviewer_id=target_reviewer_id,
            from_status=task.status,
            to_status=target_status,
            revision=revision,
            reason_code=reason_code,
            trace_id=context.trace_id,
            created_at=now,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Review task clock must be timezone-aware")
        return value

    @staticmethod
    def _normalized_token(value: str) -> str:
        normalized = value.strip()
        if len(normalized) != 64:
            raise BadRequestError("Review lease token is invalid")
        return normalized

    @staticmethod
    def _normalized_actor(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise BadRequestError("Target reviewer identifier is invalid")
        return normalized
