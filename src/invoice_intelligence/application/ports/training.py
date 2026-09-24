"""Application boundaries for controlled hard-negative datasets."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from invoice_intelligence.domain.examples import ReviewedExample
from invoice_intelligence.domain.training import (
    HardNegativeCandidate,
    HardNegativeProposal,
    HardNegativeReview,
    HumanRetrievalJudgment,
    ModelArtifact,
    ModelDeployment,
    ModelEvaluation,
    ModelLifecycleStage,
    PromotionCandidateRecord,
    PromotionDecision,
    ProviderTrainingCapabilities,
    RemoteTrainingJob,
    TrainingDatasetRecord,
    TrainingDatasetVersion,
    TrainingRun,
    TrainingTargetType,
)
from invoice_intelligence.domain.workflow import JsonValue


@dataclass(frozen=True, slots=True)
class TrainingRedactionProfile:
    """Non-secret policy metadata exposed to the export use case."""

    strategy: Literal["none", "mask", "hash", "drop"]
    policy_version: str

    def __post_init__(self) -> None:
        if self.strategy not in {"none", "mask", "hash", "drop"}:
            raise ValueError("Unsupported training redaction strategy")
        if not self.policy_version.strip():
            raise ValueError("Training redaction policy version must not be empty")


@dataclass(frozen=True, slots=True)
class RedactedTrainingValue:
    """Distinguish an intentional drop from a legitimate JSON null label."""

    value: JsonValue
    was_dropped: bool


class TrainingDataRepository(Protocol):
    """PostgreSQL fact source for judgments, candidates, and dataset versions."""

    async def save_retrieval_judgment(self, judgment: HumanRetrievalJudgment) -> None:
        """Idempotently persist one explicit human retrieval rejection."""

        ...

    async def list_retrieval_judgments(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_judgment_id: str | None = None,
    ) -> tuple[HumanRetrievalJudgment, ...]:
        """Read valid judgments for exactly one tenant and Schema version."""

        ...

    async def upsert_candidate(
        self,
        candidate: HardNegativeCandidate,
    ) -> HardNegativeCandidate:
        """Merge a stable candidate fingerprint without increasing any weight."""

        ...

    async def get_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> HardNegativeCandidate | None:
        """Read a candidate without crossing tenant boundaries."""

        ...

    async def review_candidate(
        self,
        review: HardNegativeReview,
    ) -> HardNegativeCandidate:
        """Atomically apply the only allowed pending-to-final transition."""

        ...

    async def list_candidates(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        status: str,
        limit: int,
        after_candidate_id: str | None = None,
    ) -> tuple[HardNegativeCandidate, ...]:
        """Read valid candidates in stable pagination order."""

        ...

    async def create_dataset(
        self,
        dataset: TrainingDatasetVersion,
        records: Sequence[TrainingDatasetRecord],
    ) -> TrainingDatasetVersion:
        """Atomically freeze one dataset version and its auditable records."""

        ...

    async def finish_dataset(self, dataset: TrainingDatasetVersion) -> None:
        """Persist an exported or failed terminal outcome without changing contents."""

        ...

    async def get_dataset(
        self,
        tenant_scope: str,
        dataset_id: str,
        dataset_version: str,
    ) -> tuple[TrainingDatasetVersion, tuple[TrainingDatasetRecord, ...]] | None:
        """Read one exact immutable dataset version and its records."""

        ...


class TrainingDataRedactor(Protocol):
    """Apply tenant policy before data leaves PostgreSQL-owned application flow."""

    def training_profile(self, tenant_id: str) -> TrainingRedactionProfile:
        """Return non-secret policy metadata for one tenant."""

        ...

    def redact_training_value(
        self,
        tenant_id: str,
        field_path: str,
        value: JsonValue,
        *,
        force_irreversible: bool,
    ) -> RedactedTrainingValue:
        """Return a detached redacted value; never mutate the source case."""

        ...

    def redact_training_identifier(
        self,
        tenant_id: str,
        value: str,
        *,
        namespace: str,
    ) -> str:
        """Create an irreversible tenant-isolated export identifier."""

        ...


class TrainingDatasetExporter(Protocol):
    """Publish provider-neutral JSONL and a reviewable manifest."""

    async def export(
        self,
        dataset: TrainingDatasetVersion,
        records: Sequence[TrainingDatasetRecord],
    ) -> tuple[str, ...]:
        """Atomically replace only the exact artifacts for one frozen version."""

        ...


class FieldSimilarityProvider(Protocol):
    """Score field-name confusion; the score proposes and never labels a pair."""

    async def similarity(self, left_field_path: str, right_field_path: str) -> float:
        """Return a finite ranking score, not a probability."""

        ...


class HardNegativeProposalProvider(Protocol):
    """Optional model/rule candidate source with no authority to approve labels."""

    async def propose(
        self,
        examples: Sequence[ReviewedExample],
    ) -> tuple[HardNegativeProposal, ...]:
        """Return reference-only proposals for mandatory later human review."""

        ...


class RemoteTrainingProvider(Protocol):
    """Optional asynchronous training backend used only by an offline worker."""

    async def capabilities(self) -> ProviderTrainingCapabilities:
        """Return documented capabilities without inferring unsupported targets."""

        ...

    async def submit(
        self,
        run: TrainingRun,
        dataset: TrainingDatasetVersion,
        *,
        idempotency_key: str,
    ) -> RemoteTrainingJob:
        """Submit one exported dataset using a replay-safe operation identifier."""

        ...

    async def get_job(self, provider_job_id: str) -> RemoteTrainingJob:
        """Read one remote job snapshot without blocking or polling internally."""

        ...

    async def cancel(
        self,
        provider_job_id: str,
        *,
        idempotency_key: str,
    ) -> RemoteTrainingJob:
        """Request cancellation without affecting any active production model."""

        ...


class TrainingRunRepository(Protocol):
    """PostgreSQL fact source for monotonic offline training execution state."""

    async def save_training_run(self, run: TrainingRun) -> None:
        """Create or advance one run while preserving immutable version bindings."""

        ...

    async def get_training_run(
        self,
        tenant_id: str,
        training_run_id: str,
    ) -> TrainingRun | None:
        """Read a run for exactly one tenant."""

        ...


class ModelRegistryRepository(Protocol):
    """PostgreSQL registry and atomic promotion boundary for model lifecycle facts."""

    async def register_artifact(self, artifact: ModelArtifact) -> None:
        """Idempotently register a successful training artifact and model version."""

        ...

    async def get_artifact(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> ModelArtifact | None:
        """Read one tenant-owned artifact."""

        ...

    async def save_evaluation(self, evaluation: ModelEvaluation) -> None:
        """Persist a monotonic, version-bound offline/shadow/canary evaluation."""

        ...

    async def list_evaluations(
        self,
        tenant_id: str,
        artifact_id: str,
    ) -> tuple[ModelEvaluation, ...]:
        """Read all gate evidence for one tenant-owned artifact."""

        ...

    async def get_deployment(
        self,
        tenant_id: str,
        deployment_id: str,
    ) -> ModelDeployment | None:
        """Read one deployment without a cross-tenant fallback."""

        ...

    async def get_active_production(
        self,
        tenant_id: str,
        target_type: TrainingTargetType,
    ) -> ModelDeployment | None:
        """Return the active production deployment for one tenant and model target."""

        ...

    async def save_promotion_decision(self, decision: PromotionDecision) -> None:
        """Persist an idempotent rejected decision without changing deployment state."""

        ...

    async def get_promotion_decision(
        self,
        tenant_id: str,
        decision_id: str,
    ) -> PromotionDecision | None:
        """Read one decision for replay-safe promotion or rollback orchestration."""

        ...

    async def commit_promotion(
        self,
        artifact: ModelArtifact,
        decision: PromotionDecision,
        deployment: ModelDeployment | None,
    ) -> None:
        """Atomically advance artifact stage and persist decision/deployment facts."""

        ...

    async def commit_rollback(
        self,
        rolled_back_artifact: ModelArtifact,
        restored_artifact: ModelArtifact,
        decision: PromotionDecision,
        replacement_deployment: ModelDeployment,
    ) -> None:
        """Atomically record rollback and restore a previous production artifact."""

        ...


class PromotionCandidateRepository(Protocol):
    """PostgreSQL source of truth for candidate state and audit facts."""

    async def create_candidate(
        self,
        candidate: PromotionCandidateRecord,
        *,
        actor_id: str,
        trace_id: str | None,
    ) -> None: ...

    async def get_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> PromotionCandidateRecord | None: ...

    async def transition_candidate(
        self,
        candidate: PromotionCandidateRecord,
        *,
        expected_revision: int,
        audit_action: str,
        actor_id: str,
        trace_id: str | None,
    ) -> PromotionCandidateRecord: ...

    async def was_active(self, tenant_id: str, candidate_id: str) -> bool: ...

    async def rollback_candidate(
        self,
        current: PromotionCandidateRecord,
        target: PromotionCandidateRecord,
        *,
        expected_revision: int,
        expected_target_revision: int,
        actor_id: str,
        trace_id: str | None,
    ) -> PromotionCandidateRecord: ...


class ModelDeploymentController(Protocol):
    """External deployment control plane; it never chooses promotion eligibility."""

    async def deploy(
        self,
        artifact: ModelArtifact,
        *,
        stage: ModelLifecycleStage,
        traffic_percentage: float,
        operation_id: str,
        requested_by: str,
        previous_deployment: ModelDeployment | None,
    ) -> ModelDeployment:
        """Apply an already-approved shadow, canary, or production deployment."""

        ...

    async def rollback(
        self,
        current: ModelDeployment,
        target: ModelDeployment,
        *,
        operation_id: str,
        requested_by: str,
    ) -> ModelDeployment:
        """Restore a known prior deployment using an idempotent operation."""

        ...
