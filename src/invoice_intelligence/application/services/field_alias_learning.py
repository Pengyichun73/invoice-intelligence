"""Controlled tenant alias learning from explicit human field bindings."""

import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Callable, Generic, TypeVar

from invoice_intelligence.application.errors import (
    BadRequestError,
    ForbiddenError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from invoice_intelligence.application.ports.admission import MemoryConflictRepository
from invoice_intelligence.application.ports.field_semantics import (
    FieldAliasCandidateRepository,
    FieldAliasRepository,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.application.services.field_semantic_index_projection import (
    FieldSemanticIndexProjectionService,
)
from invoice_intelligence.domain.admission import (
    MemoryConflictRecord,
    MemoryConflictStatus,
)
from invoice_intelligence.domain.field_semantics import (
    FieldAlias,
    FieldAliasCandidate,
    FieldAliasCandidateDecision,
    FieldAliasCandidateDecisionAuthority,
    FieldAliasCandidateScope,
    FieldAliasCandidateSupport,
    FieldAliasPromotion,
    FieldAliasStatus,
    FieldAliasSupportSummary,
    FieldBindingEvidence,
    FieldContextAnchor,
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    GlobalFieldAliasSupportSnapshot,
    normalize_field_label,
)
from invoice_intelligence.domain.governance import (
    GovernanceAuditEvent,
    MemoryPermission,
    TrustedTenantContext,
)
from invoice_intelligence.domain.workflow import JsonValue, WorkflowIdentity

SchemaT = TypeVar("SchemaT")


@dataclass(frozen=True, slots=True)
class FieldAliasLearningPolicy:
    """Versioned deterministic support and authority gates."""

    version: str
    support_window_days: int
    min_distinct_documents: int
    min_distinct_templates: int
    min_distinct_reviewers: int
    global_min_distinct_tenants: int

    def __post_init__(self) -> None:
        if not self.version.strip() or self.version != self.version.strip():
            raise ValueError("Field alias learning policy version must be normalized")
        if self.support_window_days <= 0:
            raise ValueError("Field alias support window must be positive")
        if any(
            value < 0
            for value in (
                self.min_distinct_documents,
                self.min_distinct_templates,
                self.min_distinct_reviewers,
            )
        ):
            raise ValueError("Tenant field alias support thresholds cannot be negative")
        if self.global_min_distinct_tenants < 2:
            raise ValueError("Global aliases require at least two distinct tenants")


@dataclass(frozen=True, slots=True)
class FieldAliasLearningResult:
    """Pending candidate plus current support and conflict references."""

    candidate: FieldAliasCandidate
    support: FieldAliasCandidateSupport
    support_summary: FieldAliasSupportSummary
    conflict_ids: tuple[str, ...]


class FieldAliasLearningService(Generic[SchemaT]):
    """Aggregate reviewed mappings without allowing them to self-promote."""

    def __init__(
        self,
        *,
        catalog: FieldSemanticCatalog[SchemaT],
        alias_repository: FieldAliasRepository,
        candidate_repository: FieldAliasCandidateRepository,
        conflict_repository: MemoryConflictRepository,
        index_projection_service: FieldSemanticIndexProjectionService[SchemaT] | None,
        policy: FieldAliasLearningPolicy,
        schema_version: str,
        fingerprint_salt: str,
        template_fingerprint_salt: str,
        global_hash_salt: str | None,
        global_hash_salt_version: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name, value in (
            ("fingerprint_salt", fingerprint_salt),
            ("template_fingerprint_salt", template_fingerprint_salt),
            ("global_hash_salt_version", global_hash_salt_version),
            ("schema_version", schema_version),
        ):
            self._require_text(name, value)
        if global_hash_salt is not None:
            self._require_text("global_hash_salt", global_hash_salt)
        self._catalog = catalog
        self._aliases = alias_repository
        self._candidates = candidate_repository
        self._conflicts = conflict_repository
        self._index_projection = index_projection_service
        self._policy = policy
        self._schema_version = schema_version
        self._fingerprint_salt = fingerprint_salt
        self._template_fingerprint_salt = template_fingerprint_salt
        self._global_hash_salt = global_hash_salt
        self._global_hash_salt_version = global_hash_salt_version
        self._clock = clock or (lambda: datetime.now(UTC))

    async def record_human_mapping(
        self,
        *,
        tenant_id: str,
        identity: WorkflowIdentity,
        reviewer_id: str,
        evidence: FieldBindingEvidence,
        binding_decision_id: str,
        document_type: str,
        canonical_field_path: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
        template_features: Mapping[str, JsonValue],
    ) -> FieldAliasLearningResult:
        """Append one source fact; the aggregate always remains pending here."""

        for name, value in (
            ("tenant_id", tenant_id),
            ("reviewer_id", reviewer_id),
            ("binding_decision_id", binding_decision_id),
            ("document_type", document_type),
            ("canonical_field_path", canonical_field_path),
            ("reason", reason),
        ):
            self._require_text(name, value)
        if evidence.document_id != identity.document_id:
            raise BadRequestError("Field binding evidence belongs to another document")
        normalized_alias = normalize_field_label(evidence.observed_label)
        if not normalized_alias or normalized_alias != evidence.normalized_label:
            raise BadRequestError("Field alias label is not canonically normalized")

        definitions = await self._catalog.list_definitions(
            tenant_id,
            document_type=document_type,
            catalog_version=catalog_version,
        )
        if not any(
            item.is_valid and item.canonical_field_path == canonical_field_path
            for item in definitions
        ):
            raise BadRequestError("Field alias target is absent from the current Schema")
        canonical_collision = any(
            normalized_alias in self._canonical_labels(item.canonical_field_path, item.display_name)
            for item in definitions
        )
        candidate_id = self._tenant_digest(
            tenant_id,
            "field-alias-candidate",
            {
                "policy_version": self._policy.version,
                "schema_version": definitions[0].schema_version,
                "document_type": document_type,
                "canonical_field_path": canonical_field_path,
                "normalized_alias": normalized_alias,
            },
        )
        anchors = self._candidate_anchors(candidate_id, evidence)
        now = self._now()
        candidate = FieldAliasCandidate(
            candidate_id=candidate_id,
            scope=FieldAliasCandidateScope.TENANT,
            tenant_id=tenant_id,
            schema_version=definitions[0].schema_version,
            document_type=document_type,
            canonical_field_path=canonical_field_path,
            alias_text=evidence.observed_label,
            normalized_alias=normalized_alias,
            policy_version=self._policy.version,
            context_anchors=anchors,
            status=FieldAliasStatus.PENDING,
            canonical_collision=canonical_collision,
            support_window_days=self._policy.support_window_days,
            revision=1,
            submitted_at=now,
            updated_at=now,
        )
        support_id = self._tenant_digest(
            tenant_id,
            "field-alias-support",
            {
                "run_id": identity.run_id,
                "document_id": identity.document_id,
                "evidence_id": evidence.evidence_id,
                "binding_decision_id": binding_decision_id,
            },
        )
        support = FieldAliasCandidateSupport(
            support_id=support_id,
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            document_id=identity.document_id,
            run_id=identity.run_id,
            evidence_id=evidence.evidence_id,
            binding_decision_id=binding_decision_id,
            reviewer_id=reviewer_id,
            source_catalog_version=catalog_version,
            reason=reason,
            template_fingerprint=self._template_fingerprint(
                tenant_id,
                template_features,
            ),
            occurred_at=now,
        )
        persisted = await self._candidates.save_pending(candidate, support)
        competing = await self._candidates.list_competing(
            tenant_id,
            persisted.schema_version,
            persisted.document_type,
            persisted.normalized_alias,
        )
        candidate_conflict = await self._record_competing_conflict(persisted, competing)
        catalog_conflict = await self._record_catalog_alias_conflict(
            persisted,
            definitions,
        )
        summary = await self._candidates.summarize_support(
            tenant_id,
            persisted.candidate_id,
            now,
        )
        return FieldAliasLearningResult(
            candidate=persisted,
            support=support,
            support_summary=summary,
            conflict_ids=tuple(
                dict.fromkeys(
                    conflict.conflict_id
                    for conflict in (candidate_conflict, catalog_conflict)
                    if conflict is not None
                )
            ),
        )

    async def approve_tenant_candidate(
        self,
        context: TrustedTenantContext,
        candidate_id: str,
        reason: str,
        idempotency_key: str,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> FieldAliasPromotion:
        """Promote one eligible tenant candidate into a new inactive Catalog version."""

        self._require_permission(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        candidate_id = self._normalized("candidate_id", candidate_id)
        reason = self._normalized("reason", reason)
        idempotency_key = self._normalized("idempotency_key", idempotency_key)
        self._require_expected_revision(expected_revision)
        replay = await self._replay_candidate_decision(
            tenant_id=context.tenant_id,
            candidate_id=candidate_id,
            status=FieldAliasStatus.APPROVED,
            authority=FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            audit_event=audit_event,
        )
        if replay is not None:
            if replay.promoted_catalog_version is None:
                raise ResourceConflictError("Approved alias replay has no Catalog version")
            support = await self._candidates.summarize_support(
                context.tenant_id,
                candidate_id,
                self._now(),
            )
            return FieldAliasPromotion(
                replay,
                replay.promoted_catalog_version,
                support,
            )
        candidate = await self._require_tenant_candidate(context.tenant_id, candidate_id)
        self._require_current_revision(candidate, expected_revision)
        now = self._now()
        support = await self._candidates.summarize_support(
            context.tenant_id,
            candidate_id,
            now,
        )
        if candidate.status not in {FieldAliasStatus.PENDING, FieldAliasStatus.SUSPENDED}:
            raise ResourceConflictError("Alias candidate is not eligible for approval")
        if candidate.schema_version != self._schema_version:
            raise ResourceConflictError(
                "Alias candidate belongs to a different Entity Schema version"
            )
        self._require_tenant_support(candidate, support)
        if candidate.canonical_collision:
            raise ResourceConflictError(
                "Alias collides with a canonical field label and cannot be promoted"
            )
        open_conflicts = await self._conflicts.list_open_for_alias_candidate(
            context.tenant_id,
            candidate_id,
        )
        if open_conflicts:
            raise ResourceConflictError(
                "Alias candidate has unresolved competing field mappings"
            )

        source_definitions = await self._catalog.list_definitions(context.tenant_id)
        if not source_definitions:
            raise ResourceConflictError("Current field semantic catalog is empty")
        source_versions = {item.catalog_version for item in source_definitions}
        if len(source_versions) != 1:
            raise ResourceConflictError("Current field semantic catalog has mixed versions")
        catalog_conflict = await self._record_catalog_alias_conflict(
            candidate,
            source_definitions,
        )
        if catalog_conflict is not None:
            raise ResourceConflictError(
                "Alias conflicts with an approved Catalog mapping"
            )
        draft_version = FieldSemanticCatalogVersion(
            f"tenant-alias-{candidate.candidate_id[:20]}-r{expected_revision + 1}"
        )
        await self._catalog.register_catalog_version(
            context.tenant_id,
            draft_version,
            context.actor_id,
            now,
        )
        source_aliases = tuple(
            alias
            for definition in source_definitions
            for alias in (*definition.aliases, *definition.negative_aliases)
        )
        await self._populate_draft_catalog(
            tenant_id=context.tenant_id,
            catalog_version=draft_version,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            decided_at=now,
            source_aliases=source_aliases,
            promoted_candidate=candidate,
        )
        decision = self._candidate_decision(
            candidate=candidate,
            status=FieldAliasStatus.APPROVED,
            authority=FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            decided_at=now,
            promoted_catalog_version=draft_version,
        )
        approved = await self._candidates.decide(
            decision,
            tenant_id=context.tenant_id,
            expected_revision=expected_revision,
            audit_event=audit_event,
        )
        return FieldAliasPromotion(approved, draft_version, support)

    async def reject_tenant_candidate(
        self,
        context: TrustedTenantContext,
        candidate_id: str,
        reason: str,
        idempotency_key: str,
    ) -> FieldAliasCandidate:
        """Reject a pending candidate and resolve its competing-mapping conflicts."""

        self._require_permission(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        candidate = await self._require_tenant_candidate(
            context.tenant_id,
            self._normalized("candidate_id", candidate_id),
        )
        reason = self._normalized("reason", reason)
        idempotency_key = self._normalized("idempotency_key", idempotency_key)
        if candidate.status not in {
            FieldAliasStatus.PENDING,
            FieldAliasStatus.REJECTED,
        }:
            raise ResourceConflictError("Only pending alias candidates may be rejected")
        now = self._now()
        if candidate.status is FieldAliasStatus.PENDING:
            decision = self._candidate_decision(
                candidate=candidate,
                status=FieldAliasStatus.REJECTED,
                authority=FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR,
                reviewer_id=context.actor_id,
                reason=reason,
                idempotency_key=idempotency_key,
                decided_at=now,
            )
        else:
            decision = FieldAliasCandidateDecision(
                decision_id=self._digest_text(
                    "field-alias-candidate-decision\0"
                    f"{candidate.candidate_id}\0{self._digest_text(idempotency_key)}"
                ),
                candidate_id=candidate.candidate_id,
                scope=candidate.scope,
                previous_status=FieldAliasStatus.PENDING,
                status=FieldAliasStatus.REJECTED,
                authority=FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR,
                reviewer_id=context.actor_id,
                reason=reason,
                idempotency_key_hash=self._digest_text(idempotency_key),
                previous_revision=max(1, candidate.revision - 1),
                revision=candidate.revision,
                policy_version=self._policy.version,
                decided_at=now,
            )
        rejected = await self._candidates.decide(
            decision,
            tenant_id=context.tenant_id,
            expected_revision=decision.previous_revision,
        )
        conflicts = await self._conflicts.list_open_for_alias_candidate(
            context.tenant_id,
            candidate.candidate_id,
        )
        for conflict in conflicts:
            await self._conflicts.resolve(
                context.tenant_id,
                conflict.conflict_id,
                MemoryConflictStatus.RESOLVED,
                decision.decision_id,
                now,
            )
        remaining = await self._candidates.list_competing(
            context.tenant_id,
            candidate.schema_version,
            candidate.document_type,
            candidate.normalized_alias,
        )
        if remaining:
            await self._record_competing_conflict(remaining[0], remaining)
        return rejected

    async def disable_tenant_alias(
        self,
        context: TrustedTenantContext,
        candidate_id: str,
        reason: str,
        idempotency_key: str,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> FieldAliasCandidate:
        """Suspend a promoted alias and invalidate every derived index of its Catalog."""

        self._require_permission(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        candidate = await self._require_tenant_candidate(
            context.tenant_id,
            self._normalized("candidate_id", candidate_id),
        )
        reason = self._normalized("reason", reason)
        idempotency_key = self._normalized("idempotency_key", idempotency_key)
        self._require_expected_revision(expected_revision)
        replay = await self._replay_candidate_decision(
            tenant_id=context.tenant_id,
            candidate_id=candidate.candidate_id,
            status=FieldAliasStatus.SUSPENDED,
            authority=FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            audit_event=audit_event,
        )
        if replay is not None:
            return replay
        self._require_current_revision(candidate, expected_revision)
        if (
            candidate.status is not FieldAliasStatus.APPROVED
            or candidate.promoted_catalog_version is None
        ):
            raise ResourceConflictError("Only promoted aliases may be suspended")
        now = self._now()
        alias_id = self._promoted_alias_id(
            context.tenant_id,
            candidate.promoted_catalog_version,
            candidate.canonical_field_path,
            candidate.normalized_alias,
            False,
        )
        alias = await self._aliases.get_alias(context.tenant_id, alias_id)
        if alias is None:
            raise ResourceConflictError("Promoted alias is absent from its Catalog")
        if alias.status is not FieldAliasStatus.SUSPENDED:
            await self._catalog.decide_alias(
                context.tenant_id,
                alias_id,
                FieldAliasStatus.SUSPENDED,
                context.actor_id,
                reason,
                f"{idempotency_key}:alias",
                now,
            )
        if self._index_projection is not None:
            await self._index_projection.invalidate_catalog(
                context.tenant_id,
                candidate.schema_version,
                candidate.promoted_catalog_version,
                reason,
            )
        decision = self._candidate_decision(
            candidate=candidate,
            status=FieldAliasStatus.SUSPENDED,
            authority=FieldAliasCandidateDecisionAuthority.TENANT_GOVERNOR,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            decided_at=now,
        )
        return await self._candidates.decide(
            decision,
            tenant_id=context.tenant_id,
            expected_revision=expected_revision,
            audit_event=audit_event,
        )

    async def invalidate_catalog_version(
        self,
        context: TrustedTenantContext,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
    ) -> bool:
        """Invalidate PostgreSQL catalog state before deleting rebuildable projections."""

        self._require_permission(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        reason = self._normalized("reason", reason)
        now = self._now()
        changed = await self._aliases.invalidate_catalog_version(
            context.tenant_id,
            self._schema_version_for_catalog(context.tenant_id, catalog_version),
            catalog_version,
            reason,
            now,
        )
        if self._index_projection is not None:
            await self._index_projection.invalidate_catalog(
                context.tenant_id,
                self._schema_version_for_catalog(context.tenant_id, catalog_version),
                catalog_version,
                reason,
            )
        return changed

    async def reissue_catalog_version(
        self,
        context: TrustedTenantContext,
        source_catalog_version: FieldSemanticCatalogVersion,
        reason: str,
        idempotency_key: str,
    ) -> FieldSemanticCatalogVersion:
        """Clone a valid historical Catalog into an inactive rollback candidate."""

        self._require_permission(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        reason = self._normalized("reason", reason)
        idempotency_key = self._normalized("idempotency_key", idempotency_key)
        schema_version = self._schema_version_for_catalog(
            context.tenant_id,
            source_catalog_version,
        )
        if not await self._aliases.is_catalog_version_published(
            context.tenant_id,
            schema_version,
            source_catalog_version,
        ):
            raise ResourceConflictError("Only a valid persisted Catalog may be reissued")
        source_aliases = await self._aliases.list_approved_aliases(
            context.tenant_id,
            schema_version,
            source_catalog_version,
        )
        idempotency_hash = self._digest_text(idempotency_key)
        draft_version = FieldSemanticCatalogVersion(
            f"catalog-reissue-{source_catalog_version.value[:48]}-{idempotency_hash[:12]}"
        )
        now = self._now()
        await self._catalog.register_catalog_version(
            context.tenant_id,
            draft_version,
            context.actor_id,
            now,
        )
        await self._populate_draft_catalog(
            tenant_id=context.tenant_id,
            catalog_version=draft_version,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            decided_at=now,
            source_aliases=source_aliases,
            promoted_candidate=None,
        )
        return draft_version

    async def propose_global_candidate(
        self,
        context: TrustedTenantContext,
        source_candidate_ids: Sequence[str],
    ) -> FieldAliasCandidate:
        """Create a pending global proposal from irreversible cross-tenant aggregates."""

        self._require_permission(context, MemoryPermission.GOVERN_GLOBAL_FIELD_ALIAS)
        if self._global_hash_salt is None:
            raise ServiceUnavailableError("Global field alias hashing is not configured")
        candidate_ids = tuple(dict.fromkeys(source_candidate_ids))
        if len(candidate_ids) < self._policy.global_min_distinct_tenants:
            raise BadRequestError("Insufficient tenant candidates for a global proposal")
        sources = await self._candidates.list_by_ids_unscoped(candidate_ids)
        if len(sources) != len(candidate_ids):
            raise ResourceNotFoundError("A global alias source candidate was not found")
        if any(
            item.scope is not FieldAliasCandidateScope.TENANT
            or item.status is not FieldAliasStatus.APPROVED
            or item.tenant_id is None
            or item.promoted_catalog_version is None
            or item.canonical_collision
            for item in sources
        ):
            raise ResourceConflictError(
                "Global proposals require approved, non-colliding tenant candidates"
            )
        published_sources = tuple(
            [
                await self._aliases.is_catalog_version_published(
                    source.tenant_id,
                    source.schema_version,
                    source.promoted_catalog_version,
                )
                for source in sources
                if source.tenant_id is not None
                and source.promoted_catalog_version is not None
            ]
        )
        if len(published_sources) != len(sources) or not all(published_sources):
            raise ResourceConflictError(
                "Global proposal sources must belong to published, valid Catalogs"
            )
        tenant_ids = tuple(item.tenant_id for item in sources if item.tenant_id is not None)
        if len(set(tenant_ids)) < self._policy.global_min_distinct_tenants:
            raise ResourceConflictError("Global proposal sources lack tenant diversity")
        semantic_keys = {
            (
                item.schema_version,
                item.document_type,
                item.canonical_field_path,
                item.normalized_alias,
            )
            for item in sources
        }
        if len(semantic_keys) != 1:
            raise ResourceConflictError(
                "Global alias sources disagree on label or canonical field"
            )
        schema_version, document_type, field_path, normalized_alias = next(
            iter(semantic_keys)
        )
        now = self._now()
        summaries = tuple(
            [
                await self._candidates.summarize_support(
                    source.tenant_id or "",
                    source.candidate_id,
                    now,
                )
                for source in sources
            ]
        )
        tenant_fingerprints = tuple(
            sorted(
                self._global_digest("tenant", {"tenant_id": tenant_id})
                for tenant_id in set(tenant_ids)
            )
        )
        aggregate_counts = {
            "source_count": sum(item.source_count for item in summaries),
            "distinct_documents": sum(item.distinct_documents for item in summaries),
            "distinct_templates": sum(item.distinct_templates for item in summaries),
            "distinct_reviewers": sum(item.distinct_reviewers for item in summaries),
        }
        global_id = self._global_digest(
            "field-alias-global-candidate",
            {
                "policy_version": self._policy.version,
                "schema_version": schema_version,
                "document_type": document_type,
                "canonical_field_path": field_path,
                "normalized_alias": normalized_alias,
                "tenant_fingerprints": tenant_fingerprints,
                **aggregate_counts,
            },
        )
        snapshot = GlobalFieldAliasSupportSnapshot(
            candidate_id=global_id,
            salt_version=self._global_hash_salt_version,
            tenant_fingerprints=tenant_fingerprints,
            source_count=aggregate_counts["source_count"],
            distinct_documents=aggregate_counts["distinct_documents"],
            distinct_templates=aggregate_counts["distinct_templates"],
            distinct_reviewers=aggregate_counts["distinct_reviewers"],
            created_at=now,
        )
        candidate = FieldAliasCandidate(
            candidate_id=global_id,
            scope=FieldAliasCandidateScope.GLOBAL,
            tenant_id=None,
            schema_version=schema_version,
            document_type=document_type,
            canonical_field_path=field_path,
            alias_text=normalized_alias,
            normalized_alias=normalized_alias,
            policy_version=self._policy.version,
            context_anchors=(),
            status=FieldAliasStatus.PENDING,
            canonical_collision=False,
            support_window_days=self._policy.support_window_days,
            revision=1,
            submitted_at=now,
            updated_at=now,
        )
        return await self._candidates.save_pending(candidate, None, snapshot)

    async def approve_global_candidate(
        self,
        context: TrustedTenantContext,
        candidate_id: str,
        reason: str,
        idempotency_key: str,
    ) -> FieldAliasCandidate:
        """Approve global governance metadata without injecting any tenant index."""

        self._require_permission(context, MemoryPermission.GOVERN_GLOBAL_FIELD_ALIAS)
        candidate_id = self._normalized("candidate_id", candidate_id)
        reason = self._normalized("reason", reason)
        idempotency_key = self._normalized("idempotency_key", idempotency_key)
        candidate = await self._candidates.get_global(candidate_id)
        if candidate is None:
            raise ResourceNotFoundError("Global alias candidate was not found")
        if candidate.status is FieldAliasStatus.APPROVED:
            return candidate
        if candidate.status is not FieldAliasStatus.PENDING:
            raise ResourceConflictError("Global alias candidate is not approvable")
        support = await self._candidates.get_global_support(candidate_id)
        if (
            support is None
            or len(support.tenant_fingerprints)
            < self._policy.global_min_distinct_tenants
        ):
            raise ResourceConflictError("Global alias candidate lacks tenant diversity")
        decision = self._candidate_decision(
            candidate=candidate,
            status=FieldAliasStatus.APPROVED,
            authority=FieldAliasCandidateDecisionAuthority.GLOBAL_GOVERNOR,
            reviewer_id=context.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            decided_at=self._now(),
        )
        return await self._candidates.decide(
            decision,
            tenant_id=None,
            expected_revision=candidate.revision,
        )

    async def _record_competing_conflict(
        self,
        candidate: FieldAliasCandidate,
        competing: Sequence[FieldAliasCandidate],
    ) -> MemoryConflictRecord | None:
        conflicting = tuple(
            item
            for item in competing
            if item.canonical_field_path != candidate.canonical_field_path
        )
        if not conflicting or candidate.tenant_id is None:
            return None
        candidates = tuple(
            sorted(
                {candidate, *conflicting},
                key=lambda item: item.candidate_id,
            )
        )
        candidate_ids = tuple(item.candidate_id for item in candidates)
        candidate_paths = tuple(
            sorted({item.canonical_field_path for item in candidates})
        )
        fingerprint = self._tenant_digest(
            candidate.tenant_id,
            "field-alias-conflict",
            {
                "schema_version": candidate.schema_version,
                "document_type": candidate.document_type,
                "normalized_alias": candidate.normalized_alias,
                "candidate_ids": candidate_ids,
                "candidate_field_paths": candidate_paths,
            },
        )
        conflict = MemoryConflictRecord(
            conflict_id=self._digest_text(f"field-alias-conflict\0{fingerprint}"),
            tenant_id=candidate.tenant_id,
            example_ids=(),
            document_type=candidate.document_type,
            field_path=candidate.normalized_alias,
            schema_version=candidate.schema_version,
            conflict_type="field_alias_competing_binding",
            fingerprint=fingerprint,
            reason_codes=("field_alias.same_label_multiple_fields",),
            evidence_references=tuple(
                f"field-alias-candidate:{item}" for item in candidate_ids
            ),
            status=MemoryConflictStatus.OPEN,
            detected_at=max(item.submitted_at for item in candidates),
            resolved_at=None,
            resolution_decision_id=None,
            field_alias_candidate_ids=candidate_ids,
            candidate_field_paths=candidate_paths,
        )
        return await self._conflicts.save(conflict)

    async def _record_catalog_alias_conflict(
        self,
        candidate: FieldAliasCandidate,
        definitions: Sequence[FieldSemanticDefinition],
    ) -> MemoryConflictRecord | None:
        if candidate.tenant_id is None:
            return None
        conflicting_aliases = tuple(
            sorted(
                (
                    definition.canonical_field_path,
                    alias.alias_id,
                )
                for definition in definitions
                if definition.canonical_field_path != candidate.canonical_field_path
                for alias in definition.aliases
                if alias.normalized_alias == candidate.normalized_alias
            )
        )
        if not conflicting_aliases:
            return None
        candidate_paths = tuple(
            sorted(
                {
                    candidate.canonical_field_path,
                    *(field_path for field_path, _ in conflicting_aliases),
                }
            )
        )
        alias_ids = tuple(alias_id for _, alias_id in conflicting_aliases)
        fingerprint = self._tenant_digest(
            candidate.tenant_id,
            "field-alias-catalog-conflict",
            {
                "schema_version": candidate.schema_version,
                "document_type": candidate.document_type,
                "normalized_alias": candidate.normalized_alias,
                "candidate_id": candidate.candidate_id,
                "catalog_alias_ids": alias_ids,
                "candidate_field_paths": candidate_paths,
            },
        )
        return await self._conflicts.save(
            MemoryConflictRecord(
                conflict_id=self._digest_text(
                    f"field-alias-catalog-conflict\0{fingerprint}"
                ),
                tenant_id=candidate.tenant_id,
                example_ids=(),
                document_type=candidate.document_type,
                field_path=candidate.normalized_alias,
                schema_version=candidate.schema_version,
                conflict_type="field_alias_catalog_competing_binding",
                fingerprint=fingerprint,
                reason_codes=("field_alias.catalog_label_multiple_fields",),
                evidence_references=(
                    f"field-alias-candidate:{candidate.candidate_id}",
                    *(f"field-semantic-alias:{alias_id}" for alias_id in alias_ids),
                ),
                status=MemoryConflictStatus.OPEN,
                detected_at=candidate.submitted_at,
                resolved_at=None,
                resolution_decision_id=None,
                field_alias_candidate_ids=(candidate.candidate_id,),
                candidate_field_paths=candidate_paths,
            )
        )

    async def _populate_draft_catalog(
        self,
        *,
        tenant_id: str,
        catalog_version: FieldSemanticCatalogVersion,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        decided_at: datetime,
        source_aliases: Sequence[FieldAlias],
        promoted_candidate: FieldAliasCandidate | None,
    ) -> None:
        aliases: dict[
            tuple[str, str, str, bool],
            tuple[str, str, tuple[FieldContextAnchor, ...]],
        ] = {}
        for alias in source_aliases:
            aliases[
                (
                    alias.document_type,
                    alias.canonical_field_path,
                    alias.normalized_alias,
                    alias.is_negative,
                )
            ] = (alias.alias_text, alias.submitted_by, alias.context_anchors)
        if promoted_candidate is not None:
            aliases[
                (
                    promoted_candidate.document_type,
                    promoted_candidate.canonical_field_path,
                    promoted_candidate.normalized_alias,
                    False,
                )
            ] = (
                promoted_candidate.alias_text,
                reviewer_id,
                promoted_candidate.context_anchors,
            )
        for key in sorted(aliases):
            document_type, field_path, normalized_alias, is_negative = key
            alias_text, submitted_by, anchors = aliases[key]
            alias_id = self._promoted_alias_id(
                tenant_id,
                catalog_version,
                field_path,
                normalized_alias,
                is_negative,
            )
            existing = await self._aliases.get_alias(tenant_id, alias_id)
            if existing is None:
                await self._catalog.submit_alias(
                    FieldAlias(
                        alias_id=alias_id,
                        tenant_id=tenant_id,
                        schema_version=self._schema_version,
                        document_type=document_type,
                        canonical_field_path=field_path,
                        alias_text=alias_text,
                        normalized_alias=normalized_alias,
                        is_negative=is_negative,
                        context_anchors=anchors,
                        catalog_version=catalog_version,
                        status=FieldAliasStatus.PENDING,
                        submitted_by=submitted_by,
                        submitted_at=decided_at,
                        reviewed_by=None,
                        reviewed_at=None,
                        review_reason=None,
                    )
                )
                existing = await self._aliases.get_alias(tenant_id, alias_id)
            if existing is None:
                raise ResourceConflictError("Draft Catalog alias was not persisted")
            if existing.status is FieldAliasStatus.PENDING:
                await self._catalog.decide_alias(
                    tenant_id,
                    alias_id,
                    FieldAliasStatus.APPROVED,
                    reviewer_id,
                    reason,
                    f"{idempotency_key}:catalog-alias:{alias_id}",
                    decided_at,
                )
            elif existing.status is not FieldAliasStatus.APPROVED:
                raise ResourceConflictError("Draft Catalog contains a non-approved alias")

    def _require_tenant_support(
        self,
        candidate: FieldAliasCandidate,
        support: FieldAliasSupportSummary,
    ) -> None:
        failures: list[str] = []
        if support.distinct_documents < self._policy.min_distinct_documents:
            failures.append("documents")
        if support.distinct_templates < self._policy.min_distinct_templates:
            failures.append("templates")
        if support.distinct_reviewers < self._policy.min_distinct_reviewers:
            failures.append("reviewers")
        if failures:
            raise ResourceConflictError(
                "Alias candidate lacks required support diversity: " + ", ".join(failures)
            )
        if support.candidate_id != candidate.candidate_id:
            raise ResourceConflictError("Alias support scope is inconsistent")

    async def _require_tenant_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> FieldAliasCandidate:
        candidate = await self._candidates.get(tenant_id, candidate_id)
        if candidate is None:
            raise ResourceNotFoundError("Tenant alias candidate was not found")
        return candidate

    def _candidate_decision(
        self,
        *,
        candidate: FieldAliasCandidate,
        status: FieldAliasStatus,
        authority: FieldAliasCandidateDecisionAuthority,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        decided_at: datetime,
        promoted_catalog_version: FieldSemanticCatalogVersion | None = None,
    ) -> FieldAliasCandidateDecision:
        key_hash = self._digest_text(idempotency_key)
        return FieldAliasCandidateDecision(
            decision_id=self._digest_text(
                f"field-alias-candidate-decision\0{candidate.candidate_id}\0{key_hash}"
            ),
            candidate_id=candidate.candidate_id,
            scope=candidate.scope,
            previous_status=candidate.status,
            status=status,
            authority=authority,
            reviewer_id=reviewer_id,
            reason=reason,
            idempotency_key_hash=key_hash,
            previous_revision=candidate.revision,
            revision=candidate.revision + 1,
            policy_version=self._policy.version,
            decided_at=decided_at,
            promoted_catalog_version=promoted_catalog_version,
        )

    async def _replay_candidate_decision(
        self,
        *,
        tenant_id: str | None,
        candidate_id: str,
        status: FieldAliasStatus,
        authority: FieldAliasCandidateDecisionAuthority,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None,
    ) -> FieldAliasCandidate | None:
        key_hash = self._digest_text(idempotency_key)
        replay = await self._candidates.get_decision_by_idempotency_hash(
            tenant_id,
            candidate_id,
            key_hash,
        )
        if replay is None:
            return None
        if any(
            (
                replay.status is not status,
                replay.authority is not authority,
                replay.reviewer_id != reviewer_id,
                replay.reason != reason,
                replay.previous_revision != expected_revision,
            )
        ):
            raise ResourceConflictError(
                "Alias candidate idempotency key is bound to another request"
            )
        return await self._candidates.decide(
            replay,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
            audit_event=audit_event,
        )

    @staticmethod
    def _require_expected_revision(expected_revision: int) -> None:
        if expected_revision <= 0:
            raise BadRequestError("expected_revision must be greater than zero")

    @staticmethod
    def _require_current_revision(
        candidate: FieldAliasCandidate,
        expected_revision: int,
    ) -> None:
        if candidate.revision != expected_revision:
            raise ResourceConflictError("Alias candidate revision is stale")

    def _candidate_anchors(
        self,
        candidate_id: str,
        evidence: FieldBindingEvidence,
    ) -> tuple[FieldContextAnchor, ...]:
        return tuple(
            FieldContextAnchor(
                anchor_id=self._digest_text(
                    f"field-alias-candidate-anchor\0{candidate_id}\0{ordinal}"
                ),
                text=item.text,
                normalized_text=item.normalized_text,
                relation=item.relation,
                max_distance=(
                    item.distance if item.distance is not None and item.distance > 0 else None
                ),
                is_negative=False,
            )
            for ordinal, item in enumerate(evidence.context_observations)
        )

    def _template_fingerprint(
        self,
        tenant_id: str,
        template_features: Mapping[str, JsonValue],
    ) -> str | None:
        if not template_features:
            return None
        payload = self._canonical(dict(template_features))
        return hmac.new(
            self._template_fingerprint_salt.encode("utf-8"),
            f"field-alias-template\0{tenant_id}\0{payload}".encode("utf-8"),
            sha256,
        ).hexdigest()

    def _tenant_digest(
        self,
        tenant_id: str,
        namespace: str,
        payload: object,
    ) -> str:
        return hmac.new(
            self._fingerprint_salt.encode("utf-8"),
            f"{namespace}\0{tenant_id}\0{self._canonical(payload)}".encode("utf-8"),
            sha256,
        ).hexdigest()

    def _global_digest(self, namespace: str, payload: object) -> str:
        if self._global_hash_salt is None:
            raise ServiceUnavailableError("Global field alias hashing is not configured")
        return hmac.new(
            self._global_hash_salt.encode("utf-8"),
            f"{namespace}\0{self._canonical(payload)}".encode("utf-8"),
            sha256,
        ).hexdigest()

    @staticmethod
    def _promoted_alias_id(
        tenant_id: str,
        catalog_version: FieldSemanticCatalogVersion,
        canonical_field_path: str,
        normalized_alias: str,
        is_negative: bool,
    ) -> str:
        identity = (
            f"field-alias-catalog-entry\0{tenant_id}\0{catalog_version.value}\0"
            f"{canonical_field_path}\0{normalized_alias}\0{int(is_negative)}"
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_labels(field_path: str, display_name: str) -> frozenset[str]:
        return frozenset(
            {
                normalize_field_label(field_path),
                normalize_field_label(field_path.rsplit(".", 1)[-1]),
                normalize_field_label(display_name),
            }
        )

    def _schema_version_for_catalog(
        self,
        tenant_id: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> str:
        del tenant_id, catalog_version
        return self._schema_version

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _digest_text(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_permission(
        context: TrustedTenantContext,
        permission: MemoryPermission,
    ) -> None:
        if not context.permits(permission):
            raise ForbiddenError(f"Missing required permission: {permission.value}")

    @classmethod
    def _normalized(cls, name: str, value: str) -> str:
        cls._require_text(name, value)
        return value

    @staticmethod
    def _require_text(name: str, value: str) -> None:
        if not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be non-empty and normalized")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Field alias learning clock must be timezone-aware")
        return now
