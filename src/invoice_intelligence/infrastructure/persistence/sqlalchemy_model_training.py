"""SQLAlchemy fact source for offline training and governed model lifecycle state."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.domain.training import (
    DeploymentStatus,
    MetricDirection,
    ModelArtifact,
    ModelDeployment,
    ModelEvaluation,
    ModelEvaluationStatus,
    ModelLifecycleStage,
    ModelMetricComparison,
    PromotionDecision,
    PromotionDecisionType,
    TrainingHyperparameter,
    TrainingRun,
    TrainingRunStatus,
    TrainingTargetType,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    EvaluationDatasetRow,
    ModelArtifactRow,
    ModelDeploymentRow,
    ModelEvaluationRow,
    ModelVersionRow,
    PromotionDecisionRow,
    PromptVersionRow,
    TrainingDatasetVersionRow,
    TrainingRunRow,
)


class SQLAlchemyModelTrainingRepository:
    """Persist training, registry, promotion, and rollback facts transactionally."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def save_training_run(self, run: TrainingRun) -> None:
        await asyncio.to_thread(self._save_training_run_sync, run)

    async def get_training_run(
        self,
        tenant_id: str,
        training_run_id: str,
    ) -> TrainingRun | None:
        return await asyncio.to_thread(
            self._get_training_run_sync,
            tenant_id,
            training_run_id,
        )

    async def register_artifact(self, artifact: ModelArtifact) -> None:
        await asyncio.to_thread(self._register_artifact_sync, artifact)

    async def get_artifact(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> ModelArtifact | None:
        return await asyncio.to_thread(self._get_artifact_sync, tenant_id, artifact_id)

    async def save_evaluation(self, evaluation: ModelEvaluation) -> None:
        await asyncio.to_thread(self._save_evaluation_sync, evaluation)

    async def list_evaluations(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> tuple[ModelEvaluation, ...]:
        return await asyncio.to_thread(
            self._list_evaluations_sync,
            tenant_id,
            artifact_id,
        )

    async def get_deployment(
        self,
        tenant_id: str,
        deployment_id: str,
    ) -> ModelDeployment | None:
        return await asyncio.to_thread(
            self._get_deployment_sync,
            tenant_id,
            deployment_id,
        )

    async def get_active_production(
        self,
        tenant_id: str,
        target_type: TrainingTargetType,
    ) -> ModelDeployment | None:
        return await asyncio.to_thread(
            self._get_active_production_sync,
            tenant_id,
            target_type,
        )

    async def save_promotion_decision(self, decision: PromotionDecision) -> None:
        await asyncio.to_thread(self._save_promotion_decision_sync, decision)

    async def get_promotion_decision(
        self,
        tenant_id: str,
        decision_id: str,
    ) -> PromotionDecision | None:
        return await asyncio.to_thread(
            self._get_promotion_decision_sync,
            tenant_id,
            decision_id,
        )

    async def commit_promotion(
        self,
        artifact: ModelArtifact,
        decision: PromotionDecision,
        deployment: ModelDeployment | None,
    ) -> None:
        await asyncio.to_thread(
            self._commit_promotion_sync,
            artifact,
            decision,
            deployment,
        )

    async def commit_rollback(
        self,
        rolled_back_artifact: ModelArtifact,
        restored_artifact: ModelArtifact,
        decision: PromotionDecision,
        replacement_deployment: ModelDeployment,
    ) -> None:
        await asyncio.to_thread(
            self._commit_rollback_sync,
            rolled_back_artifact,
            restored_artifact,
            decision,
            replacement_deployment,
        )

    def _save_training_run_sync(self, run: TrainingRun) -> None:
        payload = _training_run_payload(run)
        try:
            with self._sessions.begin() as session:
                dataset_key = _dataset_key(
                    run.training_dataset_tenant_scope,
                    run.training_dataset_id,
                    run.training_dataset_version,
                )
                dataset_row = session.get(TrainingDatasetVersionRow, dataset_key)
                if dataset_row is None:
                    raise WorkflowPersistenceError(
                        "Training run requires a persisted dataset version"
                    )
                if (
                    dataset_row.status != "exported"
                    or dataset_row.schema_version != run.schema_version
                    or run.tenant_id not in dataset_row.source_tenant_ids_json
                ):
                    raise WorkflowPersistenceError(
                        "Training run dataset is not exported for this tenant and Schema"
                    )
                row = session.scalar(
                    select(TrainingRunRow)
                    .where(
                        TrainingRunRow.tenant_id == run.tenant_id,
                        TrainingRunRow.training_run_id == run.training_run_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    session.add(
                        TrainingRunRow(
                            training_run_key=_tenant_key(
                                run.tenant_id,
                                run.training_run_id,
                            ),
                            training_run_id=run.training_run_id,
                            tenant_id=run.tenant_id,
                            training_dataset_key=dataset_key,
                            target_type=run.target_type.value,
                            provider=run.provider,
                            candidate_model_version_id=run.candidate_model_version_id,
                            candidate_model_version=run.candidate_model_version,
                            schema_version=run.schema_version,
                            status=run.status.value,
                            remote_job_id=run.remote_job_id,
                            run_json=payload,
                            created_at=run.created_at,
                            submitted_at=run.submitted_at,
                            started_at=run.started_at,
                            completed_at=run.completed_at,
                        )
                    )
                    return
                existing = _training_run_from_payload(row.run_json)
                _validate_training_run_update(existing, run)
                row.status = run.status.value
                row.remote_job_id = run.remote_job_id
                row.run_json = payload
                row.submitted_at = run.submitted_at
                row.started_at = run.started_at
                row.completed_at = run.completed_at
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Training run persistence failed") from exc

    def _get_training_run_sync(
        self,
        tenant_id: str,
        training_run_id: str,
    ) -> TrainingRun | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(TrainingRunRow).where(
                        TrainingRunRow.tenant_id == tenant_id,
                        TrainingRunRow.training_run_id == training_run_id,
                    )
                )
                return _training_run_from_payload(row.run_json) if row else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Training run read failed") from exc

    def _register_artifact_sync(self, artifact: ModelArtifact) -> None:
        payload = _artifact_payload(artifact)
        try:
            with self._sessions.begin() as session:
                run_key = _tenant_key(artifact.tenant_id, artifact.training_run_id)
                run_row = session.get(TrainingRunRow, run_key)
                if run_row is None or run_row.status != TrainingRunStatus.SUCCEEDED.value:
                    raise WorkflowPersistenceError(
                        "Model artifact requires a successful training run"
                    )
                run = _training_run_from_payload(run_row.run_json)
                if (
                    run.candidate_model_version_id != artifact.model_version_id
                    or run.candidate_model_version != artifact.model_version
                    or run.provider_artifact_reference
                    != artifact.provider_artifact_reference
                    or run.target_type is not artifact.target_type
                    or run.provider != artifact.provider
                    or run.base_model != artifact.base_model
                    or run.base_model_version != artifact.base_model_version
                    or run.training_dataset_id != artifact.training_dataset_id
                    or run.training_dataset_version != artifact.training_dataset_version
                    or run.code_version != artifact.code_version
                ):
                    raise WorkflowPersistenceError(
                        "Model artifact does not match its successful training run"
                    )
                model_version = session.get(ModelVersionRow, artifact.model_version_id)
                if model_version is None:
                    by_version = session.scalar(
                        select(ModelVersionRow).where(
                            ModelVersionRow.tenant_id == artifact.tenant_id,
                            ModelVersionRow.version == artifact.model_version,
                        )
                    )
                    if by_version is not None:
                        raise WorkflowPersistenceError(
                            "Model version already has a different identifier"
                        )
                    session.add(
                        ModelVersionRow(
                            model_version_id=artifact.model_version_id,
                            tenant_id=artifact.tenant_id,
                            version=artifact.model_version,
                            created_at=artifact.created_at,
                        )
                    )
                elif (
                    model_version.tenant_id != artifact.tenant_id
                    or model_version.version != artifact.model_version
                ):
                    raise WorkflowPersistenceError("Model version identity collision")

                existing = session.get(ModelArtifactRow, artifact.artifact_id)
                if existing is not None:
                    if _canonical(existing.artifact_json) != _canonical(payload):
                        raise WorkflowPersistenceError("Model artifact is immutable")
                    return
                session.add(
                    ModelArtifactRow(
                        artifact_id=artifact.artifact_id,
                        tenant_id=artifact.tenant_id,
                        training_run_key=run_key,
                        model_version_id=artifact.model_version_id,
                        target_type=artifact.target_type.value,
                        provider=artifact.provider,
                        stage=artifact.stage.value,
                        is_valid=artifact.is_valid,
                        artifact_json=payload,
                        created_at=artifact.created_at,
                        invalidated_at=artifact.invalidated_at,
                    )
                )
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model artifact registration failed") from exc

    def _get_artifact_sync(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> ModelArtifact | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ModelArtifactRow).where(
                        ModelArtifactRow.tenant_id == tenant_id,
                        ModelArtifactRow.artifact_id == artifact_id,
                    )
                )
                return _artifact_from_payload(row.artifact_json) if row else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model artifact read failed") from exc

    def _save_evaluation_sync(self, evaluation: ModelEvaluation) -> None:
        payload = _evaluation_payload(evaluation)
        try:
            with self._sessions.begin() as session:
                artifact_row = session.get(ModelArtifactRow, evaluation.artifact_id)
                if (
                    artifact_row is None
                    or artifact_row.tenant_id != evaluation.tenant_id
                    or artifact_row.model_version_id
                    != evaluation.candidate_model_version_id
                ):
                    raise WorkflowPersistenceError(
                        "Model evaluation references an invalid artifact scope"
                    )
                training_run = session.get(
                    TrainingRunRow,
                    artifact_row.training_run_key,
                )
                if training_run is None or training_run.schema_version != evaluation.schema_version:
                    raise WorkflowPersistenceError(
                        "Model evaluation Schema does not match its training run"
                    )
                baseline = session.get(
                    ModelVersionRow,
                    evaluation.baseline_model_version_id,
                )
                if baseline is None or baseline.tenant_id != evaluation.tenant_id:
                    raise WorkflowPersistenceError(
                        "Model evaluation baseline version is not registered"
                    )
                prompt = session.scalar(
                    select(PromptVersionRow).where(
                        PromptVersionRow.tenant_id == evaluation.tenant_id,
                        PromptVersionRow.version == evaluation.prompt_version,
                    )
                )
                if prompt is None:
                    raise WorkflowPersistenceError(
                        "Model evaluation Prompt version is not registered"
                    )
                dataset = session.scalar(
                    select(EvaluationDatasetRow).where(
                        EvaluationDatasetRow.tenant_id == evaluation.tenant_id,
                        EvaluationDatasetRow.dataset_id == evaluation.dataset_id,
                        EvaluationDatasetRow.dataset_version
                        == evaluation.dataset_version,
                    )
                )
                if dataset is None or dataset.schema_version != evaluation.schema_version:
                    raise WorkflowPersistenceError(
                        "Model evaluation dataset is not registered for this Schema"
                    )
                key = _tenant_key(
                    evaluation.tenant_id,
                    evaluation.model_evaluation_id,
                )
                row = session.get(ModelEvaluationRow, key)
                if row is None:
                    session.add(
                        ModelEvaluationRow(
                            model_evaluation_key=key,
                            model_evaluation_id=evaluation.model_evaluation_id,
                            tenant_id=evaluation.tenant_id,
                            artifact_id=evaluation.artifact_id,
                            stage=evaluation.stage.value,
                            evaluation_run_id=evaluation.evaluation_run_id,
                            dataset_id=evaluation.dataset_id,
                            dataset_version=evaluation.dataset_version,
                            schema_version=evaluation.schema_version,
                            baseline_model_version_id=(
                                evaluation.baseline_model_version_id
                            ),
                            candidate_model_version_id=(
                                evaluation.candidate_model_version_id
                            ),
                            prompt_version=evaluation.prompt_version,
                            threshold_version=evaluation.threshold_version,
                            status=evaluation.status.value,
                            evaluation_json=payload,
                            created_at=evaluation.created_at,
                            started_at=evaluation.started_at,
                            completed_at=evaluation.completed_at,
                        )
                    )
                    return
                existing = _evaluation_from_payload(row.evaluation_json)
                _validate_evaluation_update(existing, evaluation)
                row.status = evaluation.status.value
                row.evaluation_json = payload
                row.started_at = evaluation.started_at
                row.completed_at = evaluation.completed_at
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model evaluation persistence failed") from exc

    def _list_evaluations_sync(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> tuple[ModelEvaluation, ...]:
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(ModelEvaluationRow)
                    .where(
                        ModelEvaluationRow.tenant_id == tenant_id,
                        ModelEvaluationRow.artifact_id == artifact_id,
                    )
                    .order_by(
                        ModelEvaluationRow.created_at,
                        ModelEvaluationRow.model_evaluation_id,
                    )
                ).all()
                return tuple(
                    _evaluation_from_payload(row.evaluation_json) for row in rows
                )
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model evaluation read failed") from exc

    def _get_deployment_sync(
        self,
        tenant_id: str,
        deployment_id: str,
    ) -> ModelDeployment | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ModelDeploymentRow).where(
                        ModelDeploymentRow.tenant_id == tenant_id,
                        ModelDeploymentRow.deployment_id == deployment_id,
                    )
                )
                return _deployment_from_payload(row.deployment_json) if row else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model deployment read failed") from exc

    def _get_active_production_sync(
        self,
        tenant_id: str,
        target_type: TrainingTargetType,
    ) -> ModelDeployment | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ModelDeploymentRow).where(
                        ModelDeploymentRow.tenant_id == tenant_id,
                        ModelDeploymentRow.target_type == target_type.value,
                        ModelDeploymentRow.stage == ModelLifecycleStage.PRODUCTION.value,
                        ModelDeploymentRow.status == DeploymentStatus.ACTIVE.value,
                    )
                )
                return _deployment_from_payload(row.deployment_json) if row else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Active production read failed") from exc

    def _save_promotion_decision_sync(self, decision: PromotionDecision) -> None:
        if decision.decision is not PromotionDecisionType.REJECTED:
            raise WorkflowPersistenceError(
                "Standalone promotion decisions must be rejected outcomes"
            )
        try:
            with self._sessions.begin() as session:
                self._save_decision(session, decision)
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Promotion decision persistence failed") from exc

    def _get_promotion_decision_sync(
        self,
        tenant_id: str,
        decision_id: str,
    ) -> PromotionDecision | None:
        try:
            with self._sessions() as session:
                row = session.get(
                    PromotionDecisionRow,
                    _tenant_key(tenant_id, decision_id),
                )
                if row is None or row.tenant_id != tenant_id:
                    return None
                return _decision_from_payload(row.decision_json)
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Promotion decision read failed") from exc

    def _commit_promotion_sync(
        self,
        artifact: ModelArtifact,
        decision: PromotionDecision,
        deployment: ModelDeployment | None,
    ) -> None:
        if decision.decision is not PromotionDecisionType.APPROVED:
            raise WorkflowPersistenceError("Only approved decisions may commit promotion")
        expected = _NEXT_ARTIFACT_STAGE.get(decision.from_stage)
        if expected is not decision.to_stage or artifact.stage is not decision.to_stage:
            raise WorkflowPersistenceError("Promotion decision skips a lifecycle stage")
        requires_deployment = decision.to_stage in {
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
            ModelLifecycleStage.PRODUCTION,
        }
        if requires_deployment != (deployment is not None):
            raise WorkflowPersistenceError("Promotion deployment boundary is inconsistent")
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(ModelArtifactRow)
                    .where(ModelArtifactRow.artifact_id == artifact.artifact_id)
                    .with_for_update()
                )
                if row is None or row.tenant_id != artifact.tenant_id:
                    raise WorkflowPersistenceError("Promotion artifact was not found")
                existing = _artifact_from_payload(row.artifact_json)
                if existing.stage is artifact.stage:
                    self._validate_promotion_replay(session, artifact, decision, deployment)
                    return
                if (
                    existing.stage is not decision.from_stage
                    or artifact.stage is not decision.to_stage
                ):
                    raise WorkflowPersistenceError("Promotion artifact stage is inconsistent")
                self._save_decision(session, decision)
                if deployment is not None:
                    if deployment.stage is ModelLifecycleStage.PRODUCTION:
                        self._supersede_previous_production(session, deployment)
                    self._insert_deployment(session, deployment)
                row.stage = artifact.stage.value
                row.artifact_json = _artifact_payload(artifact)
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model promotion commit failed") from exc

    def _commit_rollback_sync(
        self,
        rolled_back_artifact: ModelArtifact,
        restored_artifact: ModelArtifact,
        decision: PromotionDecision,
        replacement_deployment: ModelDeployment,
    ) -> None:
        if decision.decision is not PromotionDecisionType.ROLLBACK_REQUIRED:
            raise WorkflowPersistenceError("Rollback commit requires a rollback decision")
        try:
            with self._sessions.begin() as session:
                decision_key = _tenant_key(decision.tenant_id, decision.decision_id)
                existing_decision = session.get(PromotionDecisionRow, decision_key)
                if existing_decision is not None:
                    if _canonical(existing_decision.decision_json) != _canonical(
                        _decision_payload(decision)
                    ):
                        raise WorkflowPersistenceError("Rollback decision replay changed")
                    deployment_row = session.get(
                        ModelDeploymentRow,
                        _tenant_key(
                            replacement_deployment.tenant_id,
                            replacement_deployment.deployment_id,
                        ),
                    )
                    if deployment_row is None or _canonical(
                        deployment_row.deployment_json
                    ) != _canonical(_deployment_payload(replacement_deployment)):
                        raise WorkflowPersistenceError(
                            "Rollback replay changed replacement deployment"
                        )
                    return
                current = session.scalar(
                    select(ModelDeploymentRow)
                    .where(
                        ModelDeploymentRow.tenant_id == decision.tenant_id,
                        ModelDeploymentRow.artifact_id == rolled_back_artifact.artifact_id,
                        ModelDeploymentRow.stage
                        == ModelLifecycleStage.PRODUCTION.value,
                        ModelDeploymentRow.status == DeploymentStatus.ACTIVE.value,
                    )
                    .with_for_update()
                )
                if current is None:
                    raise WorkflowPersistenceError(
                        "Rollback requires an active production deployment"
                    )
                current_domain = _deployment_from_payload(current.deployment_json)
                if current_domain.previous_deployment_id is None:
                    raise WorkflowPersistenceError("Rollback has no previous deployment")
                target = session.scalar(
                    select(ModelDeploymentRow).where(
                        ModelDeploymentRow.tenant_id == decision.tenant_id,
                        ModelDeploymentRow.deployment_id
                        == current_domain.previous_deployment_id,
                    )
                )
                if (
                    target is None
                    or target.artifact_id != restored_artifact.artifact_id
                    or replacement_deployment.artifact_id != restored_artifact.artifact_id
                    or replacement_deployment.target_type
                    is not current_domain.target_type
                ):
                    raise WorkflowPersistenceError("Rollback target artifact is inconsistent")
                now = replacement_deployment.activated_at or replacement_deployment.created_at
                rolled_back_deployment = replace(
                    current_domain,
                    status=DeploymentStatus.ROLLED_BACK,
                    completed_at=now,
                )
                current.status = DeploymentStatus.ROLLED_BACK.value
                current.deployment_json = _deployment_payload(rolled_back_deployment)
                current.completed_at = now
                self._insert_deployment(session, replacement_deployment)
                self._update_artifact(session, rolled_back_artifact)
                self._update_artifact(session, restored_artifact)
                self._save_decision(session, decision)
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Model rollback commit failed") from exc

    @staticmethod
    def _save_decision(session: Session, decision: PromotionDecision) -> None:
        artifact = session.get(ModelArtifactRow, decision.artifact_id)
        if artifact is None or artifact.tenant_id != decision.tenant_id:
            raise WorkflowPersistenceError("Promotion decision artifact was not found")
        if decision.evaluation_ids:
            evaluations = session.scalars(
                select(ModelEvaluationRow).where(
                    ModelEvaluationRow.tenant_id == decision.tenant_id,
                    ModelEvaluationRow.artifact_id == decision.artifact_id,
                    ModelEvaluationRow.model_evaluation_id.in_(decision.evaluation_ids),
                )
            ).all()
            if {item.model_evaluation_id for item in evaluations} != set(
                decision.evaluation_ids
            ):
                raise WorkflowPersistenceError(
                    "Promotion decision references evaluation evidence from another scope"
                )
        key = _tenant_key(decision.tenant_id, decision.decision_id)
        payload = _decision_payload(decision)
        existing = session.get(PromotionDecisionRow, key)
        if existing is not None:
            if _canonical(existing.decision_json) != _canonical(payload):
                raise WorkflowPersistenceError("Promotion decision is immutable")
            return
        session.add(
            PromotionDecisionRow(
                decision_key=key,
                decision_id=decision.decision_id,
                tenant_id=decision.tenant_id,
                artifact_id=decision.artifact_id,
                from_stage=decision.from_stage.value,
                to_stage=decision.to_stage.value,
                decision=decision.decision.value,
                reviewer_id=decision.reviewer_id,
                decision_json=payload,
                decided_at=decision.decided_at,
            )
        )

    @staticmethod
    def _insert_deployment(session: Session, deployment: ModelDeployment) -> None:
        if deployment.status is not DeploymentStatus.ACTIVE:
            raise WorkflowPersistenceError("Committed deployment must be active")
        key = _tenant_key(deployment.tenant_id, deployment.deployment_id)
        payload = _deployment_payload(deployment)
        existing = session.get(ModelDeploymentRow, key)
        if existing is not None:
            if _canonical(existing.deployment_json) != _canonical(payload):
                raise WorkflowPersistenceError("Model deployment is immutable")
            return
        artifact = session.get(ModelArtifactRow, deployment.artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != deployment.tenant_id
            or artifact.model_version_id != deployment.model_version_id
            or artifact.target_type != deployment.target_type.value
        ):
            raise WorkflowPersistenceError("Deployment references an invalid artifact")
        session.add(
            ModelDeploymentRow(
                deployment_key=key,
                deployment_id=deployment.deployment_id,
                tenant_id=deployment.tenant_id,
                artifact_id=deployment.artifact_id,
                model_version_id=deployment.model_version_id,
                target_type=deployment.target_type.value,
                stage=deployment.stage.value,
                status=deployment.status.value,
                traffic_percentage=deployment.traffic_percentage,
                previous_deployment_id=deployment.previous_deployment_id,
                deployment_json=payload,
                created_at=deployment.created_at,
                activated_at=deployment.activated_at,
                completed_at=deployment.completed_at,
            )
        )

    @staticmethod
    def _supersede_previous_production(
        session: Session,
        deployment: ModelDeployment,
    ) -> None:
        previous = session.scalar(
            select(ModelDeploymentRow)
            .where(
                ModelDeploymentRow.tenant_id == deployment.tenant_id,
                ModelDeploymentRow.target_type == deployment.target_type.value,
                ModelDeploymentRow.stage == ModelLifecycleStage.PRODUCTION.value,
                ModelDeploymentRow.status == DeploymentStatus.ACTIVE.value,
                ModelDeploymentRow.deployment_id != deployment.deployment_id,
            )
            .with_for_update()
        )
        expected_previous = previous.deployment_id if previous else None
        if deployment.previous_deployment_id != expected_previous:
            raise WorkflowPersistenceError("Production predecessor changed during promotion")
        if previous is None:
            return
        existing = _deployment_from_payload(previous.deployment_json)
        completed_at = deployment.activated_at or deployment.created_at
        superseded = replace(
            existing,
            status=DeploymentStatus.SUPERSEDED,
            completed_at=completed_at,
        )
        previous.status = superseded.status.value
        previous.deployment_json = _deployment_payload(superseded)
        previous.completed_at = completed_at

    @staticmethod
    def _update_artifact(session: Session, artifact: ModelArtifact) -> None:
        row = session.get(ModelArtifactRow, artifact.artifact_id)
        if row is None or row.tenant_id != artifact.tenant_id:
            raise WorkflowPersistenceError("Rollback artifact was not found")
        row.stage = artifact.stage.value
        row.is_valid = artifact.is_valid
        row.artifact_json = _artifact_payload(artifact)
        row.invalidated_at = artifact.invalidated_at

    @staticmethod
    def _validate_promotion_replay(
        session: Session,
        artifact: ModelArtifact,
        decision: PromotionDecision,
        deployment: ModelDeployment | None,
    ) -> None:
        decision_row = session.get(
            PromotionDecisionRow,
            _tenant_key(decision.tenant_id, decision.decision_id),
        )
        if decision_row is None or _canonical(decision_row.decision_json) != _canonical(
            _decision_payload(decision)
        ):
            raise WorkflowPersistenceError("Promotion replay has no matching decision")
        if deployment is not None:
            deployment_row = session.get(
                ModelDeploymentRow,
                _tenant_key(deployment.tenant_id, deployment.deployment_id),
            )
            if deployment_row is None or _canonical(
                deployment_row.deployment_json
            ) != _canonical(_deployment_payload(deployment)):
                raise WorkflowPersistenceError("Promotion replay changed deployment")
        row = session.get(ModelArtifactRow, artifact.artifact_id)
        if row is None or _canonical(row.artifact_json) != _canonical(
            _artifact_payload(artifact)
        ):
            raise WorkflowPersistenceError("Promotion replay changed artifact")


def _validate_training_run_update(existing: TrainingRun, updated: TrainingRun) -> None:
    immutable_existing = (
        existing.training_run_id,
        existing.tenant_id,
        existing.target_type,
        existing.provider,
        existing.base_model,
        existing.base_model_version,
        existing.candidate_model_version_id,
        existing.candidate_model_version,
        existing.training_dataset_tenant_scope,
        existing.training_dataset_id,
        existing.training_dataset_version,
        existing.validation_dataset_id,
        existing.validation_dataset_version,
        existing.evaluation_dataset_id,
        existing.evaluation_dataset_version,
        existing.schema_version,
        existing.hyperparameters,
        existing.code_version,
        existing.training_artifact_references,
        existing.created_at,
    )
    immutable_updated = (
        updated.training_run_id,
        updated.tenant_id,
        updated.target_type,
        updated.provider,
        updated.base_model,
        updated.base_model_version,
        updated.candidate_model_version_id,
        updated.candidate_model_version,
        updated.training_dataset_tenant_scope,
        updated.training_dataset_id,
        updated.training_dataset_version,
        updated.validation_dataset_id,
        updated.validation_dataset_version,
        updated.evaluation_dataset_id,
        updated.evaluation_dataset_version,
        updated.schema_version,
        updated.hyperparameters,
        updated.code_version,
        updated.training_artifact_references,
        updated.created_at,
    )
    if immutable_existing != immutable_updated:
        raise WorkflowPersistenceError("Training run bindings are immutable")
    transitions = {
        TrainingRunStatus.CREATED: {
            TrainingRunStatus.CREATED,
            TrainingRunStatus.EXPORT_READY,
            TrainingRunStatus.UNSUPPORTED,
        },
        TrainingRunStatus.EXPORT_READY: {
            TrainingRunStatus.EXPORT_READY,
            TrainingRunStatus.SUBMITTED,
            TrainingRunStatus.QUEUED,
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
            TrainingRunStatus.UNSUPPORTED,
        },
        TrainingRunStatus.SUBMITTED: {
            TrainingRunStatus.SUBMITTED,
            TrainingRunStatus.QUEUED,
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.CANCELING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
        },
        TrainingRunStatus.QUEUED: {
            TrainingRunStatus.QUEUED,
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.CANCELING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
        },
        TrainingRunStatus.RUNNING: {
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.CANCELING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
        },
        TrainingRunStatus.CANCELING: {
            TrainingRunStatus.CANCELING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
        },
        TrainingRunStatus.SUCCEEDED: {TrainingRunStatus.SUCCEEDED},
        TrainingRunStatus.FAILED: {TrainingRunStatus.FAILED},
        TrainingRunStatus.CANCELED: {TrainingRunStatus.CANCELED},
        TrainingRunStatus.UNSUPPORTED: {TrainingRunStatus.UNSUPPORTED},
    }
    if updated.status not in transitions[existing.status]:
        raise WorkflowPersistenceError("Training run status cannot move backwards")
    if existing.remote_job_id is not None and updated.remote_job_id != existing.remote_job_id:
        raise WorkflowPersistenceError("Remote training job identity is immutable")
    if existing.status in _TERMINAL_TRAINING_STATUSES and existing != updated:
        raise WorkflowPersistenceError("Terminal training runs are immutable")


def _validate_evaluation_update(
    existing: ModelEvaluation,
    updated: ModelEvaluation,
) -> None:
    immutable_existing = replace(
        existing,
        status=ModelEvaluationStatus.PENDING,
        started_at=None,
        completed_at=None,
        failure_code=None,
    )
    immutable_updated = replace(
        updated,
        status=ModelEvaluationStatus.PENDING,
        started_at=None,
        completed_at=None,
        failure_code=None,
    )
    if immutable_existing != immutable_updated:
        raise WorkflowPersistenceError("Model evaluation bindings are immutable")
    transitions = {
        ModelEvaluationStatus.PENDING: {
            ModelEvaluationStatus.PENDING,
            ModelEvaluationStatus.RUNNING,
            ModelEvaluationStatus.FAILED,
        },
        ModelEvaluationStatus.RUNNING: {
            ModelEvaluationStatus.RUNNING,
            ModelEvaluationStatus.PASSED,
            ModelEvaluationStatus.FAILED,
        },
        ModelEvaluationStatus.PASSED: {ModelEvaluationStatus.PASSED},
        ModelEvaluationStatus.FAILED: {ModelEvaluationStatus.FAILED},
    }
    if updated.status not in transitions[existing.status]:
        raise WorkflowPersistenceError("Model evaluation status cannot move backwards")
    if existing.status in {
        ModelEvaluationStatus.PASSED,
        ModelEvaluationStatus.FAILED,
    } and existing != updated:
        raise WorkflowPersistenceError("Terminal model evaluations are immutable")


def _training_run_payload(run: TrainingRun) -> dict[str, Any]:
    return {
        "training_run_id": run.training_run_id,
        "tenant_id": run.tenant_id,
        "target_type": run.target_type.value,
        "provider": run.provider,
        "base_model": run.base_model,
        "base_model_version": run.base_model_version,
        "candidate_model_version_id": run.candidate_model_version_id,
        "candidate_model_version": run.candidate_model_version,
        "training_dataset_tenant_scope": run.training_dataset_tenant_scope,
        "training_dataset_id": run.training_dataset_id,
        "training_dataset_version": run.training_dataset_version,
        "validation_dataset_id": run.validation_dataset_id,
        "validation_dataset_version": run.validation_dataset_version,
        "evaluation_dataset_id": run.evaluation_dataset_id,
        "evaluation_dataset_version": run.evaluation_dataset_version,
        "schema_version": run.schema_version,
        "hyperparameters": [
            {"name": item.name, "value": item.value} for item in run.hyperparameters
        ],
        "code_version": run.code_version,
        "training_artifact_references": list(run.training_artifact_references),
        "status": run.status.value,
        "created_at": run.created_at.isoformat(),
        "remote_job_id": run.remote_job_id,
        "provider_artifact_reference": run.provider_artifact_reference,
        "submitted_at": _iso(run.submitted_at),
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "failure_code": run.failure_code,
    }


def _training_run_from_payload(payload: dict[str, Any]) -> TrainingRun:
    return TrainingRun(
        training_run_id=str(payload["training_run_id"]),
        tenant_id=str(payload["tenant_id"]),
        target_type=TrainingTargetType(str(payload["target_type"])),
        provider=str(payload["provider"]),
        base_model=str(payload["base_model"]),
        base_model_version=str(payload["base_model_version"]),
        candidate_model_version_id=str(payload["candidate_model_version_id"]),
        candidate_model_version=str(payload["candidate_model_version"]),
        training_dataset_tenant_scope=str(payload["training_dataset_tenant_scope"]),
        training_dataset_id=str(payload["training_dataset_id"]),
        training_dataset_version=str(payload["training_dataset_version"]),
        validation_dataset_id=str(payload["validation_dataset_id"]),
        validation_dataset_version=str(payload["validation_dataset_version"]),
        evaluation_dataset_id=str(payload["evaluation_dataset_id"]),
        evaluation_dataset_version=str(payload["evaluation_dataset_version"]),
        schema_version=str(payload["schema_version"]),
        hyperparameters=tuple(
            TrainingHyperparameter(
                name=str(item["name"]),
                value=cast(Any, item.get("value")),
            )
            for item in (
                cast(dict[str, Any], value)
                for value in cast(list[object], payload["hyperparameters"])
            )
        ),
        code_version=str(payload["code_version"]),
        training_artifact_references=tuple(
            str(item)
            for item in cast(list[object], payload["training_artifact_references"])
        ),
        status=TrainingRunStatus(str(payload["status"])),
        created_at=_datetime(payload["created_at"]),
        remote_job_id=_optional_string(payload.get("remote_job_id")),
        provider_artifact_reference=_optional_string(
            payload.get("provider_artifact_reference")
        ),
        submitted_at=_optional_datetime(payload.get("submitted_at")),
        started_at=_optional_datetime(payload.get("started_at")),
        completed_at=_optional_datetime(payload.get("completed_at")),
        failure_code=_optional_string(payload.get("failure_code")),
    )


def _artifact_payload(artifact: ModelArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "tenant_id": artifact.tenant_id,
        "training_run_id": artifact.training_run_id,
        "model_version_id": artifact.model_version_id,
        "model_version": artifact.model_version,
        "target_type": artifact.target_type.value,
        "provider": artifact.provider,
        "provider_artifact_reference": artifact.provider_artifact_reference,
        "base_model": artifact.base_model,
        "base_model_version": artifact.base_model_version,
        "training_dataset_id": artifact.training_dataset_id,
        "training_dataset_version": artifact.training_dataset_version,
        "code_version": artifact.code_version,
        "stage": artifact.stage.value,
        "created_at": artifact.created_at.isoformat(),
        "is_valid": artifact.is_valid,
        "invalidated_reason": artifact.invalidated_reason,
        "invalidated_at": _iso(artifact.invalidated_at),
    }


def _artifact_from_payload(payload: dict[str, Any]) -> ModelArtifact:
    return ModelArtifact(
        artifact_id=str(payload["artifact_id"]),
        tenant_id=str(payload["tenant_id"]),
        training_run_id=str(payload["training_run_id"]),
        model_version_id=str(payload["model_version_id"]),
        model_version=str(payload["model_version"]),
        target_type=TrainingTargetType(str(payload["target_type"])),
        provider=str(payload["provider"]),
        provider_artifact_reference=str(payload["provider_artifact_reference"]),
        base_model=str(payload["base_model"]),
        base_model_version=str(payload["base_model_version"]),
        training_dataset_id=str(payload["training_dataset_id"]),
        training_dataset_version=str(payload["training_dataset_version"]),
        code_version=str(payload["code_version"]),
        stage=ModelLifecycleStage(str(payload["stage"])),
        created_at=_datetime(payload["created_at"]),
        is_valid=_bool(payload["is_valid"]),
        invalidated_reason=_optional_string(payload.get("invalidated_reason")),
        invalidated_at=_optional_datetime(payload.get("invalidated_at")),
    )


def _evaluation_payload(evaluation: ModelEvaluation) -> dict[str, Any]:
    return {
        "model_evaluation_id": evaluation.model_evaluation_id,
        "tenant_id": evaluation.tenant_id,
        "artifact_id": evaluation.artifact_id,
        "stage": evaluation.stage.value,
        "evaluation_run_id": evaluation.evaluation_run_id,
        "dataset_id": evaluation.dataset_id,
        "dataset_version": evaluation.dataset_version,
        "schema_version": evaluation.schema_version,
        "baseline_model_version_id": evaluation.baseline_model_version_id,
        "candidate_model_version_id": evaluation.candidate_model_version_id,
        "prompt_version": evaluation.prompt_version,
        "threshold_version": evaluation.threshold_version,
        "comparisons": [
            {
                "metric_name": item.metric_name,
                "baseline_value": item.baseline_value,
                "candidate_value": item.candidate_value,
                "direction": item.direction.value,
                "maximum_regression": item.maximum_regression,
            }
            for item in evaluation.comparisons
        ],
        "status": evaluation.status.value,
        "created_at": evaluation.created_at.isoformat(),
        "started_at": _iso(evaluation.started_at),
        "completed_at": _iso(evaluation.completed_at),
        "failure_code": evaluation.failure_code,
    }


def _evaluation_from_payload(payload: dict[str, Any]) -> ModelEvaluation:
    return ModelEvaluation(
        model_evaluation_id=str(payload["model_evaluation_id"]),
        tenant_id=str(payload["tenant_id"]),
        artifact_id=str(payload["artifact_id"]),
        stage=ModelLifecycleStage(str(payload["stage"])),
        evaluation_run_id=str(payload["evaluation_run_id"]),
        dataset_id=str(payload["dataset_id"]),
        dataset_version=str(payload["dataset_version"]),
        schema_version=str(payload["schema_version"]),
        baseline_model_version_id=str(payload["baseline_model_version_id"]),
        candidate_model_version_id=str(payload["candidate_model_version_id"]),
        prompt_version=str(payload["prompt_version"]),
        threshold_version=str(payload["threshold_version"]),
        comparisons=tuple(
            ModelMetricComparison(
                metric_name=str(item["metric_name"]),
                baseline_value=float(item["baseline_value"]),
                candidate_value=float(item["candidate_value"]),
                direction=MetricDirection(str(item["direction"])),
                maximum_regression=float(item["maximum_regression"]),
            )
            for item in (
                cast(dict[str, Any], value)
                for value in cast(list[object], payload["comparisons"])
            )
        ),
        status=ModelEvaluationStatus(str(payload["status"])),
        created_at=_datetime(payload["created_at"]),
        started_at=_optional_datetime(payload.get("started_at")),
        completed_at=_optional_datetime(payload.get("completed_at")),
        failure_code=_optional_string(payload.get("failure_code")),
    )


def _deployment_payload(deployment: ModelDeployment) -> dict[str, Any]:
    return {
        "deployment_id": deployment.deployment_id,
        "tenant_id": deployment.tenant_id,
        "artifact_id": deployment.artifact_id,
        "model_version_id": deployment.model_version_id,
        "target_type": deployment.target_type.value,
        "stage": deployment.stage.value,
        "status": deployment.status.value,
        "traffic_percentage": deployment.traffic_percentage,
        "requested_by": deployment.requested_by,
        "deployment_reference": deployment.deployment_reference,
        "created_at": deployment.created_at.isoformat(),
        "previous_deployment_id": deployment.previous_deployment_id,
        "activated_at": _iso(deployment.activated_at),
        "completed_at": _iso(deployment.completed_at),
        "failure_code": deployment.failure_code,
    }


def _deployment_from_payload(payload: dict[str, Any]) -> ModelDeployment:
    return ModelDeployment(
        deployment_id=str(payload["deployment_id"]),
        tenant_id=str(payload["tenant_id"]),
        artifact_id=str(payload["artifact_id"]),
        model_version_id=str(payload["model_version_id"]),
        target_type=TrainingTargetType(str(payload["target_type"])),
        stage=ModelLifecycleStage(str(payload["stage"])),
        status=DeploymentStatus(str(payload["status"])),
        traffic_percentage=float(payload["traffic_percentage"]),
        requested_by=str(payload["requested_by"]),
        deployment_reference=str(payload["deployment_reference"]),
        created_at=_datetime(payload["created_at"]),
        previous_deployment_id=_optional_string(payload.get("previous_deployment_id")),
        activated_at=_optional_datetime(payload.get("activated_at")),
        completed_at=_optional_datetime(payload.get("completed_at")),
        failure_code=_optional_string(payload.get("failure_code")),
    )


def _decision_payload(decision: PromotionDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "tenant_id": decision.tenant_id,
        "artifact_id": decision.artifact_id,
        "from_stage": decision.from_stage.value,
        "to_stage": decision.to_stage.value,
        "decision": decision.decision.value,
        "evaluation_ids": list(decision.evaluation_ids),
        "reason": decision.reason,
        "decided_at": decision.decided_at.isoformat(),
        "reviewer_id": decision.reviewer_id,
    }


def _decision_from_payload(payload: dict[str, Any]) -> PromotionDecision:
    return PromotionDecision(
        decision_id=str(payload["decision_id"]),
        tenant_id=str(payload["tenant_id"]),
        artifact_id=str(payload["artifact_id"]),
        from_stage=ModelLifecycleStage(str(payload["from_stage"])),
        to_stage=ModelLifecycleStage(str(payload["to_stage"])),
        decision=PromotionDecisionType(str(payload["decision"])),
        evaluation_ids=tuple(
            str(item) for item in cast(list[object], payload["evaluation_ids"])
        ),
        reason=str(payload["reason"]),
        decided_at=_datetime(payload["decided_at"]),
        reviewer_id=_optional_string(payload.get("reviewer_id")),
    )


_TERMINAL_TRAINING_STATUSES = {
    TrainingRunStatus.SUCCEEDED,
    TrainingRunStatus.FAILED,
    TrainingRunStatus.CANCELED,
    TrainingRunStatus.UNSUPPORTED,
}

_NEXT_ARTIFACT_STAGE = {
    ModelLifecycleStage.REGISTERED: ModelLifecycleStage.OFFLINE_EVALUATION,
    ModelLifecycleStage.OFFLINE_EVALUATION: ModelLifecycleStage.SHADOW,
    ModelLifecycleStage.SHADOW: ModelLifecycleStage.CANARY,
    ModelLifecycleStage.CANARY: ModelLifecycleStage.PRODUCTION,
}


def _dataset_key(tenant_scope: str, dataset_id: str, version: str) -> str:
    return sha256(f"{tenant_scope}\0{dataset_id}\0{version}".encode("utf-8")).hexdigest()


def _tenant_key(tenant_id: str, value: str) -> str:
    return sha256(f"{tenant_id}\0{value}".encode("utf-8")).hexdigest()


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _optional_datetime(value: object) -> datetime | None:
    return _datetime(value) if value is not None else None


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Persisted model-training boolean has an invalid type")
    return value
