"""Persistence boundary for production human-review tasks."""

from datetime import datetime
from typing import Protocol

from invoice_intelligence.domain.review_tasks import (
    ReviewTask,
    ReviewTaskAudit,
    ReviewTaskCursor,
    ReviewTaskFilter,
    ReviewTaskPage,
)


class ReviewTaskRepository(Protocol):
    async def list_review_tasks(
        self,
        tenant_id: str,
        filters: ReviewTaskFilter,
        *,
        limit: int,
        cursor: ReviewTaskCursor | None,
    ) -> ReviewTaskPage: ...

    async def get_review_task(
        self,
        tenant_id: str,
        identifier: str,
    ) -> ReviewTask | None: ...

    async def claim_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        reviewer_id: str,
        expected_revision: int,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask: ...

    async def release_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        reviewer_id: str,
        lease_token: str,
        expected_revision: int,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask: ...

    async def reassign_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        actor_id: str,
        target_reviewer_id: str,
        lease_token: str,
        expected_revision: int,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask: ...

    async def cancel_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        expected_revision: int,
        reason: str,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask: ...

    async def expire_due_review_tasks(
        self,
        tenant_id: str,
        *,
        now: datetime,
        actor_id: str,
        trace_id: str | None,
        limit: int,
    ) -> tuple[ReviewTask, ...]: ...

    async def mark_review_submitted(
        self,
        tenant_id: str,
        run_id: str,
        *,
        reviewer_id: str,
        now: datetime,
        trace_id: str | None,
    ) -> ReviewTask: ...

    async def count_review_audits(
        self,
        tenant_id: str,
        review_id: str,
        action: str,
    ) -> int: ...
