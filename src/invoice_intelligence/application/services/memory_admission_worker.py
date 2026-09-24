"""PostgreSQL-leased Worker orchestration for reviewed-memory admission."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Generic, TypeVar
from uuid import uuid4

from invoice_intelligence.application.errors import (
    DocumentIntegrityError,
    InvalidDocumentError,
    RemoteInferenceError,
    RemoteInferenceRequestError,
    StorageError,
    WorkflowError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.admission import (
    MemoryAdmissionRepository,
    MemoryAdmissionWorkLease,
)
from invoice_intelligence.application.ports.business_persistence import (
    BusinessQueryRepository,
)
from invoice_intelligence.application.ports.document_repository import (
    DocumentReferenceRepository,
)
from invoice_intelligence.application.ports.examples import ReviewedExampleRepository
from invoice_intelligence.application.ports.memory import (
    CorrectionEventRepository,
    MemoryRecoveryRepository,
    MemoryRecoveryWorkLease,
)
from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.application.ports.workflow import ExtractionStateCodec
from invoice_intelligence.application.services.memory_admission import (
    MemoryAdmissionService,
)
from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.examples import ReviewedExample

InvoiceT = TypeVar("InvoiceT")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryAdmissionWorkerConfig:
    worker_id: str
    poll_interval_seconds: float
    batch_size: int
    max_concurrency: int
    lease_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or self.worker_id != self.worker_id.strip():
            raise ValueError("worker_id must be non-empty and normalized")
        if len(self.worker_id) > 128:
            raise ValueError("worker_id exceeds the persisted limit")
        if any(
            value <= 0
            for value in (
                self.poll_interval_seconds,
                self.lease_seconds,
                self.backoff_base_seconds,
                self.backoff_max_seconds,
            )
        ):
            raise ValueError("Worker timing settings must be positive")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("Worker maximum backoff cannot be lower than its base")
        if self.batch_size <= 0 or self.max_concurrency <= 0 or self.max_attempts <= 0:
            raise ValueError("Worker limits must be positive")
        if self.max_concurrency > self.batch_size:
            raise ValueError("Worker concurrency cannot exceed batch size")


class _AdmissionWorkError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _RetryableAdmissionWorkError(_AdmissionWorkError):
    pass


class _PermanentAdmissionWorkError(_AdmissionWorkError):
    pass


class MemoryAdmissionWorker(Generic[InvoiceT]):
    """Claim durable admission jobs and invoke the existing application service."""

    def __init__(
        self,
        *,
        config: MemoryAdmissionWorkerConfig,
        admission_repository: MemoryAdmissionRepository,
        recovery_repository: MemoryRecoveryRepository[InvoiceT],
        admission_service: MemoryAdmissionService[InvoiceT],
        example_repository: ReviewedExampleRepository,
        correction_event_repository: CorrectionEventRepository,
        document_repository: DocumentReferenceRepository,
        business_query_repository: BusinessQueryRepository,
        extraction_codec: ExtractionStateCodec,
        output_schema: type[InvoiceT],
        privacy_telemetry: PrivacyTelemetry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._admissions = admission_repository
        self._recoveries = recovery_repository
        self._service = admission_service
        self._examples = example_repository
        self._correction_events = correction_event_repository
        self._documents = document_repository
        self._business = business_query_repository
        self._codec = extraction_codec
        self._output_schema = output_schema
        self._clock = clock or (lambda: datetime.now(UTC))
        self._privacy_telemetry = privacy_telemetry

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run until signalled, finishing the currently claimed batch before exit."""

        _LOGGER.info(
            "memory_admission_worker_started",
            extra={"worker_id": self._config.worker_id},
        )
        try:
            while not stop_event.is_set():
                try:
                    claimed = await self.run_once()
                except Exception as exc:
                    _LOGGER.error(
                        "memory_admission_worker_poll_failed",
                        extra={
                            "worker_id": self._config.worker_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    claimed = 0
                if claimed == 0:
                    await self._wait_for_stop(stop_event)
        finally:
            _LOGGER.info(
                "memory_admission_worker_stopped",
                extra={"worker_id": self._config.worker_id},
            )

    async def run_once(self) -> int:
        now = self._now()
        capacity = min(self._config.batch_size, self._config.max_concurrency)
        recovery_leases = await self._recoveries.claim_due_memory_recoveries(
            self._config.worker_id,
            now=now,
            lease_expires_at=now + timedelta(seconds=self._config.lease_seconds),
            limit=capacity,
        )
        remaining = capacity - len(recovery_leases)
        leases = (
            await self._admissions.claim_due(
                self._config.worker_id,
                now=now,
                lease_expires_at=now + timedelta(seconds=self._config.lease_seconds),
                limit=remaining,
            )
            if remaining > 0
            else ()
        )
        if not recovery_leases and not leases:
            return 0
        semaphore = asyncio.Semaphore(self._config.max_concurrency)

        async def process(lease: MemoryAdmissionWorkLease) -> None:
            async with semaphore:
                if self._privacy_telemetry is None:
                    await self._process_lease(lease)
                    return
                trace_id = uuid4().hex
                with self._privacy_telemetry.span(
                    trace_id=trace_id,
                    tenant_id=lease.tenant_id,
                    stage=TraceStage.BACKGROUND_RECOVERY,
                    operation="process_memory_admission_lease",
                    attributes={
                        "worker_id": lease.worker_id,
                        "example_id": lease.example_id,
                        "attempt_count": lease.attempt_count,
                    },
                ):
                    with self._privacy_telemetry.span(
                        trace_id=trace_id,
                        tenant_id=lease.tenant_id,
                        stage=TraceStage.MEMORY_ADMISSION,
                        operation="evaluate_memory_admission",
                        attributes={
                            "worker_id": lease.worker_id,
                            "example_id": lease.example_id,
                            "attempt_count": lease.attempt_count,
                        },
                    ):
                        await self._process_lease(lease)

        async def recover(lease: MemoryRecoveryWorkLease) -> None:
            async with semaphore:
                if self._privacy_telemetry is None:
                    await self._process_recovery_lease(lease)
                    return
                with self._privacy_telemetry.span(
                    trace_id=lease.record.trace_id,
                    tenant_id=lease.record.tenant_id,
                    stage=TraceStage.BACKGROUND_RECOVERY,
                    operation="materialize_review_recovery",
                    attributes={
                        "worker_id": lease.worker_id,
                        "recovery_id": lease.record.recovery_id,
                        "run_id": lease.record.identity.run_id,
                        "attempt_count": lease.record.attempt_count,
                    },
                ):
                    await self._process_recovery_lease(lease)

        await asyncio.gather(
            *(recover(lease) for lease in recovery_leases),
            *(process(lease) for lease in leases),
        )
        return len(recovery_leases) + len(leases)

    async def _process_recovery_lease(
        self,
        lease: MemoryRecoveryWorkLease,
    ) -> None:
        record = lease.record
        try:
            stored_result = await self._business.get_result(
                record.identity.run_id,
                record.tenant_id,
            )
            if stored_result is None:
                raise _RetryableAdmissionWorkError("extraction_result_not_ready")
            if stored_result.document_id != record.identity.document_id:
                raise _PermanentAdmissionWorkError("result_scope_invalid")
            outcome = await self._service.materialize_claimed_review_candidates(lease)
            _LOGGER.info(
                "memory_review_recovery_completed",
                extra={
                    "worker_id": lease.worker_id,
                    "tenant_id": record.tenant_id,
                    "run_id": record.identity.run_id,
                    "recovery_id": record.recovery_id,
                    "trace_id": record.trace_id,
                    "candidate_count": len(outcome.example_ids),
                },
            )
        except _PermanentAdmissionWorkError as exc:
            await self._retry_recovery(
                lease,
                f"permanent.{exc.code}",
                terminal=True,
            )
        except _RetryableAdmissionWorkError as exc:
            await self._retry_recovery(lease, exc.code)
        except RemoteInferenceRequestError as exc:
            await self._retry_recovery(
                lease,
                f"permanent.{type(exc).__name__.lower()}",
                terminal=True,
            )
        except (RemoteInferenceError, StorageError, WorkflowPersistenceError) as exc:
            await self._retry_recovery(lease, type(exc).__name__.lower())
        except (DocumentIntegrityError, InvalidDocumentError, ValueError, WorkflowError) as exc:
            await self._retry_recovery(
                lease,
                f"permanent.{type(exc).__name__.lower()}",
                terminal=True,
            )
        except Exception as exc:
            await self._retry_recovery(
                lease,
                f"unexpected.{type(exc).__name__.lower()}",
            )

    async def _retry_recovery(
        self,
        lease: MemoryRecoveryWorkLease,
        error_code: str,
        *,
        terminal: bool = False,
    ) -> None:
        normalized = self._error_code(error_code)
        exhausted = lease.record.attempt_count >= self._config.max_attempts
        now = self._now()
        delay = min(
            self._config.backoff_max_seconds,
            self._config.backoff_base_seconds
            * (2 ** max(0, lease.record.attempt_count - 1)),
        )
        scheduled = await self._recoveries.retry_memory_recovery(
            lease,
            next_attempt_at=None if terminal or exhausted else now + timedelta(seconds=delay),
            error_code=normalized,
            error_at=now,
        )
        _LOGGER.warning(
            "memory_review_recovery_deferred",
            extra={
                "worker_id": lease.worker_id,
                "tenant_id": lease.record.tenant_id,
                "run_id": lease.record.identity.run_id,
                "recovery_id": lease.record.recovery_id,
                "trace_id": lease.record.trace_id,
                "attempt_count": lease.record.attempt_count,
                "error_code": normalized,
                "retry_exhausted": exhausted or terminal,
                "lease_matched": scheduled,
            },
        )

    async def _process_lease(self, lease: MemoryAdmissionWorkLease) -> None:
        example: ReviewedExample | None = None
        try:
            example = await self._examples.get_for_governance(
                lease.tenant_id,
                lease.example_id,
            )
            if example is None:
                raise _PermanentAdmissionWorkError("reviewed_example_missing")
            await self._validate_correction_event(lease, example)
            run = await self._business.get_run(example.run_id, lease.tenant_id)
            if run is None or run.identity.document_id != example.document_id:
                raise _PermanentAdmissionWorkError("workflow_scope_invalid")
            document = await self._documents.get_document(
                example.document_id,
                lease.tenant_id,
            )
            if document is None:
                raise _PermanentAdmissionWorkError("document_scope_invalid")
            stored_result = await self._business.get_result(
                example.run_id,
                lease.tenant_id,
            )
            if stored_result is None:
                raise _RetryableAdmissionWorkError("extraction_result_not_ready")
            if stored_result.document_id != example.document_id:
                raise _PermanentAdmissionWorkError("result_scope_invalid")
            reviewed_result = self._codec.load(
                stored_result.payload,
                self._output_schema,
            )
            result = await self._service.process_admission(
                identity=run.identity,
                tenant_id=lease.tenant_id,
                document=document,
                # The final payload retains original visual evidence; model/reviewed
                # values remain independently bound to the immutable example fact.
                original_result=reviewed_result,
                reviewed_result=reviewed_result,
                examples=(example,),
            )
            case = result.cases[0]
            if not case.projection_reconciled:
                raise _RetryableAdmissionWorkError(
                    "projection_reconciliation_unavailable"
                )
            if case.admission.status is MemoryAdmissionStatus.PENDING:
                if case.advisory_error_code == RemoteInferenceRequestError.__name__:
                    raise _PermanentAdmissionWorkError("model_advisory_request_invalid")
                raise _RetryableAdmissionWorkError(
                    "model_advisory_unavailable"
                    if case.advisory_error_code is not None
                    else "admission_remains_pending"
                )
            await self._complete(lease, case.admission.status)
        except _PermanentAdmissionWorkError as exc:
            await self._quarantine(lease, example, f"permanent.{exc.code}")
        except _RetryableAdmissionWorkError as exc:
            await self._retry_or_quarantine(lease, example, exc.code)
        except RemoteInferenceRequestError as exc:
            await self._quarantine(
                lease,
                example,
                f"permanent.{type(exc).__name__.lower()}",
            )
        except (RemoteInferenceError, StorageError, WorkflowPersistenceError) as exc:
            await self._retry_or_quarantine(
                lease,
                example,
                type(exc).__name__.lower(),
            )
        except (DocumentIntegrityError, InvalidDocumentError, ValueError, WorkflowError) as exc:
            await self._quarantine(
                lease,
                example,
                f"permanent.{type(exc).__name__.lower()}",
            )
        except Exception as exc:
            await self._retry_or_quarantine(
                lease,
                example,
                f"unexpected.{type(exc).__name__.lower()}",
            )

    async def _complete(
        self,
        lease: MemoryAdmissionWorkLease,
        status: MemoryAdmissionStatus,
        error_code: str | None = None,
    ) -> None:
        completed = await self._admissions.complete_claim(
            lease,
            error_code=error_code,
            error_at=self._now() if error_code is not None else None,
        )
        _LOGGER.info(
            "memory_admission_work_completed",
            extra={
                "worker_id": lease.worker_id,
                "tenant_id": lease.tenant_id,
                "example_id": lease.example_id,
                "attempt_count": lease.attempt_count,
                "admission_status": status.value,
                "error_code": error_code,
                "lease_matched": completed,
            },
        )

    async def _validate_correction_event(
        self,
        lease: MemoryAdmissionWorkLease,
        example: ReviewedExample,
    ) -> None:
        source_event_id = example.source_event_id
        if source_event_id is None:
            return
        stored = await self._correction_events.get_correction_event(
            lease.tenant_id,
            source_event_id,
            run_id=example.run_id,
            document_id=example.document_id,
        )
        if stored is None:
            raise _PermanentAdmissionWorkError("correction_event_missing")
        event = stored.event
        if (
            event.document_type != example.document_type
            or event.field_path != example.field_path
            or event.schema_version != example.schema_version
            or event.model_value != example.model_value
            or event.corrected_value != example.reviewed_value
            or event.correction_reason != example.correction_reason
            or event.document_reference
            != example.evidence_reference.document_reference
            or event.image_reference != example.evidence_reference.image_reference
            or not event.is_reviewed
            or not event.is_valid
        ):
            raise _PermanentAdmissionWorkError("correction_event_mismatch")

    async def _retry_or_quarantine(
        self,
        lease: MemoryAdmissionWorkLease,
        example: ReviewedExample | None,
        error_code: str,
    ) -> None:
        normalized = self._error_code(error_code)
        if lease.attempt_count >= self._config.max_attempts:
            await self._quarantine(
                lease,
                example,
                f"retry_exhausted.{normalized}",
            )
            return
        now = self._now()
        delay = min(
            self._config.backoff_max_seconds,
            self._config.backoff_base_seconds * (2 ** (lease.attempt_count - 1)),
        )
        scheduled = await self._admissions.retry_claim(
            lease,
            next_attempt_at=now + timedelta(seconds=delay),
            error_code=normalized,
            error_at=now,
        )
        _LOGGER.warning(
            "memory_admission_work_retry_scheduled",
            extra={
                "worker_id": lease.worker_id,
                "tenant_id": lease.tenant_id,
                "example_id": lease.example_id,
                "attempt_count": lease.attempt_count,
                "error_code": normalized,
                "retry_delay_seconds": delay,
                "lease_matched": scheduled,
            },
        )

    async def _quarantine(
        self,
        lease: MemoryAdmissionWorkLease,
        example: ReviewedExample | None,
        reason_code: str,
    ) -> None:
        normalized = self._error_code(reason_code)
        if example is None:
            now = self._now()
            # The admission row may have been deleted concurrently (for example
            # by tenant cleanup). Release the lease without scheduling a retry;
            # permanent scope failures must never spin forever.
            await self._admissions.complete_claim(
                lease,
                error_code=normalized,
                error_at=now,
            )
            _LOGGER.error(
                "memory_admission_work_orphaned",
                extra={
                    "worker_id": lease.worker_id,
                    "tenant_id": lease.tenant_id,
                    "example_id": lease.example_id,
                    "error_code": normalized,
                },
            )
            return
        result = await self._service.quarantine_processing_failure(
            example=example,
            reason_code=normalized,
        )
        await self._complete(
            lease,
            result.admission.status,
            error_code=normalized,
        )

    async def _wait_for_stop(self, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=self._config.poll_interval_seconds,
            )
        except TimeoutError:
            return

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Worker clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _error_code(value: str) -> str:
        normalized = "".join(
            character if character.isalnum() or character in {".", "_", "-"} else "_"
            for character in value.strip().lower()
        )
        return (normalized or "unknown_error")[:128]
