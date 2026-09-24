"""SQLAlchemy fact-source adapters for tenant-scoped offline evaluation."""

import asyncio
import json
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, cast

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationBucket,
    EvaluationBucketDimension,
    EvaluationBucketResult,
    EvaluationCase,
    EvaluationDataset,
    EvaluationReviewerJudgment,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationVariant,
    EvaluationVariantResult,
    ExpectedExample,
    ExpectedMemoryEffect,
    ExtractionMetric,
    FieldBindingGroundTruth,
    MemoryAdmissionGroundTruth,
    MemoryEffectJudgment,
    PromotionCandidate,
    RetrievalMetric,
    TrustedMemoryFieldMetric,
)
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    ExampleLabelType,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.domain.field_semantics import FieldBindingStatus
from invoice_intelligence.domain.workflow import JsonValue
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    EvaluationDatasetRow,
    EvaluationRunRow,
)


class SQLAlchemyEvaluationRepository:
    """Persist immutable datasets and monotonic run state in the business database."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def add(self, dataset: EvaluationDataset) -> None:
        await asyncio.to_thread(self._add_dataset_sync, dataset)

    async def get_dataset(
        self,
        tenant_id: str,
        dataset_id: str,
        dataset_version: str,
    ) -> EvaluationDataset | None:
        return await asyncio.to_thread(
            self._get_dataset_sync,
            tenant_id,
            dataset_id,
            dataset_version,
        )

    async def save(self, run: EvaluationRun) -> None:
        await asyncio.to_thread(self._save_run_sync, run)

    async def get_run(
        self,
        tenant_id: str,
        evaluation_run_id: str,
    ) -> EvaluationRun | None:
        return await asyncio.to_thread(
            self._get_run_sync,
            tenant_id,
            evaluation_run_id,
        )

    def _add_dataset_sync(self, dataset: EvaluationDataset) -> None:
        payload = _dataset_payload(dataset)
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(EvaluationDatasetRow).where(
                        EvaluationDatasetRow.tenant_id == dataset.tenant_id,
                        EvaluationDatasetRow.dataset_id == dataset.dataset_id,
                        EvaluationDatasetRow.dataset_version == dataset.version,
                    )
                )
                if existing is not None:
                    if _dataset_from_payload(existing.dataset_json) != dataset:
                        raise WorkflowPersistenceError(
                            "Evaluation dataset version is immutable"
                        )
                    return
                session.add(
                    EvaluationDatasetRow(
                        dataset_key=_dataset_key(
                            dataset.tenant_id,
                            dataset.dataset_id,
                            dataset.version,
                        ),
                        tenant_id=dataset.tenant_id,
                        dataset_id=dataset.dataset_id,
                        dataset_version=dataset.version,
                        schema_version=dataset.schema_version,
                        dataset_json=payload,
                        created_at=dataset.created_at,
                    )
                )
        except WorkflowPersistenceError:
            raise
        except (TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Evaluation dataset persistence failed"
            ) from exc

    def _get_dataset_sync(
        self,
        tenant_id: str,
        dataset_id: str,
        dataset_version: str,
    ) -> EvaluationDataset | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(EvaluationDatasetRow).where(
                        EvaluationDatasetRow.tenant_id == tenant_id,
                        EvaluationDatasetRow.dataset_id == dataset_id,
                        EvaluationDatasetRow.dataset_version == dataset_version,
                    )
                )
                return _dataset_from_payload(row.dataset_json) if row is not None else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Evaluation dataset read failed") from exc

    def _save_run_sync(self, run: EvaluationRun) -> None:
        payload = _run_payload(run)
        try:
            with self._sessions.begin() as session:
                dataset_key = _dataset_key(
                    run.tenant_id,
                    run.dataset_id,
                    run.dataset_version,
                )
                if session.get(EvaluationDatasetRow, dataset_key) is None:
                    raise WorkflowPersistenceError(
                        "Evaluation run requires a persisted dataset version"
                    )
                row = session.scalar(
                    select(EvaluationRunRow)
                    .where(
                        EvaluationRunRow.tenant_id == run.tenant_id,
                        EvaluationRunRow.evaluation_run_id == run.evaluation_run_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    session.add(
                        EvaluationRunRow(
                            evaluation_run_key=_run_key(
                                run.tenant_id,
                                run.evaluation_run_id,
                            ),
                            evaluation_run_id=run.evaluation_run_id,
                            tenant_id=run.tenant_id,
                            dataset_key=dataset_key,
                            dataset_id=run.dataset_id,
                            dataset_version=run.dataset_version,
                            schema_version=run.schema_version,
                            index_version=run.bindings.index_version.value,
                            model_version=run.bindings.model_version.value,
                            prompt_version=run.bindings.prompt_version.value,
                            retrieval_policy_version=(
                                run.bindings.retrieval_policy_version.value
                            ),
                            threshold_version=run.bindings.threshold_version,
                            status=run.status.value,
                            run_json=payload,
                            created_at=run.created_at,
                            started_at=run.started_at,
                            completed_at=run.completed_at,
                        )
                    )
                    return

                existing = _run_from_payload(row.run_json)
                self._validate_run_update(existing, run)
                row.status = run.status.value
                row.run_json = payload
                row.started_at = run.started_at
                row.completed_at = run.completed_at
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Evaluation run persistence failed") from exc

    def _get_run_sync(
        self,
        tenant_id: str,
        evaluation_run_id: str,
    ) -> EvaluationRun | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(EvaluationRunRow).where(
                        EvaluationRunRow.tenant_id == tenant_id,
                        EvaluationRunRow.evaluation_run_id == evaluation_run_id,
                    )
                )
                return _run_from_payload(row.run_json) if row is not None else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Evaluation run read failed") from exc

    @staticmethod
    def _validate_run_update(existing: EvaluationRun, updated: EvaluationRun) -> None:
        immutable_existing = (
            existing.evaluation_run_id,
            existing.tenant_id,
            existing.dataset_id,
            existing.dataset_version,
            existing.schema_version,
            existing.bindings,
            existing.suite,
            existing.variants,
            existing.created_at,
        )
        immutable_updated = (
            updated.evaluation_run_id,
            updated.tenant_id,
            updated.dataset_id,
            updated.dataset_version,
            updated.schema_version,
            updated.bindings,
            updated.suite,
            updated.variants,
            updated.created_at,
        )
        if immutable_existing != immutable_updated:
            raise WorkflowPersistenceError("Evaluation run bindings are immutable")
        transitions = {
            EvaluationRunStatus.CREATED: {
                EvaluationRunStatus.CREATED,
                EvaluationRunStatus.RUNNING,
                EvaluationRunStatus.FAILED,
            },
            EvaluationRunStatus.RUNNING: {
                EvaluationRunStatus.RUNNING,
                EvaluationRunStatus.COMPLETED,
                EvaluationRunStatus.FAILED,
            },
            EvaluationRunStatus.COMPLETED: {EvaluationRunStatus.COMPLETED},
            EvaluationRunStatus.FAILED: {EvaluationRunStatus.FAILED},
        }
        if updated.status not in transitions[existing.status]:
            raise WorkflowPersistenceError("Evaluation run status cannot move backwards")
        if existing.status is EvaluationRunStatus.COMPLETED:
            without_artifacts_existing = _run_payload_without_artifacts(existing)
            without_artifacts_updated = _run_payload_without_artifacts(updated)
            if without_artifacts_existing != without_artifacts_updated:
                raise WorkflowPersistenceError(
                    "Completed evaluation results are immutable"
                )
        if existing.status is EvaluationRunStatus.FAILED and existing != updated:
            raise WorkflowPersistenceError("Failed evaluation results are immutable")


def _dataset_payload(dataset: EvaluationDataset) -> dict[str, Any]:
    return {
        "dataset_id": dataset.dataset_id,
        "tenant_id": dataset.tenant_id,
        "name": dataset.name,
        "version": dataset.version,
        "schema_version": dataset.schema_version,
        "training_document_ids": list(dataset.training_document_ids),
        "training_template_fingerprints": list(
            dataset.training_template_fingerprints
        ),
        "cases": [_case_payload(item) for item in dataset.cases],
        "created_at": dataset.created_at.isoformat(),
        "is_frozen": dataset.is_frozen,
    }


def _case_payload(case: EvaluationCase) -> dict[str, Any]:
    evidence = case.evidence_reference
    return {
        "case_id": case.case_id,
        "tenant_id": case.tenant_id,
        "document_id": case.document_id,
        "document_type": case.document_type,
        "field_path": case.field_path,
        "schema_version": case.schema_version,
        "vendor_fingerprint": case.vendor_fingerprint,
        "template_fingerprint": case.template_fingerprint,
        "image_quality_bucket": case.image_quality_bucket,
        "evidence_reference": {
            "document_reference": evidence.document_reference,
            "image_reference": evidence.image_reference,
            "page_number": evidence.page_number,
            "evidence_source": evidence.evidence_source,
            "candidate_values": list(evidence.candidate_values),
            "readability": evidence.readability,
            "validation_signals": list(evidence.validation_signals),
            "ambiguous": evidence.ambiguous,
        },
        "expected_examples": [
            {
                "example_id": item.example_id,
                "source_document_id": item.source_document_id,
                "label_type": item.label_type.value,
                "relevance_grade": item.relevance_grade,
            }
            for item in case.expected_examples
        ],
        "expected_value": case.expected_value,
        "expected_is_missing": case.expected_is_missing,
        "expects_candidate": case.expects_candidate,
        "expected_review_required": case.expected_review_required,
        "reviewer_id": case.reviewer_id,
        "created_at": case.created_at.isoformat(),
        "memory_admission_ground_truth": (
            _memory_admission_ground_truth_payload(case.memory_admission_ground_truth)
            if case.memory_admission_ground_truth is not None
            else None
        ),
        "field_binding_ground_truth": (
            _field_binding_ground_truth_payload(case.field_binding_ground_truth)
            if case.field_binding_ground_truth is not None
            else None
        ),
        "expected_memory_effects": [
            {
                "example_id": item.example_id,
                "source_document_id": item.source_document_id,
                "judgment": item.judgment.value,
            }
            for item in case.expected_memory_effects
        ],
    }


def _dataset_from_payload(payload: dict[str, Any]) -> EvaluationDataset:
    return EvaluationDataset(
        dataset_id=str(payload["dataset_id"]),
        tenant_id=str(payload["tenant_id"]),
        name=str(payload["name"]),
        version=str(payload["version"]),
        schema_version=str(payload["schema_version"]),
        training_document_ids=tuple(
            str(item) for item in cast(list[object], payload["training_document_ids"])
        ),
        cases=tuple(
            _case_from_payload(cast(dict[str, Any], item))
            for item in cast(list[object], payload["cases"])
        ),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        is_frozen=_literal_true(payload["is_frozen"]),
        training_template_fingerprints=tuple(
            str(item)
            for item in cast(
                list[object],
                payload.get("training_template_fingerprints", []),
            )
        ),
    )


def _case_from_payload(payload: dict[str, Any]) -> EvaluationCase:
    evidence = cast(dict[str, Any], payload["evidence_reference"])
    return EvaluationCase(
        case_id=str(payload["case_id"]),
        tenant_id=str(payload["tenant_id"]),
        document_id=str(payload["document_id"]),
        document_type=str(payload["document_type"]),
        field_path=str(payload["field_path"]),
        schema_version=str(payload["schema_version"]),
        vendor_fingerprint=_optional_string(payload.get("vendor_fingerprint")),
        template_fingerprint=_optional_string(payload.get("template_fingerprint")),
        image_quality_bucket=str(payload["image_quality_bucket"]),
        evidence_reference=ExampleEvidenceReference(
            document_reference=str(evidence["document_reference"]),
            image_reference=_optional_string(evidence.get("image_reference")),
            page_number=_optional_int(evidence.get("page_number")),
            evidence_source=_optional_string(evidence.get("evidence_source")),
            candidate_values=tuple(
                str(item)
                for item in cast(list[object], evidence.get("candidate_values", []))
            ),
            readability=_optional_string(evidence.get("readability")),
            validation_signals=tuple(
                str(item)
                for item in cast(list[object], evidence.get("validation_signals", []))
            ),
            ambiguous=bool(evidence.get("ambiguous", False)),
        ),
        expected_examples=tuple(
            ExpectedExample(
                example_id=str(item["example_id"]),
                source_document_id=str(item["source_document_id"]),
                label_type=ExampleLabelType(str(item["label_type"])),
                relevance_grade=int(item["relevance_grade"]),
            )
            for item in (
                cast(dict[str, Any], value)
                for value in cast(list[object], payload["expected_examples"])
            )
        ),
        expected_value=cast(JsonValue, payload.get("expected_value")),
        expected_is_missing=_required_bool(payload["expected_is_missing"]),
        expects_candidate=_required_bool(payload["expects_candidate"]),
        expected_review_required=_required_bool(payload["expected_review_required"]),
        reviewer_id=str(payload["reviewer_id"]),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        memory_admission_ground_truth=_memory_admission_ground_truth_from_payload(
            cast(dict[str, Any], payload["memory_admission_ground_truth"])
        )
        if payload.get("memory_admission_ground_truth") is not None
        else None,
        field_binding_ground_truth=_field_binding_ground_truth_from_payload(
            cast(dict[str, Any], payload["field_binding_ground_truth"])
        )
        if payload.get("field_binding_ground_truth") is not None
        else None,
        expected_memory_effects=tuple(
            ExpectedMemoryEffect(
                example_id=str(item["example_id"]),
                source_document_id=str(item["source_document_id"]),
                judgment=MemoryEffectJudgment(str(item["judgment"])),
            )
            for item in (
                cast(dict[str, Any], value)
                for value in cast(
                    list[object],
                    payload.get("expected_memory_effects", []),
                )
            )
        ),
    )


def _memory_admission_ground_truth_payload(
    truth: MemoryAdmissionGroundTruth,
) -> dict[str, Any]:
    return {
        "example_id": truth.example_id,
        "expected_status": truth.expected_status.value,
        "harmful_if_admitted": truth.harmful_if_admitted,
        "reviewer_judgments": [
            {"reviewer_id": item.reviewer_id, "status": item.status.value}
            for item in truth.reviewer_judgments
        ],
        "adjudicator_id": truth.adjudicator_id,
        "adjudicated_at": truth.adjudicated_at.isoformat(),
    }


def _memory_admission_ground_truth_from_payload(
    payload: dict[str, Any],
) -> MemoryAdmissionGroundTruth:
    return MemoryAdmissionGroundTruth(
        example_id=str(payload["example_id"]),
        expected_status=MemoryAdmissionStatus(str(payload["expected_status"])),
        harmful_if_admitted=_required_bool(payload["harmful_if_admitted"]),
        reviewer_judgments=tuple(
            EvaluationReviewerJudgment(
                reviewer_id=str(item["reviewer_id"]),
                status=MemoryAdmissionStatus(str(item["status"])),
            )
            for item in (
                cast(dict[str, Any], value)
                for value in cast(list[object], payload["reviewer_judgments"])
            )
        ),
        adjudicator_id=str(payload["adjudicator_id"]),
        adjudicated_at=datetime.fromisoformat(str(payload["adjudicated_at"])),
    )


def _field_binding_ground_truth_payload(
    truth: FieldBindingGroundTruth,
) -> dict[str, Any]:
    return {
        "evidence_id": truth.evidence_id,
        "expected_status": truth.expected_status.value,
        "acceptable_field_paths": list(truth.acceptable_field_paths),
        "is_alias_case": truth.is_alias_case,
        "adjudicator_id": truth.adjudicator_id,
        "adjudicated_at": truth.adjudicated_at.isoformat(),
    }


def _field_binding_ground_truth_from_payload(
    payload: dict[str, Any],
) -> FieldBindingGroundTruth:
    return FieldBindingGroundTruth(
        evidence_id=str(payload["evidence_id"]),
        expected_status=FieldBindingStatus(str(payload["expected_status"])),
        acceptable_field_paths=tuple(
            str(item)
            for item in cast(list[object], payload["acceptable_field_paths"])
        ),
        is_alias_case=_required_bool(payload["is_alias_case"]),
        adjudicator_id=str(payload["adjudicator_id"]),
        adjudicated_at=datetime.fromisoformat(str(payload["adjudicated_at"])),
    )


def _run_payload(run: EvaluationRun) -> dict[str, Any]:
    return {
        "evaluation_run_id": run.evaluation_run_id,
        "tenant_id": run.tenant_id,
        "dataset_id": run.dataset_id,
        "dataset_version": run.dataset_version,
        "schema_version": run.schema_version,
        "bindings": {
            "index_version": run.bindings.index_version.value,
            "model_version": run.bindings.model_version.value,
            "prompt_version": run.bindings.prompt_version.value,
            "retrieval_policy_version": run.bindings.retrieval_policy_version.value,
            "threshold_version": run.bindings.threshold_version,
            "catalog_version": run.bindings.catalog_version,
            "admission_policy_version": run.bindings.admission_policy_version,
            "field_binding_policy_version": (
                run.bindings.field_binding_policy_version
            ),
        },
        "suite": run.suite.value,
        "variants": [item.value for item in run.variants],
        "status": run.status.value,
        "results": [_variant_result_payload(item) for item in run.results],
        "promotion_candidates": [
            {
                "candidate_id": item.candidate_id,
                "tenant_id": item.tenant_id,
                "evaluation_run_id": item.evaluation_run_id,
                "baseline_variant": item.baseline_variant.value,
                "candidate_variant": item.candidate_variant.value,
                "metric_deltas": item.metric_deltas,
                "rationale_codes": list(item.rationale_codes),
                "created_at": item.created_at.isoformat(),
            }
            for item in run.promotion_candidates
        ],
        "leakage_check_passed": run.leakage_check_passed,
        "report_schema_version": run.report_schema_version,
        "artifact_references": list(run.artifact_references),
        "created_at": run.created_at.isoformat(),
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "failure_code": run.failure_code,
    }


def _run_from_payload(payload: dict[str, Any]) -> EvaluationRun:
    bindings = cast(dict[str, Any], payload["bindings"])
    return EvaluationRun(
        evaluation_run_id=str(payload["evaluation_run_id"]),
        tenant_id=str(payload["tenant_id"]),
        dataset_id=str(payload["dataset_id"]),
        dataset_version=str(payload["dataset_version"]),
        schema_version=str(payload["schema_version"]),
        bindings=EvaluationBindings(
            index_version=IndexVersion(str(bindings["index_version"])),
            model_version=ModelVersion(str(bindings["model_version"])),
            prompt_version=PromptVersion(str(bindings["prompt_version"])),
            retrieval_policy_version=RetrievalPolicyVersion(
                str(bindings["retrieval_policy_version"])
            ),
            threshold_version=str(bindings["threshold_version"]),
            catalog_version=_optional_string(bindings.get("catalog_version")),
            admission_policy_version=_optional_string(
                bindings.get("admission_policy_version")
            ),
            field_binding_policy_version=_optional_string(
                bindings.get("field_binding_policy_version")
            ),
        ),
        variants=tuple(
            EvaluationVariant(str(item))
            for item in cast(list[object], payload["variants"])
        ),
        status=EvaluationRunStatus(str(payload["status"])),
        results=tuple(
            _variant_result_from_payload(cast(dict[str, Any], item))
            for item in cast(list[object], payload["results"])
        ),
        promotion_candidates=tuple(
            _promotion_from_payload(cast(dict[str, Any], item))
            for item in cast(list[object], payload["promotion_candidates"])
        ),
        leakage_check_passed=_required_bool(payload["leakage_check_passed"]),
        report_schema_version=str(payload["report_schema_version"]),
        artifact_references=tuple(
            str(item) for item in cast(list[object], payload["artifact_references"])
        ),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        started_at=_optional_datetime(payload.get("started_at")),
        completed_at=_optional_datetime(payload.get("completed_at")),
        failure_code=_optional_string(payload.get("failure_code")),
        suite=EvaluationSuite(str(payload.get("suite", EvaluationSuite.CASE_RAG.value))),
    )


def _variant_result_payload(result: EvaluationVariantResult) -> dict[str, Any]:
    return {
        "variant": result.variant.value,
        "overall": _bucket_result_payload(result.overall),
        "buckets": [_bucket_result_payload(item) for item in result.buckets],
    }


def _variant_result_from_payload(payload: dict[str, Any]) -> EvaluationVariantResult:
    return EvaluationVariantResult(
        variant=EvaluationVariant(str(payload["variant"])),
        overall=_bucket_result_from_payload(cast(dict[str, Any], payload["overall"])),
        buckets=tuple(
            _bucket_result_from_payload(cast(dict[str, Any], item))
            for item in cast(list[object], payload["buckets"])
        ),
    )


def _bucket_result_payload(result: EvaluationBucketResult) -> dict[str, Any]:
    return {
        "dimension": result.bucket.dimension.value,
        "value": result.bucket.value,
        "retrieval_metrics": [
            _retrieval_metric_payload(item) for item in result.retrieval_metrics
        ],
        "extraction_metric": _extraction_metric_payload(result.extraction_metric),
        "trusted_memory_field_metrics": [
            _trusted_memory_field_metric_payload(item)
            for item in result.trusted_memory_field_metrics
        ],
    }


def _bucket_result_from_payload(payload: dict[str, Any]) -> EvaluationBucketResult:
    return EvaluationBucketResult(
        bucket=EvaluationBucket(
            EvaluationBucketDimension(str(payload["dimension"])),
            str(payload["value"]),
        ),
        retrieval_metrics=tuple(
            _retrieval_metric_from_payload(cast(dict[str, Any], item))
            for item in cast(list[object], payload["retrieval_metrics"])
        ),
        extraction_metric=_extraction_metric_from_payload(
            cast(dict[str, Any], payload["extraction_metric"])
        ),
        trusted_memory_field_metrics=tuple(
            _trusted_memory_field_metric_from_payload(cast(dict[str, Any], item))
            for item in cast(
                list[object],
                payload.get("trusted_memory_field_metrics", []),
            )
        ),
    )


def _retrieval_metric_payload(metric: RetrievalMetric) -> dict[str, Any]:
    return {
        name: getattr(metric, name)
        for name in metric.__dataclass_fields__
    }


def _retrieval_metric_from_payload(payload: dict[str, Any]) -> RetrievalMetric:
    return RetrievalMetric(
        k=int(payload["k"]),
        recall_at_k=_optional_float(payload.get("recall_at_k")),
        hit_rate_at_k=_optional_float(payload.get("hit_rate_at_k")),
        mrr=_optional_float(payload.get("mrr")),
        ndcg_at_k=_optional_float(payload.get("ndcg_at_k")),
        positive_negative_separation=_optional_float(
            payload.get("positive_negative_separation")
        ),
        empty_retrieval_rate=float(payload["empty_retrieval_rate"]),
        evaluation_case_count=int(payload["evaluation_case_count"]),
        expected_example_case_count=int(payload["expected_example_case_count"]),
        expected_example_count=int(payload["expected_example_count"]),
        relevant_hit_count=int(payload["relevant_hit_count"]),
        empty_retrieval_count=int(payload["empty_retrieval_count"]),
        separation_case_count=int(payload["separation_case_count"]),
    )


def _extraction_metric_payload(metric: ExtractionMetric) -> dict[str, Any]:
    return {
        name: getattr(metric, name)
        for name in metric.__dataclass_fields__
    }


def _extraction_metric_from_payload(payload: dict[str, Any]) -> ExtractionMetric:
    return ExtractionMetric(
        field_accuracy=_optional_float(payload.get("field_accuracy")),
        missing_recognition_accuracy=float(payload["missing_recognition_accuracy"]),
        candidate_hit_rate=_optional_float(payload.get("candidate_hit_rate")),
        erroneous_auto_filled_value_count=int(
            payload["erroneous_auto_filled_value_count"]
        ),
        historical_override_violation_count=int(
            payload["historical_override_violation_count"]
        ),
        review_required_precision=_optional_float(
            payload.get("review_required_precision")
        ),
        review_required_recall=_optional_float(payload.get("review_required_recall")),
        evaluation_case_count=int(payload["evaluation_case_count"]),
        field_accuracy_denominator=int(payload["field_accuracy_denominator"]),
        missing_recognition_correct_count=int(
            payload["missing_recognition_correct_count"]
        ),
        candidate_case_count=int(payload["candidate_case_count"]),
        candidate_hit_count=int(payload["candidate_hit_count"]),
        review_true_positive_count=int(payload["review_true_positive_count"]),
        review_false_positive_count=int(payload["review_false_positive_count"]),
        review_false_negative_count=int(payload["review_false_negative_count"]),
    )


def _trusted_memory_field_metric_payload(
    metric: TrustedMemoryFieldMetric,
) -> dict[str, Any]:
    return {name: getattr(metric, name) for name in metric.__dataclass_fields__}


def _trusted_memory_field_metric_from_payload(
    payload: dict[str, Any],
) -> TrustedMemoryFieldMetric:
    return TrustedMemoryFieldMetric(
        k=int(payload["k"]),
        memory_approval_precision=_optional_float(
            payload.get("memory_approval_precision")
        ),
        harmful_memory_admission_rate=_optional_float(
            payload.get("harmful_memory_admission_rate")
        ),
        quarantine_rate=_optional_float(payload.get("quarantine_rate")),
        reviewer_disagreement_rate=_optional_float(
            payload.get("reviewer_disagreement_rate")
        ),
        alias_binding_accuracy=_optional_float(payload.get("alias_binding_accuracy")),
        top_k_field_recall=_optional_float(payload.get("top_k_field_recall")),
        field_binding_ambiguity_rate=_optional_float(
            payload.get("field_binding_ambiguity_rate")
        ),
        wrong_field_auto_fill_count=int(payload["wrong_field_auto_fill_count"]),
        memory_helpfulness_rate=_optional_float(
            payload.get("memory_helpfulness_rate")
        ),
        misleading_retrieval_rate=_optional_float(
            payload.get("misleading_retrieval_rate")
        ),
        evaluation_case_count=int(payload["evaluation_case_count"]),
        memory_admission_case_count=int(payload["memory_admission_case_count"]),
        approved_prediction_count=int(payload["approved_prediction_count"]),
        correct_approved_count=int(payload["correct_approved_count"]),
        harmful_ground_truth_count=int(payload["harmful_ground_truth_count"]),
        harmful_admitted_count=int(payload["harmful_admitted_count"]),
        quarantined_prediction_count=int(payload["quarantined_prediction_count"]),
        multi_reviewer_case_count=int(payload["multi_reviewer_case_count"]),
        reviewer_disagreement_count=int(payload["reviewer_disagreement_count"]),
        alias_binding_case_count=int(payload["alias_binding_case_count"]),
        correct_alias_binding_count=int(payload["correct_alias_binding_count"]),
        field_binding_case_count=int(payload["field_binding_case_count"]),
        expected_field_path_count=int(payload["expected_field_path_count"]),
        recalled_field_path_count=int(payload["recalled_field_path_count"]),
        ambiguous_prediction_count=int(payload["ambiguous_prediction_count"]),
        helpful_opportunity_count=int(payload["helpful_opportunity_count"]),
        helpful_hit_count=int(payload["helpful_hit_count"]),
        memory_effect_case_count=int(payload["memory_effect_case_count"]),
        misleading_hit_count=int(payload["misleading_hit_count"]),
    )


def _promotion_from_payload(payload: dict[str, Any]) -> PromotionCandidate:
    return PromotionCandidate(
        candidate_id=str(payload["candidate_id"]),
        tenant_id=str(payload["tenant_id"]),
        evaluation_run_id=str(payload["evaluation_run_id"]),
        baseline_variant=EvaluationVariant(str(payload["baseline_variant"])),
        candidate_variant=EvaluationVariant(str(payload["candidate_variant"])),
        metric_deltas={
            str(key): float(value)
            for key, value in cast(dict[object, object], payload["metric_deltas"]).items()
        },
        rationale_codes=tuple(
            str(item) for item in cast(list[object], payload["rationale_codes"])
        ),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
    )


def _run_payload_without_artifacts(run: EvaluationRun) -> str:
    payload = _run_payload(run)
    payload["artifact_references"] = []
    return _canonical(payload)


def _dataset_key(tenant_id: str, dataset_id: str, dataset_version: str) -> str:
    return sha256(f"{tenant_id}\0{dataset_id}\0{dataset_version}".encode("utf-8")).hexdigest()


def _run_key(tenant_id: str, evaluation_run_id: str) -> str:
    return sha256(f"{tenant_id}\0{evaluation_run_id}".encode("utf-8")).hexdigest()


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    return int(cast(Any, value)) if value is not None else None


def _optional_float(value: object) -> float | None:
    return float(cast(Any, value)) if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Persisted evaluation boolean has an invalid type")
    return value


def _literal_true(value: object) -> Literal[True]:
    if value is not True:
        raise ValueError("Persisted evaluation dataset is not frozen")
    return True
