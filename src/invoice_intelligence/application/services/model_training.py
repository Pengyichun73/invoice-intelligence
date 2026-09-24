"""Offline-only training orchestration, promotion gates, and rollback control."""

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
    TrainingDataError,
)
from invoice_intelligence.application.ports.training import (
    ModelDeploymentController,
    ModelRegistryRepository,
    RemoteTrainingProvider,
    TrainingDataRepository,
    TrainingRunRepository,
)
from invoice_intelligence.domain.training import (
    TRAINING_TARGET_PRIORITY,
    DeploymentStatus,
    MetricDirection,
    ModelArtifact,
    ModelDeployment,
    ModelEvaluation,
    ModelEvaluationStatus,
    ModelLifecycleStage,
    PromotionDecision,
    PromotionDecisionType,
    ProviderTrainingCapabilities,
    RemoteTrainingJob,
    TrainingDatasetStatus,
    TrainingDatasetVersion,
    TrainingHyperparameter,
    TrainingRun,
    TrainingRunStatus,
    TrainingTargetType,
)


@dataclass(frozen=True, slots=True)
class TrainingPlanRequest:
    """Operator-owned immutable bindings for one optional remote training run."""

    training_run_id: str
    tenant_id: str
    tenant_scope: str
    training_dataset_id: str
    training_dataset_version: str
    validation_dataset_id: str
    validation_dataset_version: str
    evaluation_dataset_id: str
    evaluation_dataset_version: str
    schema_version: str
    provider: str
    base_model: str
    base_model_version: str
    candidate_model_version_id: str
    candidate_model_version: str
    code_version: str
    requested_targets: tuple[TrainingTargetType, ...]
    hyperparameters: tuple[TrainingHyperparameter, ...] = ()
    allow_vision_generation: bool = False

    def __post_init__(self) -> None:
        values = (
            self.training_run_id,
            self.tenant_id,
            self.tenant_scope,
            self.training_dataset_id,
            self.training_dataset_version,
            self.validation_dataset_id,
            self.validation_dataset_version,
            self.evaluation_dataset_id,
            self.evaluation_dataset_version,
            self.schema_version,
            self.provider,
            self.base_model,
            self.base_model_version,
            self.candidate_model_version_id,
            self.candidate_model_version,
            self.code_version,
        )
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("Training plan identifiers and versions must be normalized")
        if not self.requested_targets:
            raise ValueError("Training plan requires at least one target")
        if len(self.requested_targets) != len(set(self.requested_targets)):
            raise ValueError("Training plan target types must be unique")
        if (
            TrainingTargetType.VISION_GENERATION in self.requested_targets
            and not self.allow_vision_generation
        ):
            raise ValueError("Vision/generative fine-tuning requires an explicit opt-in")


@dataclass(frozen=True, slots=True)
class PromotionGatePolicy:
    """Versioned deterministic thresholds shared by promotion and rollback."""

    version: str
    primary_metric_name: str
    maximum_primary_regression: float = 0.0
    maximum_review_required_rate_increase: float = 0.0
    maximum_erroneous_auto_fill_increase: float = 0.0

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.primary_metric_name.strip():
            raise ValueError("Promotion gate version and primary metric must not be empty")
        values = (
            self.maximum_primary_regression,
            self.maximum_review_required_rate_increase,
            self.maximum_erroneous_auto_fill_increase,
        )
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("Promotion gate tolerances must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    """Explicit request to cross exactly one model lifecycle boundary."""

    decision_id: str
    operation_id: str
    tenant_id: str
    artifact_id: str
    target_stage: ModelLifecycleStage
    requested_by: str
    reason: str
    traffic_percentage: float
    reviewer_id: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.decision_id,
            self.operation_id,
            self.tenant_id,
            self.artifact_id,
            self.requested_by,
            self.reason,
        ):
            if not value.strip() or value != value.strip():
                raise ValueError("Promotion request text must be non-empty and normalized")
        if self.target_stage not in {
            ModelLifecycleStage.OFFLINE_EVALUATION,
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
            ModelLifecycleStage.PRODUCTION,
        }:
            raise ValueError("Promotion target must be an ordered pre-production stage")
        if self.target_stage in {
            ModelLifecycleStage.OFFLINE_EVALUATION,
            ModelLifecycleStage.SHADOW,
        } and self.traffic_percentage != 0.0:
            raise ValueError("Offline and shadow stages cannot receive production traffic")
        if (
            self.target_stage is ModelLifecycleStage.CANARY
            and not 0.0 < self.traffic_percentage < 100.0
        ):
            raise ValueError("Canary promotion requires partial traffic")
        if (
            self.target_stage is ModelLifecycleStage.PRODUCTION
            and self.traffic_percentage != 100.0
        ):
            raise ValueError("Production promotion requires all configured traffic")


@dataclass(frozen=True, slots=True)
class RollbackRequest:
    """Accountable request to restore the immediately preceding production deployment."""

    decision_id: str
    operation_id: str
    tenant_id: str
    current_deployment_id: str
    target_deployment_id: str
    production_evaluation_id: str
    requested_by: str
    reason: str

    def __post_init__(self) -> None:
        values = (
            self.decision_id,
            self.operation_id,
            self.tenant_id,
            self.current_deployment_id,
            self.target_deployment_id,
            self.production_evaluation_id,
            self.requested_by,
            self.reason,
        )
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("Rollback request text must be non-empty and normalized")
        if self.current_deployment_id == self.target_deployment_id:
            raise ValueError("Rollback current and target deployments must differ")


class TrainingPlanningService:
    """Select the highest-priority documented capability and freeze a run plan."""

    def __init__(
        self,
        *,
        training_data_repository: TrainingDataRepository,
        run_repository: TrainingRunRepository,
        provider: RemoteTrainingProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._training_data = training_data_repository
        self._runs = run_repository
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(UTC))

    async def plan(self, request: TrainingPlanRequest) -> TrainingRun:
        existing = await self._runs.get_training_run(
            request.tenant_id,
            request.training_run_id,
        )
        if existing is not None:
            self._validate_plan_replay(existing, request)
            return existing

        stored = await self._training_data.get_dataset(
            request.tenant_scope,
            request.training_dataset_id,
            request.training_dataset_version,
        )
        if stored is None:
            raise ResourceNotFoundError("Exported training dataset was not found")
        dataset, _ = stored
        self._validate_dataset(dataset, request)

        capabilities = await self._provider.capabilities()
        if capabilities.provider != request.provider:
            raise ResourceConflictError("Training provider capability identity changed")
        target = self._select_target(request, capabilities)
        now = self._now()
        supported = target in capabilities.supported_targets
        run = TrainingRun(
            training_run_id=request.training_run_id,
            tenant_id=request.tenant_id,
            target_type=target,
            provider=request.provider,
            base_model=request.base_model,
            base_model_version=request.base_model_version,
            candidate_model_version_id=request.candidate_model_version_id,
            candidate_model_version=request.candidate_model_version,
            training_dataset_tenant_scope=dataset.tenant_scope,
            training_dataset_id=dataset.dataset_id,
            training_dataset_version=dataset.version,
            validation_dataset_id=request.validation_dataset_id,
            validation_dataset_version=request.validation_dataset_version,
            evaluation_dataset_id=request.evaluation_dataset_id,
            evaluation_dataset_version=request.evaluation_dataset_version,
            schema_version=request.schema_version,
            hyperparameters=request.hyperparameters,
            code_version=request.code_version,
            training_artifact_references=dataset.artifact_references,
            status=(
                TrainingRunStatus.EXPORT_READY
                if supported
                else TrainingRunStatus.UNSUPPORTED
            ),
            created_at=now,
            completed_at=None if supported else now,
            failure_code=None if supported else "REMOTE_TRAINING_CAPABILITY_UNAVAILABLE",
        )
        await self._runs.save_training_run(run)
        return run

    @staticmethod
    def _select_target(
        request: TrainingPlanRequest,
        capabilities: ProviderTrainingCapabilities,
    ) -> TrainingTargetType:
        requested = set(request.requested_targets)
        eligible = tuple(
            target
            for target in TRAINING_TARGET_PRIORITY
            if target in requested
            and (
                target is not TrainingTargetType.VISION_GENERATION
                or request.allow_vision_generation
            )
        )
        for target in eligible:
            if target in capabilities.supported_targets:
                return target
        return eligible[0]

    @staticmethod
    def _validate_dataset(
        dataset: TrainingDatasetVersion,
        request: TrainingPlanRequest,
    ) -> None:
        if dataset.status is not TrainingDatasetStatus.EXPORTED:
            raise ResourceConflictError("Training requires an exported dataset version")
        if request.tenant_id not in dataset.source_tenant_ids:
            raise ResourceConflictError("Training dataset does not belong to the tenant")
        if dataset.schema_version != request.schema_version:
            raise ResourceConflictError("Training dataset Schema version does not match")

    @staticmethod
    def _validate_plan_replay(existing: TrainingRun, request: TrainingPlanRequest) -> None:
        immutable = (
            existing.training_run_id,
            existing.tenant_id,
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
        )
        replay = (
            request.training_run_id,
            request.tenant_id,
            request.provider,
            request.base_model,
            request.base_model_version,
            request.candidate_model_version_id,
            request.candidate_model_version,
            request.tenant_scope,
            request.training_dataset_id,
            request.training_dataset_version,
            request.validation_dataset_id,
            request.validation_dataset_version,
            request.evaluation_dataset_id,
            request.evaluation_dataset_version,
            request.schema_version,
            request.hyperparameters,
            request.code_version,
        )
        if immutable != replay:
            raise ResourceConflictError("Training run identifier was replayed with new bindings")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Training planning clock must return timezone-aware values")
        return value


class OfflineTrainingOrchestrationService:
    """Non-blocking worker use case; callers explicitly submit, refresh, or cancel."""

    def __init__(
        self,
        *,
        training_data_repository: TrainingDataRepository,
        run_repository: TrainingRunRepository,
        model_registry: ModelRegistryRepository,
        provider: RemoteTrainingProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._training_data = training_data_repository
        self._runs = run_repository
        self._registry = model_registry
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(UTC))

    async def submit(self, tenant_id: str, training_run_id: str) -> TrainingRun:
        run = await self._require_run(tenant_id, training_run_id)
        if run.status in _TRAINING_TERMINAL_STATES | _TRAINING_REMOTE_STATES:
            return run
        if run.status is not TrainingRunStatus.EXPORT_READY:
            raise ResourceConflictError("Training run is not ready for remote submission")
        dataset = await self._require_dataset(run)
        capabilities = await self._provider.capabilities()
        if run.target_type not in capabilities.supported_targets:
            unsupported = replace(
                run,
                status=TrainingRunStatus.UNSUPPORTED,
                completed_at=self._now(),
                failure_code="REMOTE_TRAINING_CAPABILITY_UNAVAILABLE",
            )
            await self._runs.save_training_run(unsupported)
            return unsupported
        job = await self._provider.submit(
            run,
            dataset,
            idempotency_key=run.training_run_id,
        )
        updated = self._apply_job(run, job)
        await self._runs.save_training_run(updated)
        await self._register_success(updated)
        return updated

    async def refresh(self, tenant_id: str, training_run_id: str) -> TrainingRun:
        run = await self._require_run(tenant_id, training_run_id)
        if run.status in _TRAINING_TERMINAL_STATES:
            return run
        if run.status not in _TRAINING_REMOTE_STATES or run.remote_job_id is None:
            raise ResourceConflictError("Training run has not been submitted")
        job = await self._provider.get_job(run.remote_job_id)
        updated = self._apply_job(run, job)
        await self._runs.save_training_run(updated)
        await self._register_success(updated)
        return updated

    async def cancel(self, tenant_id: str, training_run_id: str) -> TrainingRun:
        run = await self._require_run(tenant_id, training_run_id)
        if run.status in _TRAINING_TERMINAL_STATES:
            return run
        if run.status not in _TRAINING_REMOTE_STATES or run.remote_job_id is None:
            raise ResourceConflictError("Only a submitted training run can be canceled")
        job = await self._provider.cancel(
            run.remote_job_id,
            idempotency_key=f"cancel:{run.training_run_id}",
        )
        updated = self._apply_job(run, job)
        await self._runs.save_training_run(updated)
        return updated

    async def _require_run(self, tenant_id: str, training_run_id: str) -> TrainingRun:
        run = await self._runs.get_training_run(tenant_id, training_run_id)
        if run is None:
            raise ResourceNotFoundError("Training run was not found")
        return run

    async def _require_dataset(self, run: TrainingRun) -> TrainingDatasetVersion:
        stored = await self._training_data.get_dataset(
            run.training_dataset_tenant_scope,
            run.training_dataset_id,
            run.training_dataset_version,
        )
        if stored is None:
            raise ResourceNotFoundError("Training run dataset was not found")
        dataset, _ = stored
        if dataset.status is not TrainingDatasetStatus.EXPORTED:
            raise ResourceConflictError("Training run dataset is no longer export-ready")
        return dataset

    def _apply_job(self, run: TrainingRun, job: RemoteTrainingJob) -> TrainingRun:
        if run.remote_job_id is not None and run.remote_job_id != job.provider_job_id:
            raise ResourceConflictError("Remote provider changed the training job identity")
        now = self._now()
        started_at = run.started_at
        if job.status in {
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.SUCCEEDED,
        } and started_at is None:
            started_at = now
        completed_at = (
            now if job.status in _TRAINING_TERMINAL_STATES else run.completed_at
        )
        return replace(
            run,
            status=job.status,
            remote_job_id=job.provider_job_id,
            provider_artifact_reference=job.artifact_reference,
            submitted_at=run.submitted_at or now,
            started_at=started_at,
            completed_at=completed_at,
            failure_code=job.failure_code,
        )

    async def _register_success(self, run: TrainingRun) -> None:
        if run.status is not TrainingRunStatus.SUCCEEDED:
            return
        if run.provider_artifact_reference is None:
            raise TrainingDataError("Successful training has no provider artifact")
        artifact = ModelArtifact(
            artifact_id=_stable_id(
                "model-artifact",
                run.tenant_id,
                run.training_run_id,
                run.provider_artifact_reference,
            ),
            tenant_id=run.tenant_id,
            training_run_id=run.training_run_id,
            model_version_id=run.candidate_model_version_id,
            model_version=run.candidate_model_version,
            target_type=run.target_type,
            provider=run.provider,
            provider_artifact_reference=run.provider_artifact_reference,
            base_model=run.base_model,
            base_model_version=run.base_model_version,
            training_dataset_id=run.training_dataset_id,
            training_dataset_version=run.training_dataset_version,
            code_version=run.code_version,
            stage=ModelLifecycleStage.REGISTERED,
            created_at=run.completed_at or self._now(),
        )
        await self._registry.register_artifact(artifact)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Training orchestration clock must be timezone-aware")
        return value


class ModelPromotionService:
    """Apply ordered gates; production remains impossible without a human reviewer."""

    def __init__(
        self,
        *,
        registry: ModelRegistryRepository,
        deployment_controller: ModelDeploymentController,
        policy: PromotionGatePolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._deployments = deployment_controller
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))

    async def promote(
        self,
        request: PromotionRequest,
    ) -> tuple[PromotionDecision, ModelDeployment | None]:
        replay = await self._promotion_replay(request)
        if replay is not None:
            return replay
        artifact = await self._registry.get_artifact(request.tenant_id, request.artifact_id)
        if artifact is None:
            raise ResourceNotFoundError("Model artifact was not found")
        if not artifact.is_valid:
            raise ResourceConflictError("Invalidated model artifact cannot be promoted")
        expected_target = _NEXT_PROMOTION_STAGE.get(artifact.stage)
        if expected_target is not request.target_stage:
            raise ResourceConflictError("Model lifecycle stages cannot be skipped")

        evaluations = await self._registry.list_evaluations(
            request.tenant_id,
            request.artifact_id,
        )
        required_stages = _required_evaluation_stages(request.target_stage)
        passed, gate_ids, rejection_reason = self._evaluate_gates(
            artifact,
            evaluations,
            required_stages,
        )
        if (
            request.target_stage is ModelLifecycleStage.PRODUCTION
            and request.reviewer_id is None
        ):
            passed = False
            rejection_reason = "PRODUCTION_HUMAN_APPROVAL_MISSING"

        decision = PromotionDecision(
            decision_id=request.decision_id,
            tenant_id=request.tenant_id,
            artifact_id=request.artifact_id,
            from_stage=artifact.stage,
            to_stage=request.target_stage,
            decision=(
                PromotionDecisionType.APPROVED
                if passed
                else PromotionDecisionType.REJECTED
            ),
            evaluation_ids=gate_ids,
            reason=request.reason if passed else rejection_reason,
            decided_at=self._now(),
            reviewer_id=request.reviewer_id,
        )
        if not passed:
            await self._registry.save_promotion_decision(decision)
            return decision, None

        deployment: ModelDeployment | None = None
        if request.target_stage in {
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
            ModelLifecycleStage.PRODUCTION,
        }:
            previous = (
                await self._registry.get_active_production(
                    request.tenant_id,
                    artifact.target_type,
                )
                if request.target_stage is ModelLifecycleStage.PRODUCTION
                else None
            )
            deployment = await self._deployments.deploy(
                artifact,
                stage=request.target_stage,
                traffic_percentage=request.traffic_percentage,
                operation_id=request.operation_id,
                requested_by=request.requested_by,
                previous_deployment=previous,
            )
            self._validate_deployment(deployment, request, artifact)
        promoted = replace(artifact, stage=request.target_stage)
        await self._registry.commit_promotion(promoted, decision, deployment)
        return decision, deployment

    async def _promotion_replay(
        self,
        request: PromotionRequest,
    ) -> tuple[PromotionDecision, ModelDeployment | None] | None:
        decision = await self._registry.get_promotion_decision(
            request.tenant_id,
            request.decision_id,
        )
        if decision is None:
            return None
        if (
            decision.tenant_id != request.tenant_id
            or decision.artifact_id != request.artifact_id
            or decision.to_stage is not request.target_stage
            or decision.reviewer_id != request.reviewer_id
        ):
            raise ResourceConflictError("Promotion decision identifier was reused")
        if decision.decision is PromotionDecisionType.REJECTED:
            return decision, None
        deployment = None
        if request.target_stage in {
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
            ModelLifecycleStage.PRODUCTION,
        }:
            deployment = await self._registry.get_deployment(
                request.tenant_id,
                request.operation_id,
            )
            if deployment is None:
                raise ResourceConflictError("Promotion replay is missing its deployment")
            if (
                deployment.artifact_id != request.artifact_id
                or deployment.stage is not request.target_stage
                or deployment.traffic_percentage != request.traffic_percentage
            ):
                raise ResourceConflictError("Promotion replay deployment changed")
        return decision, deployment

    def _evaluate_gates(
        self,
        artifact: ModelArtifact,
        evaluations: tuple[ModelEvaluation, ...],
        required_stages: tuple[ModelLifecycleStage, ...],
    ) -> tuple[bool, tuple[str, ...], str]:
        selected: list[ModelEvaluation] = []
        for stage in required_stages:
            matches = tuple(
                item
                for item in evaluations
                if item.stage is stage
                and item.candidate_model_version_id == artifact.model_version_id
            )
            if not matches:
                return False, tuple(item.model_evaluation_id for item in selected), (
                    f"MISSING_{stage.value.upper()}_EVALUATION"
                )
            latest = max(matches, key=lambda item: item.created_at)
            selected.append(latest)
            if latest.status is not ModelEvaluationStatus.PASSED:
                return False, tuple(item.model_evaluation_id for item in selected), (
                    f"{stage.value.upper()}_EVALUATION_NOT_PASSED"
                )
            failure = self.metric_failure(latest)
            if failure is not None:
                return False, tuple(item.model_evaluation_id for item in selected), failure
        return True, tuple(item.model_evaluation_id for item in selected), ""

    def metric_failure(self, evaluation: ModelEvaluation) -> str | None:
        if evaluation.threshold_version != self._policy.version:
            return "PROMOTION_THRESHOLD_VERSION_MISMATCH"
        metrics = {item.metric_name: item for item in evaluation.comparisons}
        required = {
            self._policy.primary_metric_name,
            "review_required_rate",
            "erroneous_auto_filled_value_count",
        }
        if not required.issubset(metrics):
            return "PROMOTION_GATE_METRIC_MISSING"
        primary = metrics[self._policy.primary_metric_name]
        if primary.regression > self._policy.maximum_primary_regression:
            return "PRIMARY_QUALITY_REGRESSION"
        review_rate = metrics["review_required_rate"]
        if review_rate.direction is not MetricDirection.LOWER_IS_BETTER:
            return "REVIEW_REQUIRED_RATE_DIRECTION_INVALID"
        if review_rate.regression > self._policy.maximum_review_required_rate_increase:
            return "REVIEW_REQUIRED_RATE_INCREASED"
        erroneous = metrics["erroneous_auto_filled_value_count"]
        if erroneous.direction is not MetricDirection.LOWER_IS_BETTER:
            return "ERRONEOUS_AUTO_FILL_DIRECTION_INVALID"
        if erroneous.regression > self._policy.maximum_erroneous_auto_fill_increase:
            return "ERRONEOUS_AUTO_FILL_INCREASED"
        return None

    @staticmethod
    def _validate_deployment(
        deployment: ModelDeployment,
        request: PromotionRequest,
        artifact: ModelArtifact,
    ) -> None:
        if (
            deployment.deployment_id != request.operation_id
            or deployment.tenant_id != request.tenant_id
            or deployment.artifact_id != artifact.artifact_id
            or deployment.target_type is not artifact.target_type
            or deployment.stage is not request.target_stage
            or deployment.status is not DeploymentStatus.ACTIVE
        ):
            raise TrainingDataError("Deployment controller returned inconsistent state")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Promotion clock must return timezone-aware values")
        return value


class ModelRollbackService:
    """Restore the previous production artifact when production gate metrics regress."""

    def __init__(
        self,
        *,
        registry: ModelRegistryRepository,
        deployment_controller: ModelDeploymentController,
        policy: PromotionGatePolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._deployments = deployment_controller
        self._promotion_gates = ModelPromotionService(
            registry=registry,
            deployment_controller=deployment_controller,
            policy=policy,
            clock=clock,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def rollback(
        self,
        request: RollbackRequest,
    ) -> tuple[PromotionDecision, ModelDeployment]:
        replay = await self._registry.get_promotion_decision(
            request.tenant_id,
            request.decision_id,
        )
        if replay is not None:
            if (
                replay.decision is not PromotionDecisionType.ROLLBACK_REQUIRED
                or replay.reviewer_id != request.requested_by
            ):
                raise ResourceConflictError("Rollback decision identifier was reused")
            replacement = await self._registry.get_deployment(
                request.tenant_id,
                request.operation_id,
            )
            current = await self._registry.get_deployment(
                request.tenant_id,
                request.current_deployment_id,
            )
            target = await self._registry.get_deployment(
                request.tenant_id,
                request.target_deployment_id,
            )
            if replacement is None or current is None or target is None:
                raise ResourceConflictError("Rollback replay is missing its deployment")
            if (
                replay.artifact_id != current.artifact_id
                or replacement.artifact_id != target.artifact_id
                or replacement.previous_deployment_id != current.deployment_id
            ):
                raise ResourceConflictError("Rollback replay deployment changed")
            return replay, replacement
        current = await self._registry.get_deployment(
            request.tenant_id,
            request.current_deployment_id,
        )
        target = await self._registry.get_deployment(
            request.tenant_id,
            request.target_deployment_id,
        )
        if current is None or target is None:
            raise ResourceNotFoundError("Rollback deployment was not found")
        if (
            current.stage is not ModelLifecycleStage.PRODUCTION
            or current.status is not DeploymentStatus.ACTIVE
            or target.stage is not ModelLifecycleStage.PRODUCTION
            or target.status is not DeploymentStatus.SUPERSEDED
            or current.target_type is not target.target_type
            or current.previous_deployment_id != target.deployment_id
        ):
            raise ResourceConflictError("Rollback target is not the preceding production")
        current_artifact = await self._registry.get_artifact(
            request.tenant_id,
            current.artifact_id,
        )
        target_artifact = await self._registry.get_artifact(
            request.tenant_id,
            target.artifact_id,
        )
        if current_artifact is None or target_artifact is None:
            raise ResourceNotFoundError("Rollback model artifact was not found")
        if (
            not target_artifact.is_valid
            or current_artifact.target_type is not current.target_type
            or target_artifact.target_type is not target.target_type
        ):
            raise ResourceConflictError("Rollback artifact scope is invalid")
        evaluations = await self._registry.list_evaluations(
            request.tenant_id,
            current.artifact_id,
        )
        evaluation = next(
            (
                item
                for item in evaluations
                if item.model_evaluation_id == request.production_evaluation_id
            ),
            None,
        )
        if evaluation is None or evaluation.stage is not ModelLifecycleStage.PRODUCTION:
            raise ResourceConflictError("Rollback requires production evaluation evidence")
        failure = self._promotion_gates.metric_failure(evaluation)
        if evaluation.status is ModelEvaluationStatus.PASSED and failure is None:
            raise ResourceConflictError("Production metrics do not require rollback")

        replacement = await self._deployments.rollback(
            current,
            target,
            operation_id=request.operation_id,
            requested_by=request.requested_by,
        )
        if (
            replacement.deployment_id != request.operation_id
            or replacement.tenant_id != request.tenant_id
            or replacement.artifact_id != target.artifact_id
            or replacement.target_type is not target.target_type
            or replacement.stage is not ModelLifecycleStage.PRODUCTION
            or replacement.status is not DeploymentStatus.ACTIVE
            or replacement.previous_deployment_id != current.deployment_id
        ):
            raise TrainingDataError("Rollback controller returned inconsistent state")
        decision = PromotionDecision(
            decision_id=request.decision_id,
            tenant_id=request.tenant_id,
            artifact_id=current.artifact_id,
            from_stage=ModelLifecycleStage.PRODUCTION,
            to_stage=ModelLifecycleStage.ROLLED_BACK,
            decision=PromotionDecisionType.ROLLBACK_REQUIRED,
            evaluation_ids=(evaluation.model_evaluation_id,),
            reason=request.reason,
            decided_at=self._now(),
            reviewer_id=request.requested_by,
        )
        await self._registry.commit_rollback(
            replace(current_artifact, stage=ModelLifecycleStage.ROLLED_BACK),
            replace(target_artifact, stage=ModelLifecycleStage.PRODUCTION),
            decision,
            replacement,
        )
        return decision, replacement

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Rollback clock must return timezone-aware values")
        return value


_TRAINING_REMOTE_STATES = {
    TrainingRunStatus.SUBMITTED,
    TrainingRunStatus.QUEUED,
    TrainingRunStatus.RUNNING,
    TrainingRunStatus.CANCELING,
}

_TRAINING_TERMINAL_STATES = {
    TrainingRunStatus.SUCCEEDED,
    TrainingRunStatus.FAILED,
    TrainingRunStatus.CANCELED,
    TrainingRunStatus.UNSUPPORTED,
}

_NEXT_PROMOTION_STAGE = {
    ModelLifecycleStage.REGISTERED: ModelLifecycleStage.OFFLINE_EVALUATION,
    ModelLifecycleStage.OFFLINE_EVALUATION: ModelLifecycleStage.SHADOW,
    ModelLifecycleStage.SHADOW: ModelLifecycleStage.CANARY,
    ModelLifecycleStage.CANARY: ModelLifecycleStage.PRODUCTION,
}


def _required_evaluation_stages(
    target: ModelLifecycleStage,
) -> tuple[ModelLifecycleStage, ...]:
    if target is ModelLifecycleStage.OFFLINE_EVALUATION:
        return (ModelLifecycleStage.OFFLINE_EVALUATION,)
    if target is ModelLifecycleStage.SHADOW:
        return (ModelLifecycleStage.OFFLINE_EVALUATION,)
    if target is ModelLifecycleStage.CANARY:
        return (
            ModelLifecycleStage.OFFLINE_EVALUATION,
            ModelLifecycleStage.SHADOW,
        )
    if target is ModelLifecycleStage.PRODUCTION:
        return (
            ModelLifecycleStage.OFFLINE_EVALUATION,
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
        )
    raise ResourceConflictError("Unsupported promotion target stage")


def _stable_id(namespace: str, *parts: str) -> str:
    return sha256("\0".join((namespace, *parts)).encode("utf-8")).hexdigest()
