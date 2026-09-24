"""Application boundaries for versioned field semantic metadata."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypeAlias, TypeVar

from invoice_intelligence.application.ports.memory import EmbeddingProvider
from invoice_intelligence.application.ports.projection_lease import ProjectionLease
from invoice_intelligence.domain.examples import IndexVersion, ModelVersion, SparseVector
from invoice_intelligence.domain.field_semantics import (
    FieldAlias,
    FieldAliasCandidate,
    FieldAliasCandidateDecision,
    FieldAliasCandidateSupport,
    FieldAliasStatus,
    FieldAliasSupportSummary,
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    FieldSemanticIndexScope,
    FieldSemanticIndexVersionRecord,
    FieldSemanticProjectionTask,
    FieldSemanticVectorProjection,
    GlobalFieldAliasSupportSnapshot,
    RetrievedFieldSemantic,
)
from invoice_intelligence.domain.governance import GovernanceAuditEvent

SchemaT = TypeVar("SchemaT")
FieldSemanticSparseEmbedding: TypeAlias = SparseVector


class FieldSemanticSchemaReader(Protocol):
    """Read base names, descriptions, and types from the Entity Schema only."""

    def read_field_semantics(
        self,
        output_schema: type[SchemaT],
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> tuple[FieldSemanticDefinition, ...]:
        """Return immutable Schema-derived definitions without tenant aliases."""

        ...

class FieldAliasRepository(Protocol):
    """Persist tenant alias submissions and immutable approval decisions in PostgreSQL."""

    async def register_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        created_by: str,
        created_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        """Idempotently register a tenant-scoped draft catalog version."""

        ...


    async def activate_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        approved_by: str,
        reason: str,
        idempotency_key: str,
        approved_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        """Activate an explicitly approved version and supersede its previous version."""

        ...

    async def get_active_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> FieldSemanticCatalogVersion | None:
        """Return the active tenant catalog version without a cross-tenant fallback."""

        ...

    async def invalidate_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
        invalidated_at: datetime,
    ) -> bool:
        """Invalidate one PostgreSQL catalog version before derived-index cleanup."""

        ...

    async def is_catalog_version_valid(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> bool:
        """Return whether a persisted tenant Catalog may be composed or projected."""

        ...

    async def is_catalog_version_published(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> bool:
        """Return whether a valid Catalog has completed an explicit activation."""

        ...

    async def submit_alias(self, alias: FieldAlias) -> FieldAlias:
        """Persist one pending alias and its context anchors idempotently."""

        ...

    async def get_alias(self, tenant_id: str, alias_id: str) -> FieldAlias | None:
        """Read one tenant-owned alias with its current immutable decision."""

        ...

    async def decide_alias(
        self,
        tenant_id: str,
        alias_id: str,
        target_status: FieldAliasStatus,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        decided_at: datetime,
    ) -> FieldAlias:
        """Append an attributable alias decision using optimistic status transitions."""

        ...

    async def list_approved_aliases(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        *,
        document_type: str | None = None,
        canonical_field_paths: Sequence[str] = (),
    ) -> tuple[FieldAlias, ...]:
        """Return only approved, valid aliases in the exact tenant/version scope."""

        ...


class FieldAliasCandidateRepository(Protocol):
    """Persist candidate aggregates and support facts without indexing them."""

    async def save_pending(
        self,
        candidate: FieldAliasCandidate,
        support: FieldAliasCandidateSupport | None,
        global_support: GlobalFieldAliasSupportSnapshot | None = None,
    ) -> FieldAliasCandidate:
        """Create one pending candidate and append one idempotent source."""

        ...

    async def get(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> FieldAliasCandidate | None:
        """Read one tenant-owned candidate."""

        ...

    async def get_global(self, candidate_id: str) -> FieldAliasCandidate | None:
        """Read one global candidate without exposing contributing tenants."""

        ...

    async def list_by_ids_unscoped(
        self,
        candidate_ids: Sequence[str],
    ) -> tuple[FieldAliasCandidate, ...]:
        """Privileged source read used only by global-governance orchestration."""

        ...

    async def list_competing(
        self,
        tenant_id: str,
        schema_version: str,
        document_type: str,
        normalized_alias: str,
    ) -> tuple[FieldAliasCandidate, ...]:
        """Return same-label tenant candidates before conflict evaluation."""

        ...

    async def list_for_governance(
        self,
        tenant_id: str,
        statuses: Sequence[FieldAliasStatus],
        *,
        schema_version: str,
        document_type: str | None,
        canonical_field_path: str | None,
        limit: int,
        after_candidate_id: str | None = None,
    ) -> tuple[FieldAliasCandidate, ...]:
        """Page tenant alias candidates in a stable, tenant-scoped order."""

        ...

    async def list_source_reviewer_ids(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> tuple[str, ...]:
        """Return distinct source reviewers for configurable dual-control checks."""

        ...

    async def summarize_support(
        self,
        tenant_id: str,
        candidate_id: str,
        as_of: datetime,
    ) -> FieldAliasSupportSummary:
        """Compute distinct source counts inside the candidate's configured window."""

        ...

    async def get_global_support(
        self,
        candidate_id: str,
    ) -> GlobalFieldAliasSupportSnapshot | None:
        """Read only irreversible global support aggregates."""

        ...

    async def get_decision_by_idempotency_hash(
        self,
        tenant_id: str | None,
        candidate_id: str,
        idempotency_key_hash: str,
    ) -> FieldAliasCandidateDecision | None:
        """Read a prior decision before evaluating the caller's expected revision."""

        ...

    async def decide(
        self,
        decision: FieldAliasCandidateDecision,
        *,
        tenant_id: str | None,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> FieldAliasCandidate:
        """Apply one idempotent human transition with an atomic revision CAS."""

        ...

    async def delete_tenant(self, tenant_id: str) -> int:
        """Delete tenant candidate aggregates and sources for retention handling."""

        ...


class FieldSemanticIndexStore(Protocol):
    """Dedicated rebuildable index for approved field catalog metadata."""

    async def upsert(
        self,
        projections: Sequence[FieldSemanticVectorProjection],
    ) -> None:
        """Idempotently write derived catalog documents; PostgreSQL remains canonical."""

        ...

    async def hybrid_search(
        self,
        scope: FieldSemanticIndexScope,
        query_text: str,
        dense_embedding: tuple[float, ...],
        sparse_embedding: FieldSemanticSparseEmbedding | None,
        index_version: IndexVersion,
        options: "FieldSemanticSearchOptions",
    ) -> tuple[RetrievedFieldSemantic, ...]:
        """Apply exact tenant/catalog filters before dense and sparse ANN search."""

        ...

    async def delete(
        self,
        tenant_id: str,
        semantic_ids: Sequence[str],
        index_version: IndexVersion,
    ) -> None:
        """Delete explicit derived primary keys in one tenant/version scope."""

        ...

    async def delete_tenant(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        """Delete one tenant's derived documents without touching PostgreSQL facts."""

        ...

    async def ensure_collection(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> str:
        """Ensure the dedicated versioned collection exists and is loaded."""

        ...

    async def switch_alias(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        """Move the configured Milvus alias to a completed versioned collection."""

        ...


@dataclass(frozen=True, slots=True)
class FieldSemanticSearchOptions:
    """Per-request fusion policy; scores remain uncalibrated relevance values."""

    limit: int
    fusion_strategy: Literal["weighted", "rrf"] = "weighted"
    dense_weight: float = 0.7
    sparse_weight: float = 0.3

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("Field semantic search limit must be greater than zero")
        if self.fusion_strategy not in {"weighted", "rrf"}:
            raise ValueError("Unsupported field semantic fusion strategy")
        if self.dense_weight < 0 or self.sparse_weight < 0:
            raise ValueError("Field semantic fusion weights must not be negative")
        if self.dense_weight + self.sparse_weight <= 0:
            raise ValueError("At least one field semantic fusion weight is required")


@dataclass(frozen=True, slots=True)
class FieldSemanticRerankCandidate:
    """Approved catalog metadata safe for a remote field-semantic reranker."""

    candidate_id: str
    canonical_field_path: str
    redacted_content: str

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id),
            ("canonical_field_path", self.canonical_field_path),
            ("redacted_content", self.redacted_content),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        lowered = self.redacted_content.casefold()
        if "base64," in lowered or "data:image" in lowered:
            raise ValueError("Field semantic rerank content cannot contain inline images")


class FieldSemanticRerankingProvider(Protocol):
    """Rerank approved semantic metadata without selecting invoice fields or values."""

    async def rerank_field_semantics(
        self,
        redacted_query_text: str,
        candidates: Sequence[FieldSemanticRerankCandidate],
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        """Return ordered semantic IDs and uncalibrated relevance scores."""

        ...


class FieldSemanticDenseEmbeddingProvider(EmbeddingProvider, Protocol):
    """SDK-neutral dense embedding boundary for field semantic projection."""


class FieldSemanticSparseEmbeddingProvider(Protocol):
    """Optional sparse provider when Milvus BM25 Function is disabled."""

    async def embed(
        self,
        texts: Sequence[str],
    ) -> tuple[FieldSemanticSparseEmbedding, ...]:
        ...


class FieldSemanticProjectionRepository(Protocol):
    """PostgreSQL-owned source identity, queue, version, and rollback state."""

    async def register_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
    ) -> None:
        ...

    async def schedule(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        tasks: Sequence[FieldSemanticProjectionTask],
    ) -> None:
        ...

    async def claim_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ProjectionLease[FieldSemanticProjectionTask], ...]:
        ...

    async def requeue_stale(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        ...

    async def renew_lease(
        self, tenant_id: str, semantic_id: str, index_version: IndexVersion,
        worker_id: str, lease_token: str, lease_seconds: float,
    ) -> None:
        ...

    async def mark_projected(
        self,
        tenant_id: str,
        semantic_id: str,
        index_version: IndexVersion,
        projection_checksum: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        ...

    async def mark_failed(
        self,
        tenant_id: str,
        semantic_id: str,
        index_version: IndexVersion,
        error_code: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        ...

    async def get_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexVersionRecord | None:
        ...

    async def get_active_version(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> FieldSemanticIndexVersionRecord | None:
        ...

    async def list_versions(
        self,
        tenant_id: str,
        *,
        schema_version: str | None = None,
    ) -> tuple[IndexVersion, ...]:
        ...

    async def activate_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        ...

    async def reset_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> int:
        ...

    async def invalidate_catalog(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        ...

    async def invalidate_schema(
        self,
        tenant_id: str,
        schema_version: str,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        ...

    async def delete_tenant(self, tenant_id: str) -> tuple[IndexVersion, ...]:
        ...
