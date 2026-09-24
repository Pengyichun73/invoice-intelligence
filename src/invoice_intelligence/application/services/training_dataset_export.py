"""Build redacted, leakage-resistant hard-negative dataset artifacts."""

import hmac
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Callable

from invoice_intelligence.application.errors import TrainingDataError
from invoice_intelligence.application.ports.examples import ReviewedExampleRepository
from invoice_intelligence.application.ports.training import (
    TrainingDataRedactor,
    TrainingDataRepository,
    TrainingDatasetExporter,
    TrainingRedactionProfile,
)
from invoice_intelligence.domain.examples import ExampleLabelType, ReviewedExample
from invoice_intelligence.domain.training import (
    CandidateReviewStatus,
    CrossTenantTrainingAuthorization,
    HardNegativeCandidate,
    TrainingDatasetRecord,
    TrainingDatasetStatus,
    TrainingDatasetVersion,
    TrainingRecord,
    TrainingRecordScope,
    TrainingSplit,
)
from invoice_intelligence.domain.workflow import JsonValue

_POSITIVE_LABELS = {
    ExampleLabelType.CONFIRMED_CORRECT,
    ExampleLabelType.CORRECTED,
}


@dataclass(frozen=True, slots=True)
class DatasetSplitPolicy:
    """Deterministic group split; values are ratios, not sampling probabilities."""

    rule_version: str
    salt_version: str
    train_ratio: float
    validation_ratio: float
    evaluation_ratio: float

    def __post_init__(self) -> None:
        if not self.rule_version.strip() or not self.salt_version.strip():
            raise ValueError("Split rule and salt versions must not be empty")
        ratios = (self.train_ratio, self.validation_ratio, self.evaluation_ratio)
        if any(value < 0 or value > 1 for value in ratios):
            raise ValueError("Split ratios must be between zero and one")
        if abs(sum(ratios) - 1.0) > 1e-9:
            raise ValueError("Split ratios must sum to one")
        if self.train_ratio <= 0:
            raise ValueError("Training split ratio must be greater than zero")


@dataclass(frozen=True, slots=True)
class TrainingDatasetExportRequest:
    """Explicit version and governance bindings for one frozen export."""

    dataset_id: str
    dataset_version: str
    tenant_ids: tuple[str, ...]
    schema_version: str
    generation_rule_version: str
    redaction_policy_version: str
    created_by: str
    authorization: CrossTenantTrainingAuthorization | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("dataset_version", self.dataset_version),
            ("schema_version", self.schema_version),
            ("generation_rule_version", self.generation_rule_version),
            ("redaction_policy_version", self.redaction_policy_version),
            ("created_by", self.created_by),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not self.tenant_ids:
            raise ValueError("Training export requires at least one tenant")
        if any(not value.strip() or value != value.strip() for value in self.tenant_ids):
            raise ValueError("Training export tenant identifiers must be normalized")
        if len(self.tenant_ids) != len(set(self.tenant_ids)):
            raise ValueError("Training export tenant identifiers must be unique")


@dataclass(frozen=True, slots=True)
class TrainingDatasetExportResult:
    """Frozen PostgreSQL version and provider-neutral artifact references."""

    dataset: TrainingDatasetVersion
    records: tuple[TrainingDatasetRecord, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRecord:
    source_tenant_id: str
    positive_example_id: str
    source_document_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    group_keys: tuple[str, ...]
    record: TrainingRecord
    fingerprint: str


class TrainingDatasetExportService:
    """Export only approved human-grounded pairs; never invoke a training API."""

    def __init__(
        self,
        *,
        examples: ReviewedExampleRepository,
        training_data: TrainingDataRepository,
        redactor: TrainingDataRedactor,
        exporter: TrainingDatasetExporter,
        split_policy: DatasetSplitPolicy,
        split_salt: str,
        cross_tenant_enabled: bool = False,
        page_size: int = 500,
        max_candidates: int = 100_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not split_salt.strip():
            raise ValueError("Dataset split salt must not be empty")
        if page_size <= 0 or max_candidates <= 0:
            raise ValueError("Dataset export limits must be greater than zero")
        self._examples = examples
        self._training_data = training_data
        self._redactor = redactor
        self._exporter = exporter
        self._split_policy = split_policy
        self._split_salt = split_salt
        self._cross_tenant_enabled = cross_tenant_enabled
        self._page_size = page_size
        self._max_candidates = max_candidates
        self._clock = clock or (lambda: datetime.now(UTC))

    async def export(
        self,
        request: TrainingDatasetExportRequest,
    ) -> TrainingDatasetExportResult:
        """Create JSONL/manifest artifacts from approved candidates without training."""

        now = self._clock()
        if now.tzinfo is None:
            raise TrainingDataError("Dataset clock must return a timezone-aware datetime")
        tenants = tuple(sorted(request.tenant_ids))
        cross_tenant = len(tenants) > 1
        cluster_assignments = self._validate_governance(
            request,
            tenants,
            cross_tenant,
            now,
        )
        profiles = {
            tenant_id: self._redactor.training_profile(tenant_id)
            for tenant_id in tenants
        }
        if any(profile.strategy == "none" for profile in profiles.values()):
            raise TrainingDataError("Training export forbids the none redaction strategy")

        candidates_by_tenant = {
            tenant_id: await self._load_approved_candidates(
                tenant_id,
                request.schema_version,
            )
            for tenant_id in tenants
        }
        prepared: list[_PreparedRecord] = []
        for tenant_id in tenants:
            prepared.extend(
                await self._prepare_tenant_records(
                    tenant_id=tenant_id,
                    schema_version=request.schema_version,
                    candidates=candidates_by_tenant[tenant_id],
                    cross_tenant=cross_tenant,
                    cohort_id=(
                        request.authorization.opaque_cohort_id
                        if request.authorization is not None
                        else None
                    ),
                    cluster_assignments=cluster_assignments,
                )
            )
        if not prepared:
            raise TrainingDataError("No approved redacted hard-negative records are exportable")

        tenant_scope = self._tenant_scope(request, tenants)
        dataset_fingerprint = self._dataset_fingerprint(
            request,
            tenants,
            profiles,
            prepared,
            tenant_scope,
        )
        records = self._assign_splits(
            request,
            dataset_fingerprint,
            prepared,
        )
        building = TrainingDatasetVersion(
            dataset_id=request.dataset_id,
            version=request.dataset_version,
            tenant_scope=tenant_scope,
            source_tenant_ids=tenants,
            schema_version=request.schema_version,
            generation_rule_version=request.generation_rule_version,
            redaction_policy_version=request.redaction_policy_version,
            split_rule_version=self._split_policy.rule_version,
            split_salt_version=self._split_policy.salt_version,
            created_by=request.created_by,
            created_at=now,
            cross_tenant=cross_tenant,
            authorization_id=(
                request.authorization.authorization_id
                if request.authorization is not None
                else None
            ),
            status=TrainingDatasetStatus.BUILDING,
            record_count=len(records),
            fingerprint=dataset_fingerprint,
        )
        persisted = await self._training_data.create_dataset(building, records)
        if persisted.fingerprint != dataset_fingerprint:
            raise TrainingDataError("Dataset version already exists with different bindings")
        stored = await self._training_data.get_dataset(
            tenant_scope,
            request.dataset_id,
            request.dataset_version,
        )
        if stored is None:
            raise TrainingDataError("Frozen dataset could not be reloaded")
        persisted, stored_records = stored
        if persisted.status is TrainingDatasetStatus.EXPORTED:
            return TrainingDatasetExportResult(persisted, stored_records)
        try:
            artifact_references = await self._exporter.export(persisted, stored_records)
        except Exception as exc:
            failed = replace(
                persisted,
                status=TrainingDatasetStatus.FAILED,
                artifact_references=(),
                completed_at=now,
                failure_code="artifact_export_failed",
            )
            await self._training_data.finish_dataset(failed)
            raise TrainingDataError("Training dataset artifact export failed") from exc
        completed_at = self._clock()
        if completed_at.tzinfo is None:
            raise TrainingDataError("Dataset clock must return a timezone-aware datetime")
        exported = replace(
            persisted,
            status=TrainingDatasetStatus.EXPORTED,
            artifact_references=artifact_references,
            completed_at=completed_at,
            failure_code=None,
        )
        await self._training_data.finish_dataset(exported)
        return TrainingDatasetExportResult(exported, stored_records)

    def _validate_governance(
        self,
        request: TrainingDatasetExportRequest,
        tenants: tuple[str, ...],
        cross_tenant: bool,
        now: datetime,
    ) -> dict[tuple[str, str], str]:
        authorization = request.authorization
        if not cross_tenant:
            if authorization is not None:
                raise TrainingDataError("Single-tenant export cannot use cross-tenant approval")
            return {}
        if not self._cross_tenant_enabled:
            raise TrainingDataError("Cross-tenant training export is disabled")
        if authorization is None:
            raise TrainingDataError("Cross-tenant export requires explicit authorization")
        if tuple(sorted(authorization.tenant_ids)) != tenants:
            raise TrainingDataError("Cross-tenant authorization scope does not match request")
        if authorization.approved_at > now:
            raise TrainingDataError("Cross-tenant authorization is not active yet")
        if authorization.expires_at <= now:
            raise TrainingDataError("Cross-tenant authorization has expired")
        return {
            (item.tenant_id, item.template_fingerprint): item.cluster_id
            for item in authorization.template_clusters
        }

    async def _load_approved_candidates(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> tuple[HardNegativeCandidate, ...]:
        loaded: list[HardNegativeCandidate] = []
        cursor: str | None = None
        while len(loaded) < self._max_candidates:
            page = await self._training_data.list_candidates(
                tenant_id,
                schema_version,
                status=CandidateReviewStatus.APPROVED.value,
                limit=min(self._page_size, self._max_candidates - len(loaded)),
                after_candidate_id=cursor,
            )
            if not page:
                break
            loaded.extend(page)
            cursor = page[-1].candidate_id
            if len(page) < self._page_size:
                break
        if len(loaded) == self._max_candidates:
            overflow = await self._training_data.list_candidates(
                tenant_id,
                schema_version,
                status=CandidateReviewStatus.APPROVED.value,
                limit=1,
                after_candidate_id=loaded[-1].candidate_id,
            )
            if overflow:
                raise TrainingDataError("Approved hard-negative export limit exceeded")
        return tuple(loaded)

    async def _prepare_tenant_records(
        self,
        *,
        tenant_id: str,
        schema_version: str,
        candidates: Sequence[HardNegativeCandidate],
        cross_tenant: bool,
        cohort_id: str | None,
        cluster_assignments: Mapping[tuple[str, str], str],
    ) -> tuple[_PreparedRecord, ...]:
        if not candidates:
            return ()
        example_ids = tuple(
            sorted(
                {
                    example_id
                    for item in candidates
                    for example_id in (
                        item.positive_example_id,
                        item.negative_example_id,
                    )
                }
            )
        )
        examples = await self._examples.get_eligible_by_ids(
            tenant_id,
            schema_version,
            example_ids,
        )
        by_id = {item.example_id: item for item in examples}
        grouped: dict[str, list[tuple[HardNegativeCandidate, ReviewedExample]]] = defaultdict(list)
        for candidate in candidates:
            positive = by_id.get(candidate.positive_example_id)
            negative = by_id.get(candidate.negative_example_id)
            if positive is None or negative is None:
                continue
            if positive.label_type not in _POSITIVE_LABELS:
                continue
            grouped[positive.example_id].append((candidate, negative))

        prepared: list[_PreparedRecord] = []
        for positive_id in sorted(grouped):
            positive = by_id[positive_id]
            pairs = sorted(grouped[positive_id], key=lambda item: item[0].candidate_id)
            template_cluster = self._template_cluster(
                positive,
                cross_tenant,
                cohort_id,
                cluster_assignments,
            )
            tenant_scope = self._redactor.redact_training_identifier(
                tenant_id,
                cohort_id if cohort_id is not None else tenant_id,
                namespace="tenant-scope",
            )
            query = self._query_text(positive, cross_tenant)
            positive_text = self._value_text(
                positive,
                positive.reviewed_value,
                cross_tenant,
                section="POSITIVE_REVIEWED_VALUE",
            )
            if positive_text is None:
                continue
            negatives: list[str] = []
            candidate_ids: list[str] = []
            source_ids = {self._source_identifier(positive)}
            source_documents = {positive.document_id}
            group_keys = {
                f"document:{tenant_id}:{positive.document_id}",
                self._group_cluster_key(
                    positive,
                    cross_tenant,
                    cluster_assignments,
                ),
            }
            for candidate, negative in pairs:
                negative_value = (
                    negative.model_value
                    if negative.label_type is ExampleLabelType.CONFIRMED_INCORRECT
                    else negative.reviewed_value
                )
                negative_text = self._value_text(
                    negative,
                    negative_value,
                    cross_tenant,
                    section="HARD_NEGATIVE_REVIEWED_VALUE",
                )
                if (
                    negative_text is None
                    or negative_text == positive_text
                    or negative_text in negatives
                ):
                    continue
                negatives.append(negative_text)
                candidate_ids.append(candidate.candidate_id)
                source_ids.add(self._source_identifier(negative))
                if candidate.source_judgment_id is not None:
                    source_ids.add(candidate.source_judgment_id)
                source_documents.add(negative.document_id)
                group_keys.add(f"document:{tenant_id}:{negative.document_id}")
                group_keys.add(
                    self._group_cluster_key(
                        negative,
                        cross_tenant,
                        cluster_assignments,
                    )
                )
            if not negatives:
                continue
            opaque_sources = tuple(
                sorted(
                    self._redactor.redact_training_identifier(
                        tenant_id,
                        item,
                        namespace="source-event",
                    )
                    for item in source_ids
                )
            )
            record = TrainingRecord(
                query=query,
                positive=positive_text,
                hard_negatives=tuple(negatives),
                scope=TrainingRecordScope(
                    tenant_scope=tenant_scope,
                    document_type=positive.document_type,
                    field_path=positive.field_path,
                    schema_version=positive.schema_version,
                    vendor_fingerprint=positive.vendor_fingerprint,
                    template_cluster=template_cluster,
                ),
                source_event_ids=opaque_sources,
            )
            fingerprint = sha256(
                self._canonical(self._record_payload(record)).encode("utf-8")
            ).hexdigest()
            prepared.append(
                _PreparedRecord(
                    source_tenant_id=tenant_id,
                    positive_example_id=positive.example_id,
                    source_document_ids=tuple(sorted(source_documents)),
                    candidate_ids=tuple(candidate_ids),
                    group_keys=tuple(sorted(key for key in group_keys if key)),
                    record=record,
                    fingerprint=fingerprint,
                )
            )
        return tuple(prepared)

    def _query_text(self, example: ReviewedExample, force_irreversible: bool) -> str:
        candidates = tuple(
            redacted.value
            for value in example.evidence_reference.candidate_values
            if not (
                redacted := self._redactor.redact_training_value(
                    example.tenant_id,
                    example.field_path,
                    value,
                    force_irreversible=force_irreversible,
                )
            ).was_dropped
        )
        payload = {
            "document_type": example.document_type,
            "field_path": example.field_path,
            "schema_version": example.schema_version,
            "candidate_values": candidates,
            "readability": example.evidence_reference.readability,
            "validation_signals": example.evidence_reference.validation_signals,
            "ambiguous": example.evidence_reference.ambiguous,
            "vendor_fingerprint": example.vendor_fingerprint,
            "template_fingerprint": example.template_fingerprint,
        }
        return f"REVIEWED_FIELD_QUERY\n{self._canonical(payload)}"

    def _value_text(
        self,
        example: ReviewedExample,
        value: JsonValue,
        force_irreversible: bool,
        *,
        section: str,
    ) -> str | None:
        redacted = self._redactor.redact_training_value(
            example.tenant_id,
            example.field_path,
            value,
            force_irreversible=force_irreversible,
        )
        if redacted.was_dropped:
            return None
        return (
            f"{section}\n"
            f"{self._canonical({'field_path': example.field_path, 'value': redacted.value})}"
        )

    def _template_cluster(
        self,
        example: ReviewedExample,
        cross_tenant: bool,
        cohort_id: str | None,
        assignments: Mapping[tuple[str, str], str],
    ) -> str | None:
        if example.template_fingerprint is None:
            if cross_tenant:
                raise TrainingDataError(
                    "Cross-tenant export requires every example to have a template fingerprint"
                )
            return None
        if cross_tenant:
            cluster = assignments.get((example.tenant_id, example.template_fingerprint))
            if cluster is None or cohort_id is None:
                raise TrainingDataError("Cross-tenant template cluster assignment is incomplete")
            return f"sha256:{sha256(f'{cohort_id}\0{cluster}'.encode('utf-8')).hexdigest()}"
        return self._redactor.redact_training_identifier(
            example.tenant_id,
            example.template_fingerprint,
            namespace="template-cluster",
        )

    @staticmethod
    def _group_cluster_key(
        example: ReviewedExample,
        cross_tenant: bool,
        assignments: Mapping[tuple[str, str], str],
    ) -> str:
        if cross_tenant:
            if example.template_fingerprint is None:
                raise TrainingDataError("Cross-tenant template fingerprint is missing")
            cluster = assignments.get((example.tenant_id, example.template_fingerprint))
            if cluster is None:
                raise TrainingDataError("Cross-tenant template cluster assignment is incomplete")
            return f"cross-template:{cluster}"
        if example.vendor_fingerprint is not None:
            return f"vendor-template-family:{example.tenant_id}:{example.vendor_fingerprint}"
        if example.template_fingerprint is not None:
            return f"template:{example.tenant_id}:{example.template_fingerprint}"
        return f"document:{example.tenant_id}:{example.document_id}"

    @staticmethod
    def _source_identifier(example: ReviewedExample) -> str:
        return example.source_event_id or example.source_feedback_id

    def _tenant_scope(
        self,
        request: TrainingDatasetExportRequest,
        tenants: tuple[str, ...],
    ) -> str:
        if request.authorization is not None:
            value = f"cohort:{request.authorization.opaque_cohort_id}"
            return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"
        return self._redactor.redact_training_identifier(
            tenants[0],
            tenants[0],
            namespace="dataset-tenant-scope",
        )

    def _dataset_fingerprint(
        self,
        request: TrainingDatasetExportRequest,
        tenants: tuple[str, ...],
        profiles: Mapping[str, TrainingRedactionProfile],
        prepared: Sequence[_PreparedRecord],
        tenant_scope: str,
    ) -> str:
        policy_profiles = {
            tenant_id: {
                "strategy": profiles[tenant_id].strategy,
                "policy_version": profiles[tenant_id].policy_version,
            }
            for tenant_id in tenants
        }
        payload = {
            "dataset_id": request.dataset_id,
            "dataset_version": request.dataset_version,
            "tenant_scope": tenant_scope,
            "schema_version": request.schema_version,
            "generation_rule_version": request.generation_rule_version,
            "redaction_policy_version": request.redaction_policy_version,
            "redaction_profiles": policy_profiles,
            "split_rule_version": self._split_policy.rule_version,
            "split_salt_version": self._split_policy.salt_version,
            "candidate_fingerprints": sorted(item.fingerprint for item in prepared),
            "authorization_id": (
                request.authorization.authorization_id
                if request.authorization is not None
                else None
            ),
        }
        return sha256(self._canonical(payload).encode("utf-8")).hexdigest()

    def _assign_splits(
        self,
        request: TrainingDatasetExportRequest,
        dataset_fingerprint: str,
        prepared: Sequence[_PreparedRecord],
    ) -> tuple[TrainingDatasetRecord, ...]:
        parents = list(range(len(prepared)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        key_owner: dict[str, int] = {}
        for index, item in enumerate(prepared):
            for key in item.group_keys:
                owner = key_owner.setdefault(key, index)
                union(index, owner)
        component_keys: dict[int, set[str]] = defaultdict(set)
        for index, item in enumerate(prepared):
            component_keys[find(index)].update(item.group_keys)

        records: list[TrainingDatasetRecord] = []
        for index, item in enumerate(prepared):
            group_key_payload = tuple(sorted(component_keys[find(index)]))
            group_fingerprint = hmac.new(
                self._split_salt.encode("utf-8"),
                self._canonical(group_key_payload).encode("utf-8"),
                sha256,
            ).hexdigest()
            split = self._split_for_group(group_fingerprint)
            record_fingerprint = sha256(
                f"{dataset_fingerprint}\0{item.fingerprint}\0{split.value}".encode("utf-8")
            ).hexdigest()
            record_id = sha256(
                f"training-record\0{record_fingerprint}".encode("utf-8")
            ).hexdigest()
            records.append(
                TrainingDatasetRecord(
                    record_id=record_id,
                    dataset_id=request.dataset_id,
                    dataset_version=request.dataset_version,
                    source_tenant_id=item.source_tenant_id,
                    source_document_ids=item.source_document_ids,
                    candidate_ids=item.candidate_ids,
                    group_fingerprint=group_fingerprint,
                    split=split,
                    record=item.record,
                    fingerprint=record_fingerprint,
                )
            )
        return tuple(sorted(records, key=lambda item: item.record_id))

    def _split_for_group(self, group_fingerprint: str) -> TrainingSplit:
        digest = hmac.new(
            self._split_salt.encode("utf-8"),
            (
                f"{self._split_policy.salt_version}\0"
                f"{self._split_policy.rule_version}\0{group_fingerprint}"
            ).encode("utf-8"),
            sha256,
        ).digest()
        value = int.from_bytes(digest[:8], "big") / 2**64
        if value < self._split_policy.train_ratio:
            return TrainingSplit.TRAIN
        if value < self._split_policy.train_ratio + self._split_policy.validation_ratio:
            return TrainingSplit.VALIDATION
        return TrainingSplit.EVALUATION

    @staticmethod
    def _record_payload(record: TrainingRecord) -> dict[str, object]:
        return {
            "query": record.query,
            "positive": record.positive,
            "hard_negatives": list(record.hard_negatives),
            "scope": {
                "tenant_scope": record.scope.tenant_scope,
                "document_type": record.scope.document_type,
                "field_path": record.scope.field_path,
                "schema_version": record.scope.schema_version,
                "vendor_fingerprint": record.scope.vendor_fingerprint,
                "template_cluster": record.scope.template_cluster,
            },
            "source_event_ids": list(record.source_event_ids),
        }

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
