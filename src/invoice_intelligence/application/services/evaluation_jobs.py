"""Application service for isolated, deterministic offline evaluations."""

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from invoice_intelligence.application.errors import (
    BadRequestError,
    ForbiddenError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from invoice_intelligence.application.ports.evaluation import EvaluationDatasetRepository
from invoice_intelligence.application.ports.evaluation_jobs import EvaluationJobRepository
from invoice_intelligence.application.ports.memory_gold import GoldAnnotationRepository
from invoice_intelligence.application.services.memory_benefit_batch import (
    MemoryBenefitBatchService,
    frozen_dataset_digest,
)
from invoice_intelligence.application.services.offline_evaluation import (
    OfflineEvaluationRequest,
    OfflineEvaluationService,
)
from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
)
from invoice_intelligence.domain.evaluation_jobs import (
    DatasetSnapshot,
    EvaluationJob,
    EvaluationJobStatus,
    EvaluationSchedule,
    SnapshotCase,
    calculate_stub_metrics,
)
from invoice_intelligence.domain.examples import (
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.domain.governance import MemoryPermission, TrustedTenantContext


@dataclass(frozen=True, slots=True)
class CreateSnapshotCommand:
    dataset_version: str
    schema_version: str
    cases: tuple[SnapshotCase, ...]


@dataclass(frozen=True, slots=True)
class CreateEvaluationJobCommand:
    snapshot_id: str
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    threshold_version: str


@dataclass(frozen=True, slots=True)
class CreateSuiteEvaluationJobCommand:
    dataset_id: str
    dataset_version: str
    suite: EvaluationSuite
    index_version: str
    model_version: str
    prompt_version: str
    retrieval_policy_version: str
    threshold_version: str
    catalog_version: str | None = None
    admission_policy_version: str | None = None
    field_binding_policy_version: str | None = None


@dataclass(frozen=True, slots=True)
class CreateMemoryBenefitJobCommand:
    document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateEvaluationScheduleCommand:
    snapshot_id: str
    index_version: str
    model_version: str
    prompt_version: str
    threshold_version: str
    interval_seconds: int


class EvaluationJobService:
    def __init__(
        self,
        repository: EvaluationJobRepository,
        *,
        dataset_repository: EvaluationDatasetRepository | None = None,
        suite_evaluation: OfflineEvaluationService | None = None,
        gold_repository: GoldAnnotationRepository | None = None,
        memory_benefit_batch: MemoryBenefitBatchService | None = None,
        memory_benefit_threshold_version: str = "memory-benefit-gate-v1",
        suite_run_timeout_seconds: float = 3600,
        max_attempts: int = 3,
    ) -> None:
        if not math.isfinite(suite_run_timeout_seconds) or suite_run_timeout_seconds <= 0:
            raise ValueError("Suite evaluation timeout must be positive")
        self._repository = repository
        self._datasets = dataset_repository
        self._suite_evaluation = suite_evaluation
        self._gold_repository = gold_repository
        self._memory_benefit_batch = memory_benefit_batch
        self._memory_benefit_threshold_version = memory_benefit_threshold_version
        self._suite_run_timeout_seconds = suite_run_timeout_seconds
        self._max_attempts = max_attempts

    async def create_snapshot(
        self, context: TrustedTenantContext, command: CreateSnapshotCommand,
        *, idempotency_key: str,
    ) -> DatasetSnapshot:
        if not command.cases:
            raise BadRequestError("Evaluation snapshot requires at least one case")
        payload = [_case_payload(case) for case in command.cases]
        digest = sha256(_canonical(payload).encode()).hexdigest()
        snapshot = DatasetSnapshot(
            snapshot_id=sha256(
                f"snapshot:{context.tenant_id}:{idempotency_key}".encode()
            ).hexdigest(),
            tenant_id=context.tenant_id,
            dataset_version=command.dataset_version,
            schema_version=command.schema_version,
            cases=command.cases,
            content_sha256=digest,
            created_at=datetime.now(UTC),
        )
        return await self._repository.add_snapshot(snapshot)

    async def get_snapshot(self, context: TrustedTenantContext, snapshot_id: str) -> DatasetSnapshot:
        snapshot = await self._repository.get_snapshot(context.tenant_id, snapshot_id)
        if snapshot is None:
            raise ResourceNotFoundError("Evaluation snapshot was not found")
        return snapshot

    async def create_job(
        self, context: TrustedTenantContext, command: CreateEvaluationJobCommand,
        *, idempotency_key: str | None,
    ) -> EvaluationJob:
        snapshot = await self.get_snapshot(context, command.snapshot_id)
        if (snapshot.dataset_version, snapshot.schema_version) != (
            command.dataset_version, command.schema_version
        ):
            raise BadRequestError("Evaluation job bindings do not match snapshot")
        request_hash = sha256(_canonical(asdict(command)).encode()).hexdigest()
        key_hash = sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
        now = datetime.now(UTC)
        job = EvaluationJob(
            job_id=uuid4().hex,
            tenant_id=context.tenant_id,
            snapshot_id=snapshot.snapshot_id,
            dataset_version=command.dataset_version,
            schema_version=command.schema_version,
            index_version=command.index_version,
            model_version=command.model_version,
            prompt_version=command.prompt_version,
            threshold_version=command.threshold_version,
            status=EvaluationJobStatus.PENDING,
            attempt_count=0,
            next_attempt_at=None,
            lease_expires_at=None,
            worker_id=None,
            lease_token=None,
            failure_code=None,
            report=None,
            created_at=now,
            updated_at=now,
        )
        return await self._repository.create_job(job, request_hash, key_hash)

    async def get_job(self, context: TrustedTenantContext, job_id: str) -> EvaluationJob:
        job = await self._repository.get_job(context.tenant_id, job_id)
        if job is None:
            raise ResourceNotFoundError("Evaluation job was not found")
        return job

    async def create_suite_job(
        self,
        context: TrustedTenantContext,
        command: CreateSuiteEvaluationJobCommand,
        *,
        idempotency_key: str,
    ) -> EvaluationJob:
        if self._datasets is None:
            raise ServiceUnavailableError("Evaluation dataset repository is unavailable")
        dataset = await self._datasets.get_dataset(
            context.tenant_id, command.dataset_id, command.dataset_version
        )
        if dataset is None:
            raise ResourceNotFoundError("Evaluation dataset was not found")
        if not dataset.is_frozen:
            raise ResourceConflictError("Evaluation dataset must be frozen")
        if command.suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING and not all((
            any(case.memory_admission_ground_truth is not None for case in dataset.cases),
            any(case.field_binding_ground_truth is not None for case in dataset.cases),
            any(case.expected_memory_effects for case in dataset.cases),
        )):
            raise BadRequestError("Evaluation dataset lacks Suite ground truth")
        required_versions = (
            command.index_version, command.model_version, command.prompt_version,
            command.retrieval_policy_version, command.threshold_version,
        )
        if any(not version.strip() for version in required_versions):
            raise BadRequestError("Evaluation job versions are required")
        if command.suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING and any(
            not value or not value.strip() for value in (
                command.catalog_version,
                command.admission_policy_version,
                command.field_binding_policy_version,
            )
        ):
            raise BadRequestError("Trusted-memory Suite policy versions are required")
        now = datetime.now(UTC)
        job = EvaluationJob(
            job_id=uuid4().hex,
            tenant_id=context.tenant_id,
            snapshot_id=None,
            dataset_version=dataset.version,
            schema_version=dataset.schema_version,
            index_version=command.index_version,
            model_version=command.model_version,
            prompt_version=command.prompt_version,
            threshold_version=command.threshold_version,
            status=EvaluationJobStatus.PENDING,
            attempt_count=0,
            next_attempt_at=None,
            lease_expires_at=None,
            worker_id=None,
            lease_token=None,
            failure_code=None,
            report=None,
            created_at=now,
            updated_at=now,
            evidence_class="suite_run",
            dataset_id=dataset.dataset_id,
            suite=command.suite,
            retrieval_policy_version=command.retrieval_policy_version,
            catalog_version=command.catalog_version,
            admission_policy_version=command.admission_policy_version,
            field_binding_policy_version=command.field_binding_policy_version,
        )
        request_hash = sha256(_canonical(asdict(command)).encode()).hexdigest()
        key_hash = sha256(idempotency_key.encode()).hexdigest()
        return await self._repository.create_job(job, request_hash, key_hash)

    async def create_memory_benefit_job(
        self, context: TrustedTenantContext, command: CreateMemoryBenefitJobCommand,
        *, idempotency_key: str,
    ) -> EvaluationJob:
        if not context.permits(MemoryPermission.READ_EVALUATION):
            raise ForbiddenError("Evaluation permission is required")
        if self._gold_repository is None or self._memory_benefit_batch is None:
            raise ServiceUnavailableError("Paired gold evaluation is unavailable")
        if not command.document_ids or len(command.document_ids) > 200 or (
            len(command.document_ids) != len(set(command.document_ids))
        ):
            raise BadRequestError("Benefit Job requires 1 to 200 distinct documents")
        cases = []
        for document_id in command.document_ids:
            case = await self._gold_repository.get_case(context.tenant_id, document_id)
            if case is None:
                raise ResourceNotFoundError("Frozen gold case was not found")
            if case.status != "frozen" or not case.gold_checksum:
                raise ResourceConflictError("Gold case is not frozen")
            cases.append(case)
        if len({case.document_checksum for case in cases}) != len(cases):
            raise ResourceConflictError("Benefit dataset repeats source bytes")
        versions = cases[0].versions
        if any(case.versions != versions for case in cases):
            raise ResourceConflictError("Benefit dataset has mixed runtime versions")
        digest = frozen_dataset_digest(cases)
        now = datetime.now(UTC)
        job = EvaluationJob(
            job_id=uuid4().hex, tenant_id=context.tenant_id,
            snapshot_id=None, dataset_id=f"gold-{digest}",
            dataset_version=digest,
            schema_version=versions["schema_version"],
            index_version=versions["index_version"],
            model_version=versions["model_version"],
            prompt_version=versions["prompt_version"],
            catalog_version=versions["catalog_version"],
            threshold_version=self._memory_benefit_threshold_version,
            status=EvaluationJobStatus.PENDING, attempt_count=0,
            next_attempt_at=None, lease_expires_at=None, worker_id=None,
            lease_token=None, failure_code=None, report=None,
            created_at=now, updated_at=now,
            evidence_class="memory_benefit",
        )
        request_hash = sha256(_canonical({
            "document_ids": sorted(command.document_ids), "dataset_digest": digest,
        }).encode()).hexdigest()
        return await self._repository.create_memory_benefit_job(
            job, request_hash, sha256(idempotency_key.encode()).hexdigest(),
            command.document_ids,
        )

    async def list_jobs(self, context: TrustedTenantContext, *, limit: int, offset: int) -> tuple[EvaluationJob, ...]:
        return await self._repository.list_jobs(context.tenant_id, limit, offset)

    async def create_schedule(
        self, context: TrustedTenantContext, command: CreateEvaluationScheduleCommand,
        *, idempotency_key: str,
    ) -> EvaluationSchedule:
        await self.get_snapshot(context, command.snapshot_id)
        if command.interval_seconds < 3600:
            raise BadRequestError("Evaluation schedule interval must be at least one hour")
        schedule = EvaluationSchedule(
            schedule_id=sha256(
                f"schedule:{context.tenant_id}:{idempotency_key}".encode()
            ).hexdigest(),
            tenant_id=context.tenant_id,
            snapshot_id=command.snapshot_id,
            index_version=command.index_version,
            model_version=command.model_version,
            prompt_version=command.prompt_version,
            threshold_version=command.threshold_version,
            interval_seconds=command.interval_seconds,
            next_run_at=datetime.now(UTC),
            enabled=True,
        )
        return await self._repository.add_schedule(schedule)

    async def list_schedules(self, context: TrustedTenantContext) -> tuple[EvaluationSchedule, ...]:
        return await self._repository.list_schedules(context.tenant_id)

    async def disable_schedule(self, context: TrustedTenantContext, schedule_id: str) -> None:
        if not await self._repository.disable_schedule(context.tenant_id, schedule_id):
            raise ResourceNotFoundError("Evaluation schedule was not found")

    async def execute_one(
        self, worker_id: str, *, lease_seconds: float = 300
    ) -> EvaluationJob | None:
        job = await self._repository.claim(worker_id, lease_seconds, self._max_attempts)
        if job is None:
            return None
        if job.evidence_class == "suite_run":
            await self._execute_suite_job(job, lease_seconds)
            return job
        if job.evidence_class == "memory_benefit":
            await self._execute_memory_benefit_job(job, lease_seconds)
            return job
        try:
            if job.snapshot_id is None:
                raise ValueError("Diagnostic evaluation job has no Snapshot")
            snapshot = await self._repository.get_snapshot(job.tenant_id, job.snapshot_id)
            if snapshot is None:
                raise ServiceUnavailableError("Evaluation snapshot is unavailable")
            report: dict[str, object] = dict(calculate_stub_metrics(snapshot.cases))
            report["variant"] = "deterministic_stub"
            report["evidence_class"] = "diagnostic_only"
            await self._repository.complete(job, report)
        except ResourceConflictError:
            # Lease fencing: a late worker must not mutate a newer attempt.
            return job
        except ServiceUnavailableError:
            await self._repository.fail(job, "snapshot.unavailable", retry=True, max_attempts=self._max_attempts)
        except (ValueError, TypeError):
            await self._repository.fail(job, "evaluation.invalid_snapshot", retry=False, max_attempts=self._max_attempts)
        except Exception:
            await self._repository.fail(job, "evaluation.provider_failure", retry=True, max_attempts=self._max_attempts)
        return job

    async def _execute_suite_job(self, job: EvaluationJob, lease_seconds: float) -> None:
        if self._suite_evaluation is None:
            await self._repository.fail(
                job, "evaluation.runner_unavailable", retry=False,
                max_attempts=self._max_attempts,
            )
            return
        try:
            if job.dataset_id is None or job.suite is None or job.retrieval_policy_version is None:
                raise ValueError("Suite Job bindings are incomplete")
            request = OfflineEvaluationRequest(
                evaluation_run_id=sha256(
                    f"suite-job:{job.job_id}:{job.attempt_count}".encode()
                ).hexdigest(),
                tenant_id=job.tenant_id,
                dataset_id=job.dataset_id,
                dataset_version=job.dataset_version,
                suite=job.suite,
                bindings=EvaluationBindings(
                    index_version=IndexVersion(job.index_version),
                    model_version=ModelVersion(job.model_version),
                    prompt_version=PromptVersion(job.prompt_version),
                    retrieval_policy_version=RetrievalPolicyVersion(
                        job.retrieval_policy_version
                    ),
                    threshold_version=job.threshold_version,
                    catalog_version=job.catalog_version,
                    admission_policy_version=job.admission_policy_version,
                    field_binding_policy_version=job.field_binding_policy_version,
                ),
            )
            run = await self._evaluate_with_lease(job, request, lease_seconds)
            if run.status is not EvaluationRunStatus.COMPLETED or not run.artifact_references:
                raise ValueError("Suite Run has no completed report")
            await self._repository.complete_suite(job, run.evaluation_run_id)
        except ResourceConflictError:
            await self._fail_suite_if_owned(job, "evaluation.run_conflict", retry=False)
        except (ValueError, TypeError):
            await self._fail_suite_if_owned(job, "evaluation.invalid_suite_evidence", retry=False)
        except TimeoutError:
            await self._fail_suite_if_owned(job, "evaluation.runner_timeout", retry=True)
        except Exception:
            await self._fail_suite_if_owned(job, "evaluation.runner_failure", retry=True)

    async def _execute_memory_benefit_job(
        self, job: EvaluationJob, lease_seconds: float
    ) -> None:
        if self._memory_benefit_batch is None:
            await self._fail_suite_if_owned(job, "evaluation.gold_unavailable", retry=False)
            return
        try:
            document_ids = await self._repository.get_memory_benefit_documents(
                job.tenant_id, job.job_id
            )
            if not document_ids:
                raise ValueError("Benefit Job has no frozen source documents")
            stop = asyncio.Event()
            renewal = asyncio.create_task(self._renew_suite_lease(job, lease_seconds, stop))
            evaluation = asyncio.create_task(asyncio.wait_for(
                self._memory_benefit_batch.run(
                    job.tenant_id, job.job_id, document_ids, job.dataset_version
                ), timeout=self._suite_run_timeout_seconds,
            ))
            try:
                done, _ = await asyncio.wait(
                    {evaluation, renewal}, return_when=asyncio.FIRST_COMPLETED
                )
                if renewal in done:
                    evaluation.cancel()
                    await asyncio.gather(evaluation, return_exceptions=True)
                    raise ResourceConflictError("Benefit Job lease was lost")
                result = await evaluation
            finally:
                if not evaluation.done():
                    evaluation.cancel()
                    await asyncio.gather(evaluation, return_exceptions=True)
                stop.set()
                if renewal.done():
                    renewal.result()
                else:
                    await renewal
            await self._repository.complete_memory_benefit(job, result.run_id)
        except ResourceConflictError:
            await self._fail_suite_if_owned(job, "evaluation.benefit_conflict", retry=False)
        except (ValueError, TypeError, ResourceNotFoundError):
            await self._fail_suite_if_owned(job, "evaluation.invalid_gold", retry=False)
        except TimeoutError:
            await self._fail_suite_if_owned(job, "evaluation.benefit_timeout", retry=True)
        except Exception:
            await self._fail_suite_if_owned(job, "evaluation.benefit_provider_failure", retry=True)

    async def _evaluate_with_lease(
        self,
        job: EvaluationJob,
        request: OfflineEvaluationRequest,
        lease_seconds: float,
    ) -> EvaluationRun:
        assert self._suite_evaluation is not None
        stop = asyncio.Event()
        renewal = asyncio.create_task(self._renew_suite_lease(job, lease_seconds, stop))
        evaluation = asyncio.create_task(asyncio.wait_for(
            self._suite_evaluation.evaluate(request),
            timeout=self._suite_run_timeout_seconds,
        ))
        try:
            done, _ = await asyncio.wait(
                {evaluation, renewal}, return_when=asyncio.FIRST_COMPLETED
            )
            if renewal in done:
                evaluation.cancel()
                await asyncio.gather(evaluation, return_exceptions=True)
                raise ResourceConflictError("Suite Job lease was lost")
            return await evaluation
        finally:
            if not evaluation.done():
                evaluation.cancel()
                await asyncio.gather(evaluation, return_exceptions=True)
            stop.set()
            if renewal.done():
                try:
                    renewal.result()
                except Exception:
                    pass
            else:
                await renewal

    async def _renew_suite_lease(
        self, job: EvaluationJob, lease_seconds: float, stop: asyncio.Event
    ) -> None:
        interval = max(0.01, lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                await self._repository.renew(job, lease_seconds)

    async def _fail_suite_if_owned(
        self, job: EvaluationJob, code: str, *, retry: bool
    ) -> None:
        try:
            await self._repository.fail(
                job, code, retry=retry, max_attempts=self._max_attempts
            )
        except ResourceConflictError:
            # A reclaimed lease belongs to another Worker.
            pass

    async def recover_expired(self) -> int:
        return await self._repository.recover_expired(self._max_attempts)

    async def enqueue_due(self, *, limit: int = 50) -> int:
        return await self._repository.enqueue_due(datetime.now(UTC), limit)


def _case_payload(case: SnapshotCase) -> dict[str, object]:
    return {
        "case_id": case.case_id, "field_path": case.field_path,
        "expected_present": case.expected_present, "predicted_present": case.predicted_present,
        "field_correct": case.field_correct, "evidence_covered": case.evidence_covered,
        "amount_absolute_error": case.amount_absolute_error, "review_required": case.review_required,
        "negative_false_recall": case.negative_false_recall,
        "ocr_vision_consistent": case.ocr_vision_consistent,
    }


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
