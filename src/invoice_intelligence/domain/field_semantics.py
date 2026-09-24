"""Framework-independent field semantic catalog and binding contracts."""

import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from invoice_intelligence.domain.examples import (
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    RetrievalRecallSource,
    RetrievalScore,
    SparseVector,
)


class FieldAliasStatus(StrEnum):
    """Approval lifecycle for tenant-authored aliases."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    INVALIDATED = "invalidated"


class FieldAliasCandidateScope(StrEnum):
    """Whether an alias candidate is tenant-owned or globally governed."""

    TENANT = "tenant"
    GLOBAL = "global"


class FieldAliasCandidateDecisionAuthority(StrEnum):
    """Human authorities allowed to decide alias candidates."""

    TENANT_GOVERNOR = "tenant_governor"
    GLOBAL_GOVERNOR = "global_governor"


class FieldContextRelation(StrEnum):
    """Deterministic spatial/text relation used to disambiguate a label."""

    SAME_LINE = "same_line"
    PRECEDING = "preceding"
    FOLLOWING = "following"
    SAME_BLOCK = "same_block"
    DOCUMENT_SECTION = "document_section"


class FieldBindingDecisionAuthority(StrEnum):
    """Authorities allowed to finalize a field binding; models are advisory only."""

    DETERMINISTIC_POLICY = "deterministic_policy"
    HUMAN_REVIEWER = "human_reviewer"


class FieldBindingStatus(StrEnum):
    """Deterministic outcome without authority to change the observed invoice value."""

    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class FieldSemanticCatalogVersion:
    """Opaque, rollback-capable version of a composed field semantic catalog."""

    value: str

    def __post_init__(self) -> None:
        _require_normalized("catalog version", self.value)


@dataclass(frozen=True, slots=True)
class FieldContextAnchor:
    """Approved context required to distinguish an otherwise ambiguous label."""

    anchor_id: str
    text: str
    normalized_text: str
    relation: FieldContextRelation
    max_distance: int | None = None
    is_negative: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("anchor_id", self.anchor_id),
            ("anchor text", self.text),
            ("normalized anchor text", self.normalized_text),
        ):
            _require_normalized(name, value)
        if self.max_distance is not None and self.max_distance <= 0:
            raise ValueError("Context anchor max_distance must be greater than zero")


@dataclass(frozen=True, slots=True)
class FieldContextObservation:
    """Current-page location of nearby text used only for deterministic binding."""

    text: str
    normalized_text: str
    relation: FieldContextRelation
    distance: int | None = None

    def __post_init__(self) -> None:
        _require_normalized("context observation text", self.text)
        _require_normalized("normalized context observation", self.normalized_text)
        if self.distance is not None and self.distance < 0:
            raise ValueError("Context observation distance must not be negative")


@dataclass(frozen=True, slots=True)
class FieldAlias:
    """Versioned tenant alias whose review status is independent from Schema fields."""

    alias_id: str
    tenant_id: str
    schema_version: str
    document_type: str
    canonical_field_path: str
    alias_text: str
    normalized_alias: str
    is_negative: bool
    context_anchors: tuple[FieldContextAnchor, ...]
    catalog_version: FieldSemanticCatalogVersion
    status: FieldAliasStatus
    submitted_by: str
    submitted_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_reason: str | None
    is_valid: bool = True
    source_run_id: str | None = None
    source_document_id: str | None = None
    source_evidence_id: str | None = None
    source_binding_decision_id: str | None = None
    submission_reason: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("alias_id", self.alias_id),
            ("tenant_id", self.tenant_id),
            ("schema_version", self.schema_version),
            ("document_type", self.document_type),
            ("canonical_field_path", self.canonical_field_path),
            ("alias_text", self.alias_text),
            ("normalized_alias", self.normalized_alias),
            ("submitted_by", self.submitted_by),
        ):
            _require_normalized(name, value)
        _require_aware("submitted_at", self.submitted_at)
        anchor_ids = tuple(item.anchor_id for item in self.context_anchors)
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("Field alias context anchor IDs must be unique")

        reviewed = self.status is not FieldAliasStatus.PENDING
        review_values = (self.reviewed_by, self.reviewed_at, self.review_reason)
        if reviewed and any(value is None for value in review_values):
            raise ValueError("Reviewed field aliases require reviewer, time, and reason")
        if not reviewed and any(value is not None for value in review_values):
            raise ValueError("Pending field aliases cannot contain review metadata")
        if self.reviewed_by is not None:
            _require_normalized("reviewed_by", self.reviewed_by)
        if self.reviewed_at is not None:
            _require_aware("reviewed_at", self.reviewed_at)
            if self.reviewed_at < self.submitted_at:
                raise ValueError("Alias reviewed_at cannot precede submitted_at")
        if self.review_reason is not None:
            _require_normalized("review_reason", self.review_reason)
        if self.status is FieldAliasStatus.INVALIDATED and self.is_valid:
            raise ValueError("Invalidated aliases cannot remain valid")
        if not self.is_valid and self.status is not FieldAliasStatus.INVALIDATED:
            raise ValueError("Only invalidated aliases may set is_valid=false")
        source_values = (
            self.source_run_id,
            self.source_document_id,
            self.source_evidence_id,
            self.source_binding_decision_id,
            self.submission_reason,
        )
        if any(value is not None for value in source_values):
            if any(value is None for value in source_values):
                raise ValueError("Reviewed binding aliases require complete source metadata")
            for source_name, source_value in (
                ("source_run_id", self.source_run_id),
                ("source_document_id", self.source_document_id),
                ("source_evidence_id", self.source_evidence_id),
                ("source_binding_decision_id", self.source_binding_decision_id),
                ("submission_reason", self.submission_reason),
            ):
                _require_normalized(source_name, source_value or "")

    @property
    def eligible_for_binding(self) -> bool:
        return self.status is FieldAliasStatus.APPROVED and self.is_valid


@dataclass(frozen=True, slots=True)
class FieldAliasCandidateSupport:
    """One idempotent human-mapping source for a tenant alias candidate."""

    support_id: str
    candidate_id: str
    tenant_id: str
    document_id: str
    run_id: str
    evidence_id: str
    binding_decision_id: str
    reviewer_id: str
    source_catalog_version: FieldSemanticCatalogVersion
    reason: str
    template_fingerprint: str | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("support_id", self.support_id),
            ("candidate_id", self.candidate_id),
            ("tenant_id", self.tenant_id),
            ("document_id", self.document_id),
            ("run_id", self.run_id),
            ("evidence_id", self.evidence_id),
            ("binding_decision_id", self.binding_decision_id),
            ("reviewer_id", self.reviewer_id),
            ("support reason", self.reason),
        ):
            _require_normalized(name, value)
        if self.template_fingerprint is not None:
            _require_normalized("template_fingerprint", self.template_fingerprint)
        _require_aware("support occurred_at", self.occurred_at)


@dataclass(frozen=True, slots=True)
class FieldAliasSupportSummary:
    """Windowed support diversity; counts are facts, not approval authority."""

    candidate_id: str
    window_started_at: datetime
    window_ended_at: datetime
    source_count: int
    distinct_documents: int
    distinct_templates: int
    distinct_reviewers: int

    def __post_init__(self) -> None:
        _require_normalized("candidate_id", self.candidate_id)
        _require_aware("support window_started_at", self.window_started_at)
        _require_aware("support window_ended_at", self.window_ended_at)
        if self.window_ended_at < self.window_started_at:
            raise ValueError("Alias support window end cannot precede its start")
        counts = (
            self.source_count,
            self.distinct_documents,
            self.distinct_templates,
            self.distinct_reviewers,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Alias support counts must not be negative")
        if any(value > self.source_count for value in counts[1:]):
            raise ValueError("Alias support diversity cannot exceed source_count")


@dataclass(frozen=True, slots=True)
class GlobalFieldAliasSupportSnapshot:
    """Irreversibly tenant-hashed cross-tenant aggregate for global governance."""

    candidate_id: str
    salt_version: str
    tenant_fingerprints: tuple[str, ...]
    source_count: int
    distinct_documents: int
    distinct_templates: int
    distinct_reviewers: int
    created_at: datetime

    def __post_init__(self) -> None:
        _require_normalized("candidate_id", self.candidate_id)
        _require_normalized("global alias salt_version", self.salt_version)
        _require_unique_text("global alias tenant fingerprint", self.tenant_fingerprints)
        if len(self.tenant_fingerprints) < 2:
            raise ValueError("Global alias support requires multiple tenant fingerprints")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.tenant_fingerprints
        ):
            raise ValueError("Global alias tenant fingerprints must be SHA-256 hex digests")
        counts = (
            self.source_count,
            self.distinct_documents,
            self.distinct_templates,
            self.distinct_reviewers,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Global alias support counts must not be negative")
        if any(value > self.source_count for value in counts[1:]):
            raise ValueError("Global alias support diversity cannot exceed source_count")
        _require_aware("global alias support created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class FieldAliasCandidate:
    """Governed alias proposal; only promotion creates a catalog alias."""

    candidate_id: str
    scope: FieldAliasCandidateScope
    tenant_id: str | None
    schema_version: str
    document_type: str
    canonical_field_path: str
    alias_text: str
    normalized_alias: str
    policy_version: str
    context_anchors: tuple[FieldContextAnchor, ...]
    status: FieldAliasStatus
    canonical_collision: bool
    support_window_days: int
    revision: int
    submitted_at: datetime
    updated_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = None
    promoted_catalog_version: FieldSemanticCatalogVersion | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id),
            ("schema_version", self.schema_version),
            ("document_type", self.document_type),
            ("canonical_field_path", self.canonical_field_path),
            ("alias_text", self.alias_text),
            ("normalized_alias", self.normalized_alias),
            ("policy_version", self.policy_version),
        ):
            _require_normalized(name, value)
        if self.scope is FieldAliasCandidateScope.TENANT:
            _require_normalized("tenant candidate tenant_id", self.tenant_id or "")
        elif self.tenant_id is not None:
            raise ValueError("Global alias candidates cannot persist a raw tenant_id")
        if self.support_window_days <= 0:
            raise ValueError("Alias candidate support_window_days must be positive")
        if self.revision <= 0:
            raise ValueError("Alias candidate revision must be positive")
        _require_aware("candidate submitted_at", self.submitted_at)
        _require_aware("candidate updated_at", self.updated_at)
        if self.updated_at < self.submitted_at:
            raise ValueError("Alias candidate updated_at cannot precede submitted_at")
        anchor_ids = tuple(item.anchor_id for item in self.context_anchors)
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("Alias candidate context anchor IDs must be unique")
        reviewed = self.status is not FieldAliasStatus.PENDING
        review_metadata = (self.reviewed_by, self.reviewed_at, self.review_reason)
        if reviewed and any(item is None for item in review_metadata):
            raise ValueError("Reviewed alias candidates require complete review metadata")
        if not reviewed and any(item is not None for item in review_metadata):
            raise ValueError("Pending alias candidates cannot contain review metadata")
        if self.reviewed_at is not None:
            _require_aware("candidate reviewed_at", self.reviewed_at)
            if self.reviewed_at < self.submitted_at:
                raise ValueError("Candidate reviewed_at cannot precede submitted_at")
        if self.reviewed_by is not None:
            _require_normalized("candidate reviewed_by", self.reviewed_by)
        if self.review_reason is not None:
            _require_normalized("candidate review_reason", self.review_reason)
        if (
            self.status is FieldAliasStatus.APPROVED
            and self.scope is FieldAliasCandidateScope.TENANT
            and self.promoted_catalog_version is None
        ):
            raise ValueError("Approved tenant candidates require a promoted Catalog version")
        if (
            self.status is FieldAliasStatus.SUSPENDED
            and self.scope is FieldAliasCandidateScope.TENANT
            and self.promoted_catalog_version is None
        ):
            raise ValueError("Suspended tenant candidates require a promoted Catalog version")
        if (
            self.scope is FieldAliasCandidateScope.GLOBAL
            and self.promoted_catalog_version is not None
        ):
            raise ValueError("Global candidates cannot directly promote a tenant Catalog")
        if (
            self.status in {FieldAliasStatus.PENDING, FieldAliasStatus.REJECTED}
            and self.promoted_catalog_version is not None
        ):
            raise ValueError("Unpromoted alias candidate status cannot reference a Catalog")


@dataclass(frozen=True, slots=True)
class FieldAliasCandidateDecision:
    """Attributable candidate transition; models are not an authority."""

    decision_id: str
    candidate_id: str
    scope: FieldAliasCandidateScope
    previous_status: FieldAliasStatus
    status: FieldAliasStatus
    authority: FieldAliasCandidateDecisionAuthority
    reviewer_id: str
    reason: str
    idempotency_key_hash: str
    previous_revision: int
    revision: int
    policy_version: str
    decided_at: datetime
    promoted_catalog_version: FieldSemanticCatalogVersion | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("candidate_id", self.candidate_id),
            ("reviewer_id", self.reviewer_id),
            ("reason", self.reason),
            ("idempotency_key_hash", self.idempotency_key_hash),
            ("policy_version", self.policy_version),
        ):
            _require_normalized(name, value)
        if self.previous_revision <= 0 or self.revision <= 0:
            raise ValueError("Alias candidate decision revisions must be positive")
        if self.revision < self.previous_revision:
            raise ValueError("Alias candidate decision revision cannot move backwards")
        if self.previous_status is self.status or self.status is FieldAliasStatus.PENDING:
            raise ValueError("Alias candidate decisions require a state transition")
        if (
            self.scope is FieldAliasCandidateScope.TENANT
            and self.authority is not FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR
        ):
            raise ValueError("Tenant alias candidates require tenant governor authority")
        if (
            self.scope is FieldAliasCandidateScope.GLOBAL
            and self.authority is not FieldAliasCandidateDecisionAuthority.GLOBAL_GOVERNOR
        ):
            raise ValueError("Global alias candidates require global governor authority")
        if (
            self.status is FieldAliasStatus.APPROVED
            and self.scope is FieldAliasCandidateScope.TENANT
            and self.promoted_catalog_version is None
        ):
            raise ValueError("Approved tenant candidates require a Catalog version")
        if (
            self.scope is FieldAliasCandidateScope.GLOBAL
            and self.promoted_catalog_version is not None
        ):
            raise ValueError("Global candidates cannot directly promote a tenant Catalog")
        if (
            self.status is not FieldAliasStatus.APPROVED
            and self.promoted_catalog_version is not None
        ):
            raise ValueError("Only approval decisions may introduce a Catalog version")
        _require_aware("candidate decision decided_at", self.decided_at)


@dataclass(frozen=True, slots=True)
class FieldAliasPromotion:
    """Approved candidate and its inactive, projection-ready Catalog version."""

    candidate: FieldAliasCandidate
    catalog_version: FieldSemanticCatalogVersion
    support: FieldAliasSupportSummary

    def __post_init__(self) -> None:
        if self.candidate.status is not FieldAliasStatus.APPROVED:
            raise ValueError("Field alias promotion requires an approved candidate")
        if self.candidate.scope is not FieldAliasCandidateScope.TENANT:
            raise ValueError("Only tenant alias candidates may promote a Catalog version")
        if self.candidate.promoted_catalog_version != self.catalog_version:
            raise ValueError("Field alias promotion Catalog version is inconsistent")


@dataclass(frozen=True, slots=True)
class FieldSemanticDefinition:
    """Composed technical metadata for one immutable invoice Entity field."""

    schema_version: str
    document_type: str
    canonical_field_path: str
    display_name: str
    description: str
    value_type: str
    aliases: tuple[FieldAlias, ...]
    negative_aliases: tuple[FieldAlias, ...]
    context_anchors: tuple[FieldContextAnchor, ...]
    catalog_version: FieldSemanticCatalogVersion
    tenant_scope: str | None
    is_valid: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("schema_version", self.schema_version),
            ("document_type", self.document_type),
            ("canonical_field_path", self.canonical_field_path),
            ("display_name", self.display_name),
            ("description", self.description),
            ("value_type", self.value_type),
        ):
            _require_normalized(name, value)
        if self.tenant_scope is not None:
            _require_normalized("tenant_scope", self.tenant_scope)
        aliases = self.aliases + self.negative_aliases
        if any(not item.eligible_for_binding for item in aliases):
            raise ValueError("Semantic definitions may contain only approved, valid aliases")
        if any(item.is_negative for item in self.aliases):
            raise ValueError("Positive alias region contains a negative alias")
        if any(not item.is_negative for item in self.negative_aliases):
            raise ValueError("Negative alias region contains a positive alias")
        if any(
            item.schema_version != self.schema_version
            or item.document_type != self.document_type
            or item.canonical_field_path != self.canonical_field_path
            or item.catalog_version != self.catalog_version
            or item.tenant_id != self.tenant_scope
            for item in aliases
        ):
            raise ValueError("Field aliases must match their semantic definition scope")
        anchor_ids = tuple(item.anchor_id for item in self.context_anchors)
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("Semantic definition context anchor IDs must be unique")


@dataclass(frozen=True, slots=True)
class FieldSemanticPromptDefinition:
    """Bounded approved catalog metadata supplied to a Vision Provider."""

    document_type: str
    canonical_field_path: str
    display_name: str
    description: str
    value_type: str
    approved_aliases: tuple[str, ...]
    negative_aliases: tuple[str, ...]
    context_anchors: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("document_type", self.document_type),
            ("canonical_field_path", self.canonical_field_path),
            ("display_name", self.display_name),
            ("description", self.description),
            ("value_type", self.value_type),
        ):
            _require_normalized(name, value)
        for name, values in (
            ("approved_alias", self.approved_aliases),
            ("negative_alias", self.negative_aliases),
            ("context_anchor", self.context_anchors),
        ):
            _require_unique_text(name, values)
        if set(self.approved_aliases).intersection(self.negative_aliases):
            raise ValueError("Prompt aliases cannot be both positive and negative")


@dataclass(frozen=True, slots=True)
class FieldSemanticPromptCatalog:
    """Versioned prompt projection without tenant, reviewer, or approval metadata."""

    schema_version: str
    catalog_version: FieldSemanticCatalogVersion
    definitions: tuple[FieldSemanticPromptDefinition, ...]

    def __post_init__(self) -> None:
        _require_normalized("schema_version", self.schema_version)
        if not self.definitions:
            raise ValueError("Field semantic prompt catalog must contain definitions")
        scopes = tuple(
            (item.document_type, item.canonical_field_path) for item in self.definitions
        )
        if len(scopes) != len(set(scopes)):
            raise ValueError("Field semantic prompt definitions must have unique scopes")


@dataclass(frozen=True, slots=True)
class FieldSemanticIndexScope:
    """Mandatory pre-vector-search filters for one composed catalog view."""

    tenant_id: str
    document_type: str
    schema_version: str
    catalog_version: FieldSemanticCatalogVersion
    is_valid: Literal[True] = True

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("document_type", self.document_type),
            ("schema_version", self.schema_version),
        ):
            _require_normalized(name, value)
        if self.is_valid is not True:
            raise ValueError("Field semantic search scope must require valid definitions")


@dataclass(frozen=True, slots=True)
class FieldSemanticIndexDocument:
    """Approved, derived field metadata allowed in the dedicated Milvus index."""

    semantic_id: str
    scope: FieldSemanticIndexScope
    canonical_field_path: str
    display_name: str
    description: str
    value_type: str
    approved_aliases: tuple[str, ...]
    negative_aliases: tuple[str, ...]
    context_anchors: tuple[str, ...]
    dense_index_text: str
    sparse_index_text: str
    index_version: IndexVersion
    source_fingerprint: str

    def __post_init__(self) -> None:
        for name, value in (
            ("semantic_id", self.semantic_id),
            ("canonical_field_path", self.canonical_field_path),
            ("display_name", self.display_name),
            ("description", self.description),
            ("value_type", self.value_type),
            ("dense_index_text", self.dense_index_text),
            ("sparse_index_text", self.sparse_index_text),
            ("source_fingerprint", self.source_fingerprint),
        ):
            _require_normalized(name, value)
        for name, values in (
            ("approved alias", self.approved_aliases),
            ("negative alias", self.negative_aliases),
            ("context anchor", self.context_anchors),
        ):
            _require_unique_text(name, values)
        if set(self.approved_aliases).intersection(self.negative_aliases):
            raise ValueError("Positive and negative field aliases must remain separated")
        for text in (self.dense_index_text, self.sparse_index_text):
            lowered = text.casefold()
            if lowered.startswith("data:") or "base64," in lowered:
                raise ValueError("Field semantic index text cannot contain inline images")


@dataclass(frozen=True, slots=True)
class FieldSemanticVectorProjection:
    """Dense/sparse payload derived from one approved semantic index document."""

    document: FieldSemanticIndexDocument
    dense_embedding: tuple[float, ...]
    sparse_embedding: SparseVector | None
    projection_checksum: str

    def __post_init__(self) -> None:
        _require_normalized("projection_checksum", self.projection_checksum)
        if not self.dense_embedding:
            raise ValueError("Field semantic dense embedding must not be empty")
        if any(not math.isfinite(value) for value in self.dense_embedding):
            raise ValueError("Field semantic dense embedding must contain finite values")
        if self.sparse_embedding is not None:
            if not self.sparse_embedding:
                raise ValueError("Field semantic sparse embedding must not be empty")
            previous_index = -1
            for token_index, weight in self.sparse_embedding:
                if token_index < 0 or token_index <= previous_index:
                    raise ValueError("Sparse token indexes must be non-negative and ordered")
                if not math.isfinite(weight) or weight < 0.0:
                    raise ValueError(
                        "Sparse embedding weights must be finite and non-negative"
                    )
                previous_index = token_index


@dataclass(frozen=True, slots=True)
class RetrievedFieldSemantic:
    """Non-authoritative field binding prior returned by hybrid retrieval."""

    document: FieldSemanticIndexDocument
    score: RetrievalScore
    rank: int
    recall_sources: tuple[RetrievalRecallSource, ...]
    is_historical_prior: Literal[True] = field(default=True, init=False)
    may_override_current_evidence: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.rank <= 0:
            raise ValueError("Retrieved field semantic rank must be greater than zero")
        if self.document.scope.is_valid is not True:
            raise ValueError("Retrieved field semantics must remain valid")
        if not self.recall_sources:
            raise ValueError("Retrieved field semantics require a recall source")
        if len(self.recall_sources) != len(set(self.recall_sources)):
            raise ValueError("Field semantic recall sources must be unique")


@dataclass(frozen=True, slots=True)
class FieldSemanticProjectionTask:
    """PostgreSQL-owned, retryable identity for one derived index document."""

    projection_id: str
    semantic_id: str
    tenant_id: str
    document_type: str
    canonical_field_path: str
    schema_version: str
    catalog_version: FieldSemanticCatalogVersion
    index_version: IndexVersion
    display_name: str
    description: str
    value_type: str
    approved_aliases: tuple[str, ...]
    negative_aliases: tuple[str, ...]
    context_anchors: tuple[str, ...]
    source_fingerprint: str
    status: IndexProjectionStatus
    attempt_count: int

    def __post_init__(self) -> None:
        for name, value in (
            ("projection_id", self.projection_id),
            ("semantic_id", self.semantic_id),
            ("tenant_id", self.tenant_id),
            ("document_type", self.document_type),
            ("canonical_field_path", self.canonical_field_path),
            ("schema_version", self.schema_version),
            ("display_name", self.display_name),
            ("description", self.description),
            ("value_type", self.value_type),
            ("source_fingerprint", self.source_fingerprint),
        ):
            _require_normalized(name, value)
        for name, values in (
            ("approved alias", self.approved_aliases),
            ("negative alias", self.negative_aliases),
            ("context anchor", self.context_anchors),
        ):
            _require_unique_text(name, values)
        if set(self.approved_aliases).intersection(self.negative_aliases):
            raise ValueError("Projection source aliases cannot be both positive and negative")
        if self.attempt_count < 0:
            raise ValueError("Projection attempt_count must not be negative")


@dataclass(frozen=True, slots=True)
class FieldSemanticIndexVersionRecord:
    """Rollback-capable PostgreSQL governance state for a catalog index build."""

    tenant_id: str
    index_version: IndexVersion
    schema_version: str
    catalog_version: FieldSemanticCatalogVersion
    dense_model_version: ModelVersion
    sparse_model_version: ModelVersion | None
    is_active: bool
    is_valid: bool
    pending_count: int
    processing_count: int
    indexed_count: int
    failed_count: int
    invalidated_count: int
    created_at: datetime
    activated_at: datetime | None = None
    retired_at: datetime | None = None
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = None

    def __post_init__(self) -> None:
        _require_normalized("tenant_id", self.tenant_id)
        _require_normalized("schema_version", self.schema_version)
        for count_name, count_value in (
            ("pending_count", self.pending_count),
            ("processing_count", self.processing_count),
            ("indexed_count", self.indexed_count),
            ("failed_count", self.failed_count),
            ("invalidated_count", self.invalidated_count),
        ):
            if count_value < 0:
                raise ValueError(f"{count_name} must not be negative")
        _require_aware("created_at", self.created_at)
        for timestamp_name, timestamp_value in (
            ("activated_at", self.activated_at),
            ("retired_at", self.retired_at),
            ("invalidated_at", self.invalidated_at),
        ):
            if timestamp_value is not None:
                _require_aware(timestamp_name, timestamp_value)
        if self.is_active and not self.is_valid:
            raise ValueError("An invalid field semantic index cannot be active")
        if self.invalidated_reason is not None:
            _require_normalized("invalidated_reason", self.invalidated_reason)
        if self.is_valid and (
            self.invalidated_at is not None or self.invalidated_reason is not None
        ):
            raise ValueError("Valid index versions cannot carry invalidation metadata")
        if not self.is_valid and (
            self.invalidated_at is None or self.invalidated_reason is None
        ):
            raise ValueError("Invalid index versions require invalidation metadata")

    @property
    def ready_for_activation(self) -> bool:
        return (
            self.is_valid
            and self.pending_count == 0
            and self.processing_count == 0
            and self.failed_count == 0
            and self.indexed_count > 0
        )


@dataclass(frozen=True, slots=True)
class FieldBindingDecisionSummary:
    """Checkpoint-safe binding outcome without complete retrieval results."""

    decision_id: str
    evidence_id: str
    document_type: str
    schema_version: str
    status: FieldBindingStatus
    selected_canonical_field_path: str | None
    reason_codes: tuple[str, ...]
    catalog_version: FieldSemanticCatalogVersion
    index_version: IndexVersion | None
    policy_version: str
    authority: FieldBindingDecisionAuthority
    top1_score: float | None
    top2_score: float | None
    score_margin: float | None
    requires_review: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("evidence_id", self.evidence_id),
            ("document_type", self.document_type),
            ("schema_version", self.schema_version),
            ("policy_version", self.policy_version),
        ):
            _require_normalized(name, value)
        if self.selected_canonical_field_path is not None:
            _require_normalized(
                "selected_canonical_field_path",
                self.selected_canonical_field_path,
            )
        _require_unique_text("binding reason code", self.reason_codes)
        if not self.reason_codes:
            raise ValueError("Binding decision summaries require a reason code")
        for name, score in (
            ("top1_score", self.top1_score),
            ("top2_score", self.top2_score),
            ("score_margin", self.score_margin),
        ):
            if score is not None:
                _require_score(name, score)
        if self.status is FieldBindingStatus.ACCEPTED:
            if self.requires_review or self.selected_canonical_field_path is None:
                raise ValueError("Accepted binding summaries require one selected field")
        elif self.selected_canonical_field_path is not None or not self.requires_review:
            raise ValueError("Non-accepted binding summaries must remain unselected")


@dataclass(frozen=True, slots=True)
class FieldBindingEvidence:
    """Current-document label evidence; image bytes and Base64 are deliberately absent."""

    evidence_id: str
    document_id: str
    page_number: int
    image_reference: str
    observed_label: str
    normalized_label: str
    nearby_text: tuple[str, ...]
    observed_value_type: str | None = None
    bounding_box: tuple[int, int, int, int] | None = None
    context_observations: tuple[FieldContextObservation, ...] = ()
    candidate_field_paths: tuple[str, ...] = ()
    binding_decision: FieldBindingDecisionSummary | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("document_id", self.document_id),
            ("image_reference", self.image_reference),
            ("observed_label", self.observed_label),
            ("normalized_label", self.normalized_label),
        ):
            _require_normalized(name, value)
        if self.page_number <= 0:
            raise ValueError("Field binding page_number must be greater than zero")
        lowered_reference = self.image_reference.casefold()
        if lowered_reference.startswith("data:") or "base64," in lowered_reference:
            raise ValueError("Field binding evidence must use an image reference, not Base64")
        _require_unique_text("nearby text", self.nearby_text)
        if self.observed_value_type is not None:
            _require_normalized("observed_value_type", self.observed_value_type)
        if self.bounding_box is not None:
            left, top, right, bottom = self.bounding_box
            if min(self.bounding_box) < 0 or right <= left or bottom <= top:
                raise ValueError("Field binding bounding_box must have positive area")
        observations = tuple(
            (
                item.normalized_text,
                item.relation,
                item.distance,
            )
            for item in self.context_observations
        )
        if len(observations) != len(set(observations)):
            raise ValueError("Field binding context observations must be unique")
        _require_unique_text("candidate field path", self.candidate_field_paths)
        if self.binding_decision is not None:
            if self.binding_decision.evidence_id != self.evidence_id:
                raise ValueError("Binding decision summary must reference this evidence")
            selected = self.binding_decision.selected_canonical_field_path
            if selected is not None and selected not in self.candidate_field_paths:
                raise ValueError("Selected field path must be present in evidence candidates")


@dataclass(frozen=True, slots=True)
class FieldBindingReviewDecision:
    """Explicit human mapping decision, separate from invoice value correction."""

    evidence_id: str
    selected_canonical_field_path: str
    reason: str

    def __post_init__(self) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("selected_canonical_field_path", self.selected_canonical_field_path),
            ("reason", self.reason),
        ):
            _require_normalized(name, value)


@dataclass(frozen=True, slots=True)
class FieldBindingCandidate:
    """One non-authoritative field-path candidate with auditable match components."""

    evidence_id: str
    schema_version: str
    document_type: str
    canonical_field_path: str
    catalog_version: FieldSemanticCatalogVersion
    matched_alias_ids: tuple[str, ...]
    matched_context_anchor_ids: tuple[str, ...]
    conflicting_field_paths: tuple[str, ...]
    alias_match_score: float
    context_match_score: float
    combined_match_score: float
    position_match_score: float = 0.0
    value_type_match_score: float = 0.0
    retrieval_rank_score: float = 0.0
    rerank_rank_score: float = 0.0
    retrieval_score: RetrievalScore | None = None
    rule_failures: tuple[str, ...] = ()
    is_historical_prior: Literal[True] = field(default=True, init=False)
    may_override_current_evidence: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("schema_version", self.schema_version),
            ("document_type", self.document_type),
            ("canonical_field_path", self.canonical_field_path),
        ):
            _require_normalized(name, value)
        for name, values in (
            ("matched alias ID", self.matched_alias_ids),
            ("matched context anchor ID", self.matched_context_anchor_ids),
            ("conflicting field path", self.conflicting_field_paths),
        ):
            _require_unique_text(name, values)
        if self.canonical_field_path in self.conflicting_field_paths:
            raise ValueError("A binding candidate cannot conflict with itself")
        for name, score in (
            ("alias_match_score", self.alias_match_score),
            ("context_match_score", self.context_match_score),
            ("position_match_score", self.position_match_score),
            ("value_type_match_score", self.value_type_match_score),
            ("retrieval_rank_score", self.retrieval_rank_score),
            ("rerank_rank_score", self.rerank_rank_score),
            ("combined_match_score", self.combined_match_score),
        ):
            _require_score(name, score)
        _require_unique_text("binding rule failure", self.rule_failures)

    @property
    def rules_passed(self) -> bool:
        return not self.rule_failures


@dataclass(frozen=True, slots=True)
class FieldBindingDecision:
    """Attributable binding decision that never changes current invoice evidence."""

    decision_id: str
    tenant_id: str
    evidence_id: str
    document_type: str
    schema_version: str
    status: FieldBindingStatus
    selected_canonical_field_path: str | None
    candidates: tuple[FieldBindingCandidate, ...]
    catalog_version: FieldSemanticCatalogVersion
    index_version: IndexVersion | None
    policy_version: str
    authority: FieldBindingDecisionAuthority
    decided_by: str
    reason_codes: tuple[str, ...]
    requires_review: bool
    top1_score: float | None
    top2_score: float | None
    score_margin: float | None
    decided_at: datetime
    may_override_current_evidence: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("tenant_id", self.tenant_id),
            ("evidence_id", self.evidence_id),
            ("document_type", self.document_type),
            ("schema_version", self.schema_version),
            ("policy_version", self.policy_version),
            ("decided_by", self.decided_by),
        ):
            _require_normalized(name, value)
        if self.selected_canonical_field_path is not None:
            _require_normalized(
                "selected_canonical_field_path",
                self.selected_canonical_field_path,
            )
        _require_unique_text("binding reason code", self.reason_codes)
        if not self.reason_codes:
            raise ValueError("Field binding decisions require at least one reason code")
        _require_aware("decided_at", self.decided_at)
        if any(
            item.evidence_id != self.evidence_id
            or item.schema_version != self.schema_version
            or item.document_type != self.document_type
            or item.catalog_version != self.catalog_version
            for item in self.candidates
        ):
            raise ValueError("Binding candidates must match the decision scope")
        candidate_paths = tuple(item.canonical_field_path for item in self.candidates)
        if len(candidate_paths) != len(set(candidate_paths)):
            raise ValueError("Binding decisions cannot contain duplicate field candidates")
        if (
            self.selected_canonical_field_path is not None
            and self.selected_canonical_field_path not in candidate_paths
        ):
            raise ValueError("Selected field path must be present in the candidates")
        scores = tuple(item.combined_match_score for item in self.candidates)
        if scores != tuple(sorted(scores, reverse=True)):
            raise ValueError("Binding candidates must be ordered by combined score")
        expected_top1 = scores[0] if scores else None
        expected_top2 = scores[1] if len(scores) > 1 else None
        expected_margin = (
            expected_top1 - expected_top2
            if expected_top1 is not None and expected_top2 is not None
            else expected_top1
        )
        if (
            self.top1_score != expected_top1
            or self.top2_score != expected_top2
            or self.score_margin != expected_margin
        ):
            raise ValueError("Binding decision Top-1/Top-2 scores are inconsistent")
        for name, score in (
            ("top1_score", self.top1_score),
            ("top2_score", self.top2_score),
            ("score_margin", self.score_margin),
        ):
            if score is not None:
                _require_score(name, score)
        has_conflict = any(item.conflicting_field_paths for item in self.candidates)
        if (
            self.authority is FieldBindingDecisionAuthority.DETERMINISTIC_POLICY
            and has_conflict
            and (not self.requires_review or self.selected_canonical_field_path is not None)
        ):
            raise ValueError("Deterministic policy cannot resolve conflicting field bindings")
        if self.status is FieldBindingStatus.ACCEPTED:
            if self.requires_review or self.selected_canonical_field_path is None:
                raise ValueError("Accepted bindings require one selected field without review")
            selected = next(
                item
                for item in self.candidates
                if item.canonical_field_path == self.selected_canonical_field_path
            )
            if not selected.rules_passed:
                raise ValueError("Accepted bindings must pass deterministic rules")
        elif self.selected_canonical_field_path is not None or not self.requires_review:
            raise ValueError("Non-accepted bindings must remain unselected for review")
        if self.status is FieldBindingStatus.REVIEW_REQUIRED and not self.candidates:
            raise ValueError("Review-required bindings need at least one candidate")

    def to_summary(self) -> FieldBindingDecisionSummary:
        """Discard complete retrieval candidates before checkpoint persistence."""

        return FieldBindingDecisionSummary(
            decision_id=self.decision_id,
            evidence_id=self.evidence_id,
            document_type=self.document_type,
            schema_version=self.schema_version,
            status=self.status,
            selected_canonical_field_path=self.selected_canonical_field_path,
            reason_codes=self.reason_codes,
            catalog_version=self.catalog_version,
            index_version=self.index_version,
            policy_version=self.policy_version,
            authority=self.authority,
            top1_score=self.top1_score,
            top2_score=self.top2_score,
            score_margin=self.score_margin,
            requires_review=self.requires_review,
        )


def normalize_field_label(value: str) -> str:
    """Normalize formatting without inventing semantic synonyms."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    punctuation_normalized = "".join(
        " "
        if character.isspace()
        or unicodedata.category(character).startswith(("P", "C"))
        else character
        for character in normalized
    )
    return " ".join(punctuation_normalized.split())


def canonical_field_path_template(field_path: str) -> str:
    """Replace runtime array indexes with the stable Schema template marker."""

    return ".".join(
        "*" if segment.isdecimal() else segment
        for segment in field_path.split(".")
    )


def _require_normalized(name: str, value: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty and normalized")


def _require_unique_text(name: str, values: tuple[str, ...]) -> None:
    if any(not value.strip() or value != value.strip() for value in values):
        raise ValueError(f"{name} values must be non-empty and normalized")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} values must be unique")


def _require_score(name: str, value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
