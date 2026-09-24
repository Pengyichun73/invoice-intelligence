"""Project reviewed PostgreSQL cases into a rebuildable hybrid-index boundary."""

import json
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from invoice_intelligence.application.errors import CorrectionMemoryError
from invoice_intelligence.application.ports.admission import MemoryAdmissionRepository
from invoice_intelligence.application.ports.examples import (
    DenseEmbeddingProvider,
    ExampleIndexStore,
    ExampleRedactor,
    IndexProjectionRepository,
    SparseEmbeddingProvider,
)
from invoice_intelligence.application.ports.index_integrity import IndexIntegrityVerifier
from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.application.ports.projection_lease import ProjectionLease
from invoice_intelligence.application.services.projection_heartbeat import projection_heartbeat
from invoice_intelligence.domain.examples import (
    ExampleCandidate,
    ExampleVectorProjection,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    ReviewedExample,
    SparseVector,
)
from invoice_intelligence.domain.governance import GovernanceAuditEvent, IndexGovernanceRecord


@dataclass(frozen=True, slots=True)
class ProjectionBatchResult:
    """Counters for one bounded, retryable projection pass."""

    tenant_id: str
    index_version: IndexVersion
    claimed: int
    indexed: int
    failed: int


class ExampleIndexProjectionService:
    """Coordinate PostgreSQL projection state and a future Milvus adapter.

    PostgreSQL state is claimed and committed independently from the derived index. A
    crash after ``upsert`` but before ``mark_projected`` is safe because the same stable
    candidate checksum is upserted again and no source occurrence counter is changed.
    """

    def __init__(
        self,
        *,
        projection_repository: IndexProjectionRepository,
        admission_repository: MemoryAdmissionRepository,
        redactor: ExampleRedactor,
        index_store: ExampleIndexStore,
        dense_embedding_provider: DenseEmbeddingProvider,
        sparse_embedding_provider: SparseEmbeddingProvider | None,
        redaction_policy_version: str,
        batch_size: int = 32,
        retention_days: int | None = None,
        privacy_telemetry: PrivacyTelemetry | None = None,
        integrity_verifier: IndexIntegrityVerifier | None = None,
    ) -> None:
        if not redaction_policy_version.strip():
            raise ValueError("redaction_policy_version must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if retention_days is not None and retention_days <= 0:
            raise ValueError("retention_days must be greater than zero when configured")
        self._projection_repository = projection_repository
        self._admissions = admission_repository
        self._redactor = redactor
        self._index_store = index_store
        self._dense_embeddings = dense_embedding_provider
        self._sparse_embeddings = sparse_embedding_provider
        self._redaction_policy_version = redaction_policy_version
        self._batch_size = batch_size
        self._retention_days = retention_days
        self._privacy_telemetry = privacy_telemetry
        self._integrity_verifier = integrity_verifier

    async def register_index_version(
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
        """Register an immutable build and enqueue only eligible PostgreSQL cases."""

        await self._projection_repository.register_version(
            tenant_id,
            index_version,
            schema_version,
            dense_model_version,
            sparse_model_version,
            rerank_model_version,
            prompt_version,
            audit_event,
        )

    async def activate_index_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        """Switch a verified derived collection, then commit the PostgreSQL active version."""

        if not await self.verify_index_version(tenant_id, index_version):
            raise ValueError("Reviewed-example index version is not ready for activation")
        previous = await self._projection_repository.get_active_version(tenant_id)
        await self._index_store.switch_alias(tenant_id, index_version)
        try:
            await self._projection_repository.activate_version(tenant_id, index_version)
        except Exception:
            if previous is not None and previous != index_version:
                await self._index_store.switch_alias(tenant_id, previous)
            raise

    async def list_index_versions(self, tenant_id: str) -> tuple[IndexVersion, ...]:
        return await self._projection_repository.list_index_versions(tenant_id)

    async def get_index_governance(
        self, tenant_id: str, index_version: IndexVersion
    ) -> IndexGovernanceRecord | None:
        return await self._projection_repository.get_index_governance(tenant_id, index_version)

    async def verify_index_version(self, tenant_id: str, index_version: IndexVersion) -> bool:
        record = await self._projection_repository.get_index_governance(tenant_id, index_version)
        if record is None or not record.is_valid:
            return False
        if (
            record.projection_counts.pending
            or record.projection_counts.processing
            or record.projection_counts.failed
        ):
            return False
        await self._index_store.ensure_collection(tenant_id, index_version)
        if self._integrity_verifier is not None:
            return await self._integrity_verifier.verify_examples(tenant_id, index_version)
        return True

    async def schedule_approved_example(
        self,
        tenant_id: str,
        example_id: str,
        schema_version: str,
    ) -> IndexVersion | None:
        """Idempotently enqueue one approved case for the compatible active index."""

        active_version = await self._projection_repository.get_active_version(tenant_id)
        if active_version is None:
            return None
        index = await self._projection_repository.get_index_governance(
            tenant_id,
            active_version,
        )
        if (
            index is None
            or not index.is_active
            or not index.is_valid
            or index.schema_version != schema_version
        ):
            return None
        await self._projection_repository.schedule(
            tenant_id,
            (example_id,),
            active_version,
        )
        return active_version

    async def remove_derived_example(
        self,
        tenant_id: str,
        example_id: str,
        schema_version: str,
    ) -> tuple[IndexVersion, ...]:
        """Delete one ineligible case from every rebuildable derived index version."""

        versions = await self._projection_repository.list_index_versions(
            tenant_id,
            schema_version=schema_version,
        )
        for version in versions:
            await self._index_store.delete(tenant_id, (example_id,), version)
        return versions

    async def project_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        *,
        limit: int | None = None,
        trace_id: str | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 300.0,
    ) -> ProjectionBatchResult:
        """Claim and project one bounded batch; failed rows remain retryable."""

        span = (
            self._privacy_telemetry.span(
                trace_id=trace_id or uuid4().hex,
                tenant_id=tenant_id,
                stage=TraceStage.INDEX_PROJECTION,
                operation="project_reviewed_examples",
                attributes={"index_version": index_version.value},
            )
            if self._privacy_telemetry is not None
            else nullcontext()
        )
        with span:
            return await self._project_pending(
                tenant_id, index_version, limit=limit,
                worker_id=worker_id or f"api-projection:{uuid4().hex}",
                lease_seconds=lease_seconds,
            )

    async def _project_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        *,
        limit: int | None = None,
        worker_id: str,
        lease_seconds: float,
    ) -> ProjectionBatchResult:

        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        claim_limit = limit if limit is not None else self._batch_size
        if claim_limit <= 0:
            raise ValueError("Projection limit must be greater than zero")
        indexed = 0
        failed = 0
        claimed = 0
        for _ in range(claim_limit):
            leases = await self._projection_repository.claim_pending(
                tenant_id, index_version, 1, worker_id, lease_seconds,
            )
            if not leases:
                break
            lease = leases[0]
            claimed += 1
            try:
                await self._project_one(lease, index_version, lease_seconds)
            except Exception as exc:
                failed += 1
                await self._record_failure(tenant_id, lease, index_version, exc)
            else:
                indexed += 1
        return ProjectionBatchResult(
            tenant_id=tenant_id,
            index_version=index_version,
            claimed=claimed,
            indexed=indexed,
            failed=failed,
        )

    async def requeue_stale(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        """Return abandoned processing leases to the database-owned pending queue."""

        return await self._projection_repository.requeue_stale(
            tenant_id,
            index_version,
            stale_before,
        )

    async def invalidate_example(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion | Sequence[IndexVersion],
        reason: str,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> bool:
        """Invalidate the source first, then remove its derived index document."""

        changed = await self._projection_repository.invalidate(
            tenant_id,
            example_id,
            reason,
            audit_event,
        )
        # Delete is idempotent and is also required when a previous invalidation
        # committed but its derived-index cleanup was interrupted.
        for version in self._versions(index_version):
            await self._index_store.delete(tenant_id, (example_id,), version)
        return changed

    async def invalidate_schema(
        self,
        tenant_id: str,
        schema_version: str,
        index_version: IndexVersion | Sequence[IndexVersion],
        reason: str,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> int:
        """Invalidate source cases and the corresponding derived index version."""

        count = await self._projection_repository.invalidate_schema(
            tenant_id,
            schema_version,
            reason,
            audit_event,
        )
        # The index version is tied to a Schema version; delete even when count is zero
        # so a retry can clean up a stale derived collection after a partial failure.
        for version in self._versions(index_version):
            await self._index_store.delete_tenant(tenant_id, version)
        return count

    async def delete_tenant(
        self,
        tenant_id: str,
        index_versions: Sequence[IndexVersion],
    ) -> int:
        """Delete canonical tenant cases, then best-effort all supplied derived versions."""

        versions = self._versions(index_versions)
        count = await self._projection_repository.delete_tenant(tenant_id)
        for index_version in versions:
            await self._index_store.delete_tenant(tenant_id, index_version)
        return count

    async def enforce_retention(
        self,
        tenant_id: str,
        older_than: datetime,
        index_versions: Sequence[IndexVersion],
        reason: str = "retention_policy_expired",
    ) -> int:
        """Invalidate facts, clean every derived index, then purge source cases."""

        versions = self._versions(index_versions)
        example_ids = await self._projection_repository.invalidate_expired(
            tenant_id,
            older_than,
            reason,
        )
        if not example_ids:
            return 0
        for index_version in versions:
            await self._index_store.delete(tenant_id, example_ids, index_version)
        return await self._projection_repository.purge_invalidated(
            tenant_id,
            example_ids,
        )

    async def enforce_configured_retention(
        self,
        tenant_id: str,
        index_versions: Sequence[IndexVersion],
        *,
        now: datetime | None = None,
    ) -> int:
        """Apply the configured tenant retention window when enabled."""

        if self._retention_days is None:
            return 0
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("Retention clock must be timezone-aware")
        return await self.enforce_retention(
            tenant_id,
            current - timedelta(days=self._retention_days),
            index_versions,
        )

    async def _project_one(
        self,
        lease: ProjectionLease[ReviewedExample],
        index_version: IndexVersion,
        lease_seconds: float,
    ) -> None:
        example = lease.item
        if not example.is_reviewed or not example.is_valid:
            raise ValueError("Only reviewed and valid examples may be projected")
        approved = await self._admissions.filter_approved_example_ids(
            example.tenant_id,
            (example.example_id,),
        )
        if approved != (example.example_id,):
            raise ValueError("Only admission-approved examples may be projected")
        candidate = self._redactor.redact_example(
            example,
            index_version,
            self._redaction_policy_version,
        )
        if (
            candidate.example_id != example.example_id
            or candidate.scope != example.scope
            or candidate.index_version != index_version
        ):
            raise ValueError("Redactor changed the reviewed-example scope")
        sectioned_text = self._build_index_text(candidate)
        candidate = replace(candidate, redacted_index_text=sectioned_text)
        async with projection_heartbeat(
            lambda: self._projection_repository.renew_lease(
                example.tenant_id, example.example_id, index_version,
                lease.worker_id, lease.token, lease_seconds,
            ), lease_seconds,
        ):
            dense = await self._embed_dense(sectioned_text)
            sparse = await self._embed_sparse(sectioned_text)
            projection = ExampleVectorProjection(
                candidate=candidate,
                dense_embedding=dense,
                sparse_embedding=sparse,
                projection_checksum=self._projection_checksum(candidate),
            )
            await self._index_store.upsert((projection,))
        await self._projection_repository.mark_projected(
            example.tenant_id, example.example_id, index_version,
            projection.projection_checksum, lease.worker_id, lease.token,
        )

    async def _embed_dense(self, text: str) -> tuple[float, ...]:
        vectors = await self._dense_embeddings.embed((text,))
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("Dense embedding provider returned an invalid result")
        return tuple(float(value) for value in vectors[0])

    async def _embed_sparse(self, text: str) -> SparseVector | None:
        if self._sparse_embeddings is None:
            # Milvus can derive BM25 sparse vectors at collection level; a remote sparse
            # provider is therefore optional until that adapter is connected.
            return None
        vectors = await self._sparse_embeddings.embed((text,))
        if len(vectors) != 1:
            raise ValueError("Sparse embedding provider returned an invalid result")
        return tuple(
            sorted(
                ((int(index), float(weight)) for index, weight in vectors[0]),
                key=lambda item: item[0],
            )
        )

    async def _record_failure(
        self,
        tenant_id: str,
        lease: ProjectionLease[ReviewedExample],
        index_version: IndexVersion,
        error: Exception,
    ) -> None:
        error_code = self._error_code(error)
        try:
            await self._projection_repository.mark_failed(
                tenant_id,
                lease.item.example_id,
                index_version,
                error_code,
                lease.worker_id,
                lease.token,
            )
        except Exception as mark_error:
            raise CorrectionMemoryError(
                "Unable to persist the projection failure state"
            ) from mark_error

    @classmethod
    def _build_index_text(cls, candidate: ExampleCandidate) -> str:
        """Build a stable, auditable text layout for hybrid retrieval."""

        evidence = candidate.evidence_reference
        sections = (
            ("DOCUMENT_CONTEXT", {
                "document_type": candidate.scope.document_type,
                "schema_version": candidate.scope.schema_version,
                "document_reference": evidence.document_reference,
                "image_reference": evidence.image_reference,
                "page_number": evidence.page_number,
            }),
            ("FIELD_PATH", candidate.scope.field_path),
            ("MODEL_VALUE", candidate.redacted_model_value),
            ("REVIEWED_VALUE", candidate.redacted_reviewed_value),
            ("CORRECTION_REASON", candidate.redacted_correction_reason),
            ("VENDOR_TEMPLATE_FEATURES", {
                "vendor_fingerprint": candidate.vendor_fingerprint,
                "template_fingerprint": candidate.template_fingerprint,
            }),
            ("LABEL_TYPE", candidate.label_type.value),
        )
        lines: list[str] = []
        for name, value in sections:
            lines.extend((name, cls._canonical(value)))
        text = "\n".join(lines)
        lowered = text.casefold()
        if "base64," in lowered or "data:image" in lowered:
            raise ValueError("Index text must not contain inline image data")
        return text

    @classmethod
    def _projection_checksum(cls, candidate: ExampleCandidate) -> str:
        payload = {
            "example_id": candidate.example_id,
            "scope": {
                "tenant_id": candidate.scope.tenant_id,
                "document_type": candidate.scope.document_type,
                "field_path": candidate.scope.field_path,
                "schema_version": candidate.scope.schema_version,
            },
            "label_type": candidate.label_type.value,
            "model_value": candidate.redacted_model_value,
            "reviewed_value": candidate.redacted_reviewed_value,
            "correction_reason": candidate.redacted_correction_reason,
            "vendor_fingerprint": candidate.vendor_fingerprint,
            "template_fingerprint": candidate.template_fingerprint,
            "evidence_reference": {
                "document_reference": candidate.evidence_reference.document_reference,
                "image_reference": candidate.evidence_reference.image_reference,
                "page_number": candidate.evidence_reference.page_number,
            },
            "index_text": candidate.redacted_index_text,
            "redaction_policy_version": candidate.redaction_policy_version,
            "index_version": candidate.index_version.value,
        }
        return sha256(cls._canonical(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _error_code(error: Exception) -> str:
        name = type(error).__name__.strip() or "ProjectionError"
        return f"projection_{name[:112]}"

    @staticmethod
    def _versions(
        index_versions: IndexVersion | Sequence[IndexVersion],
    ) -> tuple[IndexVersion, ...]:
        if isinstance(index_versions, IndexVersion):
            return (index_versions,)
        versions = tuple(dict.fromkeys(index_versions))
        if not versions:
            raise ValueError("At least one index version is required")
        return versions

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
