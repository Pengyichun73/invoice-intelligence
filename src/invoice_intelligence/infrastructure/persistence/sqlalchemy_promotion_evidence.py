"""从 PostgreSQL 评估、产物和版本事实构造可信晋升证据。"""

import asyncio
import re

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError, ResourceNotFoundError
from invoice_intelligence.application.ports.promotion_evidence import PromotionEvidence
from invoice_intelligence.domain.evaluation import EvaluationRunStatus
from invoice_intelligence.domain.evaluation_jobs import EvaluationJobStatus
from invoice_intelligence.domain.training import ModelEvaluationStatus, ModelLifecycleStage
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import (
    _dataset_from_payload,
    _run_from_payload,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_model_training import (
    _artifact_from_payload,
    _evaluation_from_payload,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    EvaluationDatasetRow,
    EvaluationJobReportRow,
    EvaluationJobRow,
    EvaluationRunRow,
    IndexVersionRow,
    ModelArtifactRow,
    ModelEvaluationRow,
    ModelVersionRow,
    PromptVersionRow,
    TrainingDatasetVersionRow,
    TrainingRunRow,
)
from invoice_intelligence.infrastructure.reporting.postgres_evaluation import (
    report_artifacts_match,
)

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_METRIC = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class SQLAlchemyPromotionEvidenceRepository:
    def __init__(
        self,
        engine: Engine,
        *,
        active_schema_version: str,
        active_prompt_version: str,
        active_threshold_version: str,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
        self._active_schema = active_schema_version
        self._active_prompt = active_prompt_version
        self._active_threshold = active_threshold_version

    async def load(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        evaluation_run_id: str,
        artifact_id: str,
    ) -> PromotionEvidence:
        return await asyncio.to_thread(
            self._load_sync, tenant_id, candidate_id, evaluation_run_id, artifact_id
        )

    def _load_sync(
        self, tenant_id: str, candidate_id: str, evaluation_run_id: str, artifact_id: str
    ) -> PromotionEvidence:
        with self._sessions() as session:
            run_row = session.scalar(
                select(EvaluationRunRow).where(
                    EvaluationRunRow.tenant_id == tenant_id,
                    EvaluationRunRow.evaluation_run_id == evaluation_run_id,
                )
            )
            artifact_row = session.scalar(
                select(ModelArtifactRow).where(
                    ModelArtifactRow.tenant_id == tenant_id,
                    ModelArtifactRow.artifact_id == artifact_id,
                )
            )
            if run_row is None or artifact_row is None:
                raise ResourceNotFoundError("Promotion evidence was not found")
            try:
                run = _run_from_payload(run_row.run_json)
                artifact = _artifact_from_payload(artifact_row.artifact_json)
            except (KeyError, TypeError, ValueError) as exc:
                raise ResourceConflictError("Promotion evidence is malformed") from exc
            if (
                run.tenant_id != tenant_id
                or run.evaluation_run_id != evaluation_run_id
                or artifact.tenant_id != tenant_id
                or artifact.artifact_id != artifact_id
                or run_row.status != EvaluationRunStatus.COMPLETED.value
                or run.status is not EvaluationRunStatus.COMPLETED
                or not run.leakage_check_passed
            ):
                raise ResourceConflictError("Completed evaluation evidence is required")
            job = session.scalar(select(EvaluationJobRow).where(
                EvaluationJobRow.tenant_id == tenant_id,
                EvaluationJobRow.evaluation_run_id == evaluation_run_id,
                EvaluationJobRow.evidence_class == "suite_run",
                EvaluationJobRow.status == EvaluationJobStatus.COMPLETED.value,
            ))
            report = (
                session.scalar(select(EvaluationJobReportRow).where(
                    EvaluationJobReportRow.tenant_id == tenant_id,
                    EvaluationJobReportRow.job_id == job.job_id,
                )) if job is not None else None
            )
            if (
                job is None or report is None
                or job.dataset_key != run_row.dataset_key
                or job.dataset_id != run.dataset_id
                or job.dataset_version != run.dataset_version
                or job.schema_version != run.schema_version
                or job.suite != run.suite.value
                or job.index_version != run.bindings.index_version.value
                or job.model_version != run.bindings.model_version.value
                or job.prompt_version != run.bindings.prompt_version.value
                or job.retrieval_policy_version != run.bindings.retrieval_policy_version.value
                or job.threshold_version != run.bindings.threshold_version
                or job.catalog_version != run.bindings.catalog_version
                or job.admission_policy_version != run.bindings.admission_policy_version
                or job.field_binding_policy_version != run.bindings.field_binding_policy_version
                or report.metrics_json.get("evidence_class") != "suite_run"
                or report.metrics_json.get("evaluation_run_id") != evaluation_run_id
                or report.metrics_json.get("report_schema_version") != run.report_schema_version
                or report.metrics_json.get("variant_count") != len(run.results)
                or report.metrics_json.get("artifact_count") != len(run.artifact_references)
            ):
                raise ResourceConflictError("Completed Suite Job evidence is required")
            proposal = next(
                (item for item in run.promotion_candidates if item.candidate_id == candidate_id),
                None,
            )
            if proposal is None:
                raise ResourceConflictError("Evaluation did not propose this candidate")
            variant = next(
                (item for item in run.results if item.variant is proposal.candidate_variant),
                None,
            )
            if variant is None:
                raise ResourceConflictError("Evaluation variant evidence is missing")
            evaluation_rows = session.scalars(
                select(ModelEvaluationRow).where(
                    ModelEvaluationRow.tenant_id == tenant_id,
                    ModelEvaluationRow.artifact_id == artifact_id,
                    ModelEvaluationRow.evaluation_run_id == evaluation_run_id,
                    ModelEvaluationRow.stage == ModelLifecycleStage.OFFLINE_EVALUATION.value,
                )
            ).all()
            if len(evaluation_rows) != 1:
                raise ResourceConflictError("Exactly one artifact evaluation is required")
            evaluation_row = evaluation_rows[0]
            try:
                evaluation = _evaluation_from_payload(evaluation_row.evaluation_json)
            except (KeyError, TypeError, ValueError) as exc:
                raise ResourceConflictError("Model evaluation is malformed") from exc
            if (
                evaluation.tenant_id != tenant_id
                or evaluation.artifact_id != artifact_id
                or evaluation.evaluation_run_id != evaluation_run_id
                or evaluation.model_evaluation_id != evaluation_row.model_evaluation_id
                or evaluation.status.value != evaluation_row.status
                or evaluation.status not in {
                    ModelEvaluationStatus.PASSED,
                    ModelEvaluationStatus.FAILED,
                }
            ):
                raise ResourceConflictError("Terminal artifact evaluation is required")

            errors: list[str] = []
            dataset = session.get(EvaluationDatasetRow, run_row.dataset_key)
            try:
                frozen_dataset = (
                    _dataset_from_payload(dataset.dataset_json) if dataset is not None else None
                )
            except (KeyError, TypeError, ValueError):
                frozen_dataset = None
            if (
                dataset is None
                or frozen_dataset is None
                or dataset.tenant_id != tenant_id
                or dataset.dataset_id != run.dataset_id
                or dataset.dataset_version != run.dataset_version
                or dataset.schema_version != run.schema_version
                or frozen_dataset.tenant_id != tenant_id
                or frozen_dataset.dataset_id != run.dataset_id
                or frozen_dataset.version != run.dataset_version
                or frozen_dataset.schema_version != run.schema_version
                or not frozen_dataset.is_frozen
            ):
                errors.append("DATASET_VERSION_INVALID")
            if (
                run.report_schema_version != "invoice-offline-evaluation-v2"
                or not report_artifacts_match(session, run_row, run)
            ):
                errors.append("EVALUATION_REPORT_INVALID")
            model_version = session.get(ModelVersionRow, artifact_row.model_version_id)
            if (
                model_version is None
                or model_version.tenant_id != tenant_id
                or model_version.version != artifact.model_version
                or artifact.model_version_id != artifact_row.model_version_id
                or evaluation.candidate_model_version_id != artifact_row.model_version_id
                or run.bindings.model_version.value != artifact.model_version
            ):
                errors.append("MODEL_VERSION_MISMATCH")
            training_run = session.get(TrainingRunRow, artifact_row.training_run_key)
            training_dataset = (
                session.get(TrainingDatasetVersionRow, training_run.training_dataset_key)
                if training_run is not None
                else None
            )
            if (
                training_run is None
                or training_run.tenant_id != tenant_id
                or training_run.status != "succeeded"
                or training_run.candidate_model_version_id != artifact_row.model_version_id
                or training_run.schema_version != run.schema_version
                or artifact.training_run_id != training_run.training_run_id
                or training_dataset is None
                or training_dataset.tenant_scope != tenant_id
                or training_dataset.status != "exported"
                or training_dataset.schema_version != run.schema_version
            ):
                errors.append("TRAINING_ARTIFACT_INVALID")
            if (
                not artifact_row.is_valid
                or not artifact.is_valid
                or artifact_row.stage == "rejected"
                or artifact_row.invalidated_at is not None
            ):
                errors.append("MODEL_ARTIFACT_INVALID")
            prompt = session.scalar(
                select(PromptVersionRow).where(
                    PromptVersionRow.tenant_id == tenant_id,
                    PromptVersionRow.version == run.bindings.prompt_version.value,
                )
            )
            index = session.scalar(
                select(IndexVersionRow).where(
                    IndexVersionRow.tenant_id == tenant_id,
                    IndexVersionRow.version == run.bindings.index_version.value,
                )
            )
            if (
                prompt is None
                or run.bindings.prompt_version.value != self._active_prompt
                or evaluation.prompt_version != run.bindings.prompt_version.value
                or (index is not None and index.prompt_version_id != prompt.prompt_version_id)
            ):
                errors.append("PROMPT_VERSION_MISMATCH")
            if (
                index is None
                or not index.is_valid
                or index.activated_at is None
                or index.schema_version != run.schema_version
            ):
                errors.append("INDEX_VERSION_INVALID")
            if (
                run.schema_version != self._active_schema
                or evaluation.schema_version != run.schema_version
            ):
                errors.append("SCHEMA_VERSION_MISMATCH")
            if (
                run.bindings.threshold_version != self._active_threshold
                or evaluation.threshold_version != run.bindings.threshold_version
            ):
                errors.append("THRESHOLD_VERSION_MISMATCH")
            if (
                evaluation.dataset_id != run.dataset_id
                or evaluation.dataset_version != run.dataset_version
            ):
                errors.append("EVALUATION_DATASET_MISMATCH")

            extraction = variant.overall.extraction_metric
            hard_failure_code = None
            if extraction.historical_override_violation_count > 0:
                hard_failure_code = "HISTORICAL_OVERRIDE_VIOLATION"
            elif extraction.erroneous_auto_filled_value_count > 0:
                hard_failure_code = "ERRONEOUS_AUTO_FILL"
            elif evaluation.status is ModelEvaluationStatus.FAILED:
                code = evaluation.failure_code or "MODEL_EVALUATION_FAILED"
                hard_failure_code = (
                    code if _SAFE_CODE.fullmatch(code) else "MODEL_EVALUATION_FAILED"
                )
            if any(
                not _SAFE_METRIC.fullmatch(item.metric_name)
                for item in evaluation.comparisons
            ):
                raise ResourceConflictError("Model evaluation metric contract is invalid")
            metrics = {
                item.metric_name: item.candidate_value for item in evaluation.comparisons
            }
            return PromotionEvidence(
                candidate_id=candidate_id,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                model_evaluation_id=evaluation.model_evaluation_id,
                evaluation_run_id=evaluation_run_id,
                dataset_version=run.dataset_version,
                model_version=artifact.model_version,
                prompt_version=run.bindings.prompt_version.value,
                schema_version=run.schema_version,
                index_version=run.bindings.index_version.value,
                threshold_version=run.bindings.threshold_version,
                metric_values=metrics,
                hard_failure_code=hard_failure_code,
                compatibility_errors=tuple(sorted(set(errors))),
            )
