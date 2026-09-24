"""Correction-memory boundaries; no open-ended agent memory is exposed."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar

from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.extraction import ExtractionResult
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    HumanCorrection,
    JsonValue,
    WorkflowIdentity,
)

InvoiceT = TypeVar("InvoiceT")


@dataclass(frozen=True, slots=True)
class CorrectionMemoryScope:
    """Mandatory exact filters applied before vector similarity."""

    document_type: str
    field_path: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class StoredCorrectionEvent:
    """Stable raw-event identity returned after idempotent persistence."""

    event_id: str
    event: CorrectionEvent


class MemoryRecoveryStatus(StrEnum):
    """Technical lifecycle for retryable reviewed-example materialization."""

    PENDING = "pending"
    FAILED_RETRYABLE = "failed_retryable"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class MemoryRecoveryRecord:
    """PostgreSQL recovery fact; payloads never leave the application boundary."""

    recovery_id: str
    tenant_id: str
    identity: WorkflowIdentity
    document: DocumentReference
    trace_id: str
    status: MemoryRecoveryStatus
    review: HumanCorrection
    correction_events: tuple[StoredCorrectionEvent, ...]
    original_result_payload: dict[str, JsonValue]
    reviewed_result_payload: dict[str, JsonValue]
    example_ids: tuple[str, ...]
    attempt_count: int
    next_attempt_at: datetime | None
    last_error_code: str | None
    last_error_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryRecoveryWorkLease:
    """Atomically claimed recovery work guarded by an expiring lease token."""

    record: MemoryRecoveryRecord
    worker_id: str
    lease_token: str
    lease_expires_at: datetime


class MemoryRecoveryRepository(Protocol[InvoiceT]):
    """Persist review facts and their recovery outbox in one PostgreSQL boundary."""

    async def save_review_facts(
        self,
        *,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
        original_result: ExtractionResult[InvoiceT],
        reviewed_result: ExtractionResult[InvoiceT],
        review: HumanCorrection,
        correction_events: tuple[CorrectionEvent, ...],
    ) -> MemoryRecoveryRecord:
        """Atomically persist immutable review facts and one replay-safe outbox row."""

        ...

    async def get_memory_recovery(
        self,
        tenant_id: str,
        recovery_id: str,
    ) -> MemoryRecoveryRecord | None:
        ...

    async def claim_memory_recovery(
        self,
        tenant_id: str,
        recovery_id: str,
        worker_id: str,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MemoryRecoveryWorkLease | None:
        ...

    async def claim_due_memory_recoveries(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[MemoryRecoveryWorkLease, ...]:
        ...

    async def complete_memory_recovery(
        self,
        lease: MemoryRecoveryWorkLease,
        *,
        example_ids: tuple[str, ...],
        completed_at: datetime,
    ) -> bool:
        ...

    async def retry_memory_recovery(
        self,
        lease: MemoryRecoveryWorkLease,
        *,
        next_attempt_at: datetime | None,
        error_code: str,
        error_at: datetime,
    ) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class CorrectionMemoryMatch:
    """One reviewed and valid vector-memory match."""

    memory_id: str
    event: CorrectionEvent
    similarity: float
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class VectorMemoryUpsert:
    """Redacted derived memory linked to one immutable source event."""

    source_event_id: str
    tenant_id: str
    fingerprint: str
    event: CorrectionEvent
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class CorrectionQueryFacts:
    """Current extraction facts used only to rank scoped historical examples."""

    document_type: str
    field_values: Mapping[str, JsonValue]
    vendor_features: Mapping[str, JsonValue]
    template_features: Mapping[str, JsonValue]


class CorrectionEventRepository(Protocol):
    """Persist original correction audit events in the business database."""

    async def save_correction_events(
        self,
        tenant_id: str,
        identity: WorkflowIdentity,
        document: DocumentReference,
        events: tuple[CorrectionEvent, ...],
    ) -> tuple[StoredCorrectionEvent, ...]:
        """Write tenant-owned immutable raw events idempotently and return stable IDs."""

        ...

    async def get_correction_event(
        self,
        tenant_id: str,
        event_id: str,
        *,
        run_id: str,
        document_id: str,
    ) -> StoredCorrectionEvent | None:
        """Read one immutable event only in its exact tenant/run/document scope."""

        ...


class EmbeddingProvider(Protocol):
    """Create fixed-size vectors without leaking a model SDK into application code."""

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return one embedding per input in the same order."""

        ...


class SensitiveDataRedactor(Protocol):
    """Remove configured sensitive values before vectorization and Prompt use."""

    def redact(self, event: CorrectionEvent) -> CorrectionEvent:
        """Return a sanitized copy while leaving the raw audit event untouched."""

        ...

    def redact_query_facts(self, facts: CorrectionQueryFacts) -> CorrectionQueryFacts:
        """Sanitize current facts before sending them to an embedding provider."""

        ...


class CorrectionScopeResolver(Protocol):
    """Resolve only valid document-type and field-path combinations from a Schema."""

    def resolve(
        self,
        output_schema: type[InvoiceT],
        schema_version: str,
    ) -> tuple[CorrectionMemoryScope, ...]:
        """Return deterministic exact-filter scopes supported by the Schema."""

        ...

    def current_facts(self, invoice: object | None) -> CorrectionQueryFacts | None:
        """Flatten the current concrete invoice variant without inventing values."""

        ...


class VectorMemoryStore(Protocol):
    """Filtered vector-memory store; PostgreSQL/pgvector is the default adapter."""

    async def list_scopes(
        self,
        tenant_id: str,
        document_types: tuple[str, ...],
        schema_version: str,
        limit: int,
    ) -> tuple[CorrectionMemoryScope, ...]:
        """List reviewed, valid scopes for the active Schema only."""

        ...

    async def search(
        self,
        tenant_id: str,
        scope: CorrectionMemoryScope,
        query_embedding: tuple[float, ...],
        limit: int,
        min_similarity: float,
    ) -> tuple[CorrectionMemoryMatch, ...]:
        """Apply all exact scope filters before cosine ordering."""

        ...

    async def upsert(self, records: Sequence[VectorMemoryUpsert]) -> None:
        """Merge duplicate memories and deduplicate replayed source events."""

        ...

    async def disable(self, tenant_id: str, memory_id: str, reason: str) -> None:
        """Disable an incorrect derived memory without deleting its audit trail."""

        ...

    async def invalidate_schema(
        self,
        tenant_id: str,
        document_type: str,
        schema_version: str,
        reason: str,
    ) -> int:
        """Invalidate all memories in one obsolete Schema scope."""

        ...


class CorrectionMemoryRepository(Protocol):
    """Workflow-facing, bounded correction-memory use case."""

    async def retrieve(
        self,
        tenant_id: str,
        document: DocumentReference,
        output_schema: type[InvoiceT],
        current_invoice: object | None,
        limit: int,
    ) -> tuple[CorrectionEvent, ...]:
        """Return a small set of reviewed, valid, high-similarity examples."""

        ...

    async def save(
        self,
        tenant_id: str,
        identity: WorkflowIdentity,
        document: DocumentReference,
        events: tuple[CorrectionEvent, ...],
    ) -> None:
        """Persist raw events, then idempotently update derived vector memory."""

        ...
