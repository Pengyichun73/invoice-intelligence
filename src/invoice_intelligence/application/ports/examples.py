"""Application boundaries for reviewed-example RAG and index projection."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from invoice_intelligence.application.ports.admission import MemorySupportDiversity
from invoice_intelligence.application.ports.memory import EmbeddingProvider
from invoice_intelligence.application.ports.projection_lease import ProjectionLease
from invoice_intelligence.domain.examples import (
    ExampleCandidate,
    ExampleIndexProjectionState,
    ExampleLabelType,
    ExampleScope,
    ExampleVectorProjection,
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievedExample,
    ReviewedExample,
    ReviewedExamplePromptContext,
    SparseVector,
)
from invoice_intelligence.domain.extraction import FieldEvidence
from invoice_intelligence.domain.governance import GovernanceAuditEvent, IndexGovernanceRecord

type SparseEmbedding = SparseVector


@dataclass(frozen=True, slots=True)
class TenantRedactionPolicy:
    """Tenant-owned policy used only for derived example projections."""

    strategy: Literal["none", "mask", "hash", "drop"]
    policy_version: str
    hash_salt: str | None = None

    def __post_init__(self) -> None:
        if self.strategy not in {"none", "mask", "hash", "drop"}:
            raise ValueError("Unsupported tenant redaction strategy")
        if not self.policy_version.strip():
            raise ValueError("Redaction policy version must not be empty")
        if self.strategy == "hash" and not self.hash_salt:
            raise ValueError("Tenant hash redaction requires an isolated salt")


class ReviewedExampleRepository(Protocol):
    """Persist review facts and return admission candidates from PostgreSQL.

    Methods retaining the legacy ``eligible`` name mean reviewed and valid only.
    Callers must intersect their output with ``MemoryAdmissionRepository`` approval
    before projection, long-term retrieval, evaluation or dataset export.
    """

    async def upsert(self, example: ReviewedExample) -> ReviewedExample:
        """Persist one source and merge its canonical semantic case idempotently."""

        ...

    async def get_by_source_event(
        self,
        tenant_id: str,
        source_event_id: str,
    ) -> ReviewedExample | None:
        """Resolve an existing case without crossing tenant boundaries."""

        ...

    async def get_many(
        self,
        scope: ExampleScope,
        example_ids: Sequence[str],
    ) -> tuple[ReviewedExample, ...]:
        """Hydrate only currently reviewed and valid cases in the exact scope."""

        ...

    async def get_eligible_by_ids(
        self,
        tenant_id: str,
        schema_version: str,
        example_ids: Sequence[str],
    ) -> tuple[ReviewedExample, ...]:
        """Hydrate reviewed, valid admission candidates across field paths."""

        ...

    async def list_eligible(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[ReviewedExample, ...]:
        """Page reviewed, valid admission candidates in stable identifier order."""

        ...

    async def list_for_governance(
        self,
        tenant_id: str,
        *,
        schema_version: str | None,
        field_path: str | None,
        label_type: ExampleLabelType | None,
        is_valid: bool | None,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[ReviewedExample, ...]:
        """Page tenant-owned cases, including disabled cases, for governance."""

        ...

    async def get_for_governance(
        self,
        tenant_id: str,
        example_id: str,
    ) -> ReviewedExample | None:
        """Read one tenant-owned case regardless of its validity state."""

        ...

    async def get_support_diversity(
        self,
        tenant_id: str,
        semantic_fingerprint: str,
    ) -> MemorySupportDiversity:
        """Count independent review sources for one tenant-scoped semantic case."""

        ...

    async def invalidate(
        self,
        tenant_id: str,
        example_id: str,
        reason: str,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> bool:
        """Invalidate a bad memory without deleting its audit history."""

        ...

    async def invalidate_schema(
        self,
        tenant_id: str,
        schema_version: str,
        reason: str,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> int:
        """Invalidate all cases and projections for one obsolete Schema version."""

        ...

    async def delete_tenant(self, tenant_id: str) -> int:
        """Delete all tenant-owned reviewed-example RAG facts and index state."""

        ...

    async def invalidate_expired(
        self,
        tenant_id: str,
        older_than: datetime,
        reason: str,
    ) -> tuple[str, ...]:
        """Invalidate expired cases and return IDs for derived-index cleanup."""

        ...

    async def purge_invalidated(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
    ) -> int:
        """Delete already invalidated cases after derived-index cleanup succeeds."""

        ...


class DenseEmbeddingProvider(EmbeddingProvider, Protocol):
    """Semantic name for the existing SDK-neutral dense embedding boundary."""


class SparseEmbeddingProvider(Protocol):
    """Create sparse vectors without exposing a remote-model SDK."""

    async def embed(self, texts: Sequence[str]) -> tuple[SparseEmbedding, ...]:
        """Return one sparse embedding per input in deterministic input order."""

        ...


class RerankingProvider(Protocol):
    """Rerank redacted candidates; returned scores are not probabilities."""

    async def rerank(
        self,
        redacted_query_text: str,
        candidates: Sequence[ExampleCandidate],
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        """Return ordered ``(example_id, relevance_score)`` pairs."""

        ...


@dataclass(frozen=True, slots=True)
class RewrittenQuery:
    """Versioned retrieval-only rewrite of an already redacted query."""

    redacted_source_query: str
    rewritten_query: str
    expansion_terms: tuple[str, ...]
    model_version: ModelVersion
    prompt_version: PromptVersion
    retrieval_only: Literal[True] = True
    may_override_current_evidence: Literal[False] = False

    def __post_init__(self) -> None:
        if not self.redacted_source_query.strip():
            raise ValueError("Redacted source query must not be empty")
        if not self.rewritten_query.strip():
            raise ValueError("Rewritten query must not be empty")
        if any(not term.strip() for term in self.expansion_terms):
            raise ValueError("Query expansion terms must not contain blanks")
        if len(self.expansion_terms) != len(set(self.expansion_terms)):
            raise ValueError("Query expansion terms must be unique")


class QueryRewriteProvider(Protocol):
    """Rewrite redacted retrieval text without selecting invoice values or routes."""

    async def rewrite(
        self,
        redacted_query_text: str,
        scope: ExampleScope,
    ) -> RewrittenQuery:
        """Return a versioned retrieval hint scoped to one reviewed-example field."""

        ...


@dataclass(frozen=True, slots=True)
class HybridSearchOptions:
    """Per-request Milvus filters and fusion controls selected by the use case."""

    label_types: tuple[ExampleLabelType, ...]
    limit: int
    fusion_strategy: Literal["weighted", "rrf"] | None = None
    dense_weight: float | None = None
    sparse_weight: float | None = None
    template_fingerprint: str | None = None
    not_before: datetime | None = None

    def __post_init__(self) -> None:
        if self.fusion_strategy not in {None, "weighted", "rrf"}:
            raise ValueError("Unsupported hybrid search fusion strategy")
        if not self.label_types:
            raise ValueError("Hybrid search requires at least one reviewed label")
        if len(self.label_types) != len(set(self.label_types)):
            raise ValueError("Hybrid search labels must be unique")
        if self.limit <= 0:
            raise ValueError("Hybrid search limit must be greater than zero")
        weights = (self.dense_weight, self.sparse_weight)
        if any(weight is not None and weight < 0 for weight in weights):
            raise ValueError("Hybrid search weights must not be negative")
        configured_weights = tuple(weight for weight in weights if weight is not None)
        if len(configured_weights) == 2 and sum(configured_weights) <= 0:
            raise ValueError("At least one hybrid search weight must be greater than zero")
        if self.template_fingerprint is not None and (
            not self.template_fingerprint.strip()
            or self.template_fingerprint != self.template_fingerprint.strip()
        ):
            raise ValueError("Template fingerprint must be non-empty and normalized")
        if self.not_before is not None and (
            self.not_before.tzinfo is None or self.not_before.utcoffset() is None
        ):
            raise ValueError("Hybrid search not_before must be timezone-aware")


class ExampleRedactor(Protocol):
    """Separate raw PostgreSQL cases from values projected to remote systems.

    Implementations resolve a tenant policy (``none``, ``mask``, ``hash`` or ``drop``)
    and must never mutate the canonical ``ReviewedExample``.
    """

    def redact_example(
        self,
        example: ReviewedExample,
        index_version: IndexVersion,
        redaction_policy_version: str,
    ) -> ExampleCandidate:
        """Create a sanitized, versioned index projection without mutating the fact."""

        ...

    def redact_query(
        self,
        scope: ExampleScope,
        field_evidence: FieldEvidence,
        vendor_fingerprint: str | None,
        template_fingerprint: str | None,
    ) -> str:
        """Build sanitized current-field evidence for remote retrieval providers."""

        ...


class ExampleIndexStore(Protocol):
    """Rebuildable approved-memory index; PostgreSQL remains the source of truth."""

    async def upsert(self, projections: Sequence[ExampleVectorProjection]) -> None:
        """Idempotently upsert approved, redacted vectors into one index version."""

        ...

    async def ensure_collection(self, tenant_id: str, index_version: IndexVersion) -> str:
        """Check the target tenant/version collection before explicit activation."""

        ...

    async def switch_alias(self, tenant_id: str, index_version: IndexVersion) -> None:
        """Switch only this tenant's derived-index alias."""

        ...

    async def hybrid_search(
        self,
        scope: ExampleScope,
        redacted_query_text: str,
        dense_embedding: tuple[float, ...],
        sparse_embedding: SparseEmbedding | None,
        index_version: IndexVersion,
        options: HybridSearchOptions,
    ) -> tuple[RetrievedExample, ...]:
        """Filter exact scope first, then perform dense+sparse fusion search."""

        ...

    async def delete(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
        index_version: IndexVersion,
    ) -> None:
        """Remove invalid projections; canonical PostgreSQL rows remain intact."""

        ...

    async def delete_tenant(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        """Delete all derived projections for a tenant and index version."""

        ...


class ReviewedExampleContextProvider(Protocol):
    """Build bounded, redacted Few-shot context from approved memories only."""

    async def retrieve_for_extraction(
        self,
        tenant_id: str,
        invoice: object | None,
        field_evidence: Sequence[FieldEvidence],
    ) -> ReviewedExamplePromptContext | None:
        """Return no context when retrieval is unavailable or finds no approved cases."""

        ...


class IndexProjectionRepository(ReviewedExampleRepository, Protocol):
    """Persist rebuild and activation state in PostgreSQL, not in Milvus."""

    async def register_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
        rerank_model_version: ModelVersion | None,
        prompt_version: PromptVersion,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> None:
        """Register a build; scheduling still requires explicit admission approval."""

        ...

    async def schedule(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
        index_version: IndexVersion,
    ) -> None:
        """Idempotently create pending projections without storing raw case values."""

        ...

    async def claim_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ProjectionLease[ReviewedExample], ...]:
        """Atomically mark and return a bounded projection batch as processing."""

        ...

    async def list_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
    ) -> tuple[ReviewedExample, ...]:
        """Read pending source cases without claiming a worker lease."""

        ...

    async def requeue_stale(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        """Return abandoned processing leases to pending after worker failure."""

        ...

    async def renew_lease(
        self, tenant_id: str, example_id: str, index_version: IndexVersion,
        worker_id: str, lease_token: str, lease_seconds: float,
    ) -> None:
        """Extend only the currently owned, unexpired processing lease."""

        ...

    async def mark_projected(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
        projection_checksum: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        """Idempotently record a successful derived-index projection."""

        ...

    async def mark_failed(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
        error_code: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        """Record a non-secret projection failure for retry and audit."""

        ...

    async def get_active_version(self, tenant_id: str) -> IndexVersion | None:
        """Read the tenant's currently active, rollback-capable index version."""

        ...

    async def list_index_versions(
        self,
        tenant_id: str,
        *,
        schema_version: str | None = None,
    ) -> tuple[IndexVersion, ...]:
        """List tenant-owned index versions for cleanup or governance."""

        ...

    async def list_example_projections(
        self,
        tenant_id: str,
        example_id: str,
        *,
        index_version: IndexVersion | None,
        status: IndexProjectionStatus | None,
        limit: int,
        after_projection_id: str | None = None,
    ) -> tuple[ExampleIndexProjectionState, ...]:
        """Page safe PostgreSQL projection states for one tenant-owned example."""

        ...

    async def get_index_governance(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> IndexGovernanceRecord | None:
        """Read immutable index bindings plus PostgreSQL projection counters."""

        ...

    async def activate_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        """Atomically make a fully built index version active for the tenant."""

        ...
