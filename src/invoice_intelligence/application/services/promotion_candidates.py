"""从可信 PostgreSQL 证据执行确定性模型候选晋升和回滚。"""

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from invoice_intelligence.application.errors import (
    BadRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.ports.promotion_evidence import (
    PromotionEvidence,
    PromotionEvidenceRepository,
)
from invoice_intelligence.application.ports.training import PromotionCandidateRepository
from invoice_intelligence.domain.training import PromotionCandidateRecord, PromotionCandidateStatus

_SAFE_REASON = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class PromotionPolicy:
    """硬失败、版本不兼容和缺失指标优先于数值门禁。"""

    def __init__(
        self,
        *,
        automatic_promotion_enabled: bool = False,
        required_metrics: dict[str, float] | None = None,
    ) -> None:
        self.automatic_promotion_enabled = automatic_promotion_enabled
        self.required_metrics = dict(required_metrics or {})

    def gate(
        self,
        *,
        metrics: dict[str, float],
        hard_failure_code: str | None,
        compatibility_errors: tuple[str, ...],
    ) -> tuple[bool, str | None]:
        if hard_failure_code:
            return False, hard_failure_code
        if compatibility_errors:
            return False, compatibility_errors[0]
        if not metrics:
            return False, "METRIC_GATE_MISSING"
        for name, minimum in self.required_metrics.items():
            value = metrics.get(name)
            if value is None or value < minimum:
                return False, f"METRIC_GATE_FAILED:{name}"
        return True, None


class PromotionCandidateService:
    def __init__(
        self,
        *,
        repository: PromotionCandidateRepository,
        evidence_repository: PromotionEvidenceRepository,
        policy: PromotionPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._evidence = evidence_repository
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        evaluation_run_id: str,
        artifact_id: str,
        actor_id: str,
        trace_id: str | None,
    ) -> PromotionCandidateRecord:
        evidence = await self._evidence.load(
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            evaluation_run_id=evaluation_run_id,
            artifact_id=artifact_id,
        )
        passed, reason = self._gate(evidence)
        now = self._now()
        candidate = PromotionCandidateRecord(
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            dataset_version=evidence.dataset_version,
            evaluation_run_id=evaluation_run_id,
            model_version=evidence.model_version,
            prompt_version=evidence.prompt_version,
            schema_version=evidence.schema_version,
            index_version=evidence.index_version,
            threshold_version=evidence.threshold_version,
            status=(
                PromotionCandidateStatus.SHADOW if passed else PromotionCandidateStatus.REJECTED
            ),
            revision=1,
            metric_values=evidence.metric_values,
            hard_failure_code=evidence.hard_failure_code,
            compatibility_errors=evidence.compatibility_errors,
            created_at=now,
            updated_at=now,
            rejection_reason=None if passed else reason,
            artifact_id=artifact_id,
            model_evaluation_id=evidence.model_evaluation_id,
        )
        await self._repository.create_candidate(
            candidate, actor_id=actor_id, trace_id=trace_id
        )
        return candidate

    async def approve(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        target_status: PromotionCandidateStatus,
    ) -> PromotionCandidateRecord:
        if target_status not in {
            PromotionCandidateStatus.SHADOW,
            PromotionCandidateStatus.CANARY,
            PromotionCandidateStatus.ACTIVE,
        }:
            raise ResourceConflictError("Approval target is invalid")
        current = await self._required_candidate(tenant_id, candidate_id)
        if current.revision != expected_revision:
            raise ResourceConflictError("Promotion candidate revision changed")
        evidence = await self._required_evidence(current)
        passed, _ = self._gate(evidence)
        if not passed or not self._matches_snapshot(current, evidence):
            raise ResourceConflictError("Trusted promotion gate blocks approval")
        allowed = {
            PromotionCandidateStatus.SHADOW: {
                PromotionCandidateStatus.SHADOW,
                PromotionCandidateStatus.CANARY,
            },
            PromotionCandidateStatus.CANARY: {
                PromotionCandidateStatus.CANARY,
                PromotionCandidateStatus.ACTIVE,
            },
            PromotionCandidateStatus.ACTIVE: {PromotionCandidateStatus.ACTIVE},
        }
        if target_status not in allowed.get(current.status, set()):
            raise ResourceConflictError("Promotion lifecycle transition is invalid")
        updated = replace(
            current,
            status=target_status,
            revision=current.revision + 1,
            approved_by=actor_id,
            rejection_reason=None,
            updated_at=self._now(),
        )
        return await self._repository.transition_candidate(
            updated,
            expected_revision=expected_revision,
            audit_action="promotion_approved",
            actor_id=actor_id,
            trace_id=trace_id,
        )

    async def reject(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        expected_revision: int,
        actor_id: str,
        reason: str,
        trace_id: str | None,
    ) -> PromotionCandidateRecord:
        if not _SAFE_REASON.fullmatch(reason):
            raise BadRequestError("Promotion rejection reason must be a safe code")
        current = await self._required_candidate(tenant_id, candidate_id)
        if current.revision != expected_revision:
            raise ResourceConflictError("Promotion candidate revision changed")
        updated = replace(
            current,
            status=PromotionCandidateStatus.REJECTED,
            revision=current.revision + 1,
            rejection_reason=reason,
            updated_at=self._now(),
        )
        return await self._repository.transition_candidate(
            updated,
            expected_revision=expected_revision,
            audit_action="promotion_rejected",
            actor_id=actor_id,
            trace_id=trace_id,
        )

    async def rollback(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        target_candidate_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
    ) -> PromotionCandidateRecord:
        if candidate_id == target_candidate_id:
            raise ResourceConflictError("Rollback target must be historical")
        current = await self._required_candidate(tenant_id, candidate_id)
        target = await self._required_candidate(tenant_id, target_candidate_id)
        if (
            current.artifact_id is None
            or target.artifact_id is None
            or current.artifact_id == target.artifact_id
            or current.model_version == target.model_version
        ):
            raise ResourceConflictError("Rollback requires a different historical model version")
        if current.revision != expected_revision:
            raise ResourceConflictError("Promotion candidate revision changed")
        if current.status is not PromotionCandidateStatus.ACTIVE:
            raise ResourceConflictError("Only an active candidate can be rolled back")
        if (
            target.status is not PromotionCandidateStatus.ROLLBACK
            or not await self._repository.was_active(tenant_id, target_candidate_id)
        ):
            raise ResourceConflictError("Rollback target was not previously active")
        evidence = await self._required_evidence(target)
        passed, _ = self._gate(evidence)
        if not passed or not self._matches_snapshot(target, evidence):
            raise ResourceConflictError("Rollback target is no longer valid")
        now = self._now()
        rolled_back = replace(
            current,
            status=PromotionCandidateStatus.ROLLBACK,
            revision=current.revision + 1,
            approved_by=actor_id,
            updated_at=now,
        )
        restored = replace(
            target,
            status=PromotionCandidateStatus.ACTIVE,
            revision=target.revision + 1,
            approved_by=actor_id,
            updated_at=now,
        )
        return await self._repository.rollback_candidate(
            rolled_back,
            restored,
            expected_revision=expected_revision,
            expected_target_revision=target.revision,
            actor_id=actor_id,
            trace_id=trace_id,
        )

    async def _required_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> PromotionCandidateRecord:
        candidate = await self._repository.get_candidate(tenant_id, candidate_id)
        if candidate is None:
            raise ResourceNotFoundError("Promotion candidate was not found")
        return candidate

    async def _required_evidence(
        self, candidate: PromotionCandidateRecord
    ) -> PromotionEvidence:
        if candidate.artifact_id is None or candidate.model_evaluation_id is None:
            raise ResourceConflictError("Legacy candidate lacks trusted evidence references")
        return await self._evidence.load(
            tenant_id=candidate.tenant_id,
            candidate_id=candidate.candidate_id,
            evaluation_run_id=candidate.evaluation_run_id,
            artifact_id=candidate.artifact_id,
        )

    def _gate(self, evidence: PromotionEvidence) -> tuple[bool, str | None]:
        return self._policy.gate(
            metrics=evidence.metric_values,
            hard_failure_code=evidence.hard_failure_code,
            compatibility_errors=evidence.compatibility_errors,
        )

    @staticmethod
    def _matches_snapshot(
        candidate: PromotionCandidateRecord, evidence: PromotionEvidence
    ) -> bool:
        return (
            candidate.artifact_id == evidence.artifact_id
            and candidate.model_evaluation_id == evidence.model_evaluation_id
            and candidate.dataset_version == evidence.dataset_version
            and candidate.model_version == evidence.model_version
            and candidate.prompt_version == evidence.prompt_version
            and candidate.schema_version == evidence.schema_version
            and candidate.index_version == evidence.index_version
            and candidate.threshold_version == evidence.threshold_version
            and candidate.metric_values == evidence.metric_values
            and candidate.hard_failure_code == evidence.hard_failure_code
            and candidate.compatibility_errors == evidence.compatibility_errors
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Promotion clock must be timezone-aware")
        return value
