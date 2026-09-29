"""Durable execution requests for the existing invoice workflow."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from invoice_intelligence.application.ports.business_persistence import ExtractionRunRecord
from invoice_intelligence.domain.workflow import HumanCorrection, WorkflowIdentity


@dataclass(frozen=True, slots=True)
class ExtractionWorkLease:
    task_id: str
    identity: WorkflowIdentity
    tenant_id: str
    kind: str
    correction: HumanCorrection | None
    trace_id: str | None
    checkpoint_id: str | None
    claim_token: str
    lease_expires_at: datetime
    attempt_count: int


class ExtractionQueueRepository(Protocol):
    async def enqueue_start(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        *,
        idempotency_hash: str | None,
        request_hash: str,
        trace_id: str | None,
        actor_id: str | None = None,
    ) -> ExtractionRunRecord:
        ...

    async def enqueue_resume(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        correction: HumanCorrection,
        *,
        idempotency_hash: str,
        request_hash: str,
        trace_id: str | None,
        checkpoint_id: str,
        review_id: str,
        expected_revision: int,
        lease_token: str,
    ) -> ExtractionRunRecord:
        ...

    async def has_resume(
        self,
        run_id: str,
        tenant_id: str,
        *,
        idempotency_hash: str,
        request_hash: str,
    ) -> bool:
        ...

    async def claim(self, worker_id: str, lease_seconds: int) -> ExtractionWorkLease | None:
        ...

    async def renew(self, lease: ExtractionWorkLease, lease_seconds: int) -> bool:
        ...

    async def finish(self, lease: ExtractionWorkLease) -> bool:
        ...

    async def retry(
        self,
        lease: ExtractionWorkLease,
        *,
        error_code: str,
        max_attempts: int,
        delay_seconds: int,
    ) -> bool:
        ...
