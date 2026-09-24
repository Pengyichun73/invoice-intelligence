"""Application ports for memory governance and safe operational telemetry."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from invoice_intelligence.domain.governance import (
    GovernanceAction,
    GovernanceAuditEvent,
    RetrievalFeedback,
    RetrievalMetricSummary,
    RetrievalTrace,
)


@dataclass(frozen=True, slots=True)
class RemoteCallObservation:
    """Sensitive-value-free usage emitted by one remote-call attempt."""

    operation: str
    model: str
    outcome: str
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None


@dataclass(frozen=True, slots=True)
class RemoteCallAggregate:
    """Usage accumulated only inside one retrieval trace context."""

    error_count: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None


class RetrievalTelemetryContext(Protocol):
    """Propagate trace identity to SDK-isolated remote provider adapters."""

    def bind(self, trace_id: str, tenant_id: str) -> AbstractContextManager[None]:
        """Bind one trace for the current async context."""

        ...

    def record_remote_call(self, observation: RemoteCallObservation) -> None:
        """Accumulate usage without recording request or response content."""

        ...

    def snapshot(self, trace_id: str) -> RemoteCallAggregate:
        """Read current usage totals for a trace."""

        ...

    def clear(self, trace_id: str) -> None:
        """Release completed trace-local usage state."""

        ...


class MemoryGovernanceRepository(Protocol):
    """Persist attributable governance facts in PostgreSQL."""

    async def save_audit(self, event: GovernanceAuditEvent) -> GovernanceAuditEvent:
        """Idempotently save one immutable governance audit event."""

        ...

    async def list_audits(
        self,
        tenant_id: str,
        *,
        action: GovernanceAction | None,
        resource_type: str | None,
        resource_id: str | None,
        trace_id: str | None,
        started_at: datetime | None,
        ended_at: datetime | None,
        limit: int,
        after_audit_id: str | None,
    ) -> Sequence[GovernanceAuditEvent]:
        """List safe audit metadata without crossing the tenant boundary."""

        ...

    async def get_audit_by_idempotency_hash(
        self,
        tenant_id: str,
        action: GovernanceAction,
        idempotency_key_hash: str,
    ) -> GovernanceAuditEvent | None:
        """Read the immutable audit used by a safe idempotency replay."""

        ...

    async def save_feedback(
        self,
        feedback: RetrievalFeedback,
        audit_event: GovernanceAuditEvent,
    ) -> RetrievalFeedback:
        """Atomically save one judgment and its immutable governance audit."""

        ...

    async def get_feedback(
        self,
        tenant_id: str,
        feedback_id: str,
    ) -> RetrievalFeedback | None:
        """Read feedback without crossing the trusted tenant boundary."""

        ...


class RetrievalTelemetryRepository(Protocol):
    """Persist redacted retrieval traces and aggregate operational metrics."""

    async def save_trace(self, trace: RetrievalTrace) -> None:
        """Idempotently save one completed retrieval trace."""

        ...

    async def get_trace(
        self,
        tenant_id: str,
        trace_id: str,
    ) -> RetrievalTrace | None:
        """Read one tenant-owned trace for feedback validation."""

        ...

    async def mark_review_required(
        self,
        tenant_id: str,
        trace_ids: Sequence[str],
        review_required: bool,
    ) -> None:
        """Bind the first deterministic validation route to retrieval traces."""

        ...

    async def summarize(
        self,
        tenant_id: str,
        index_version: str,
    ) -> RetrievalMetricSummary:
        """Aggregate safe tenant/index metrics without reading Prompt content."""

        ...
