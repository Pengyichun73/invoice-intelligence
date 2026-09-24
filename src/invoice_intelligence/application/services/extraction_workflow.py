"""Service layer for starting, querying, and resuming extraction workflows."""

import json
from contextlib import nullcontext
from hashlib import sha256
from typing import cast
from uuid import uuid4

from invoice_intelligence.application.errors import (
    BadRequestError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.business_persistence import (
    BusinessQueryRepository,
    ExtractionResultRecord,
    ExtractionRunRecord,
    IdempotencyRepository,
    IdempotencyStatus,
    ReviewTaskRecord,
)
from invoice_intelligence.application.ports.document_repository import (
    DocumentReferenceRepository,
)
from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.application.ports.workflow import WorkflowExecutionGateway
from invoice_intelligence.application.services.idempotency import (
    normalize_idempotency_key,
)
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.workflow import (
    HumanCorrection,
    WorkflowIdentity,
    WorkflowStatus,
)


class ExtractionWorkflowService:
    """Only application service allowed to invoke the workflow execution gateway."""

    def __init__(
        self,
        workflow: WorkflowExecutionGateway | None,
        document_repository: DocumentReferenceRepository,
        query_repository: BusinessQueryRepository,
        idempotency_repository: IdempotencyRepository,
        privacy_telemetry: PrivacyTelemetry | None = None,
    ) -> None:
        self._workflow = workflow
        self._document_repository = document_repository
        self._query_repository = query_repository
        self._idempotency_repository = idempotency_repository
        self._privacy_telemetry = privacy_telemetry

    async def extract_document(
        self,
        document_id: str,
        tenant_id: str,
        trace_id: str | None = None,
    ) -> ExtractionRunRecord:
        """Start one new extraction run for an existing document."""

        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id or normalized_tenant_id != tenant_id:
            raise BadRequestError("tenant_id must be non-empty and normalized")
        document = await self._document_repository.get_document(
            document_id,
            normalized_tenant_id,
        )
        if document is None:
            raise ResourceNotFoundError("Document was not found")
        identity = WorkflowIdentity(
            thread_id=f"thread_{uuid4().hex}",
            run_id=f"run_{uuid4().hex}",
            document_id=document.document_id,
        )
        workflow = self._require_workflow()
        await workflow.start(identity, document, normalized_tenant_id, trace_id=trace_id)
        run = await self._query_repository.get_run(identity.run_id, normalized_tenant_id)
        if run is None:
            raise WorkflowPersistenceError("Extraction run was not persisted")
        return run

    async def get_run(self, run_id: str, tenant_id: str) -> ExtractionRunRecord:
        run = await self._query_repository.get_run(run_id, tenant_id)
        if run is None:
            raise ResourceNotFoundError("Extraction run was not found")
        return run

    async def get_result(self, run_id: str, tenant_id: str) -> ExtractionResultRecord:
        run = await self.get_run(run_id, tenant_id)
        result = await self._query_repository.get_result(run_id, tenant_id)
        if result is None:
            if run.status is WorkflowStatus.FAILED:
                raise ResourceConflictError("Failed extraction run has no result")
            raise ResourceConflictError("Extraction result is not available yet")
        return result

    async def get_review(self, run_id: str, tenant_id: str) -> ReviewTaskRecord:
        await self.get_run(run_id, tenant_id)
        review = await self._query_repository.get_review(run_id, tenant_id)
        if review is None:
            raise ResourceNotFoundError("Review task was not found")
        return review

    async def get_review_extraction(
        self,
        run_id: str,
        tenant_id: str,
    ) -> dict[str, JsonValue]:
        """Return the checkpoint extraction required to build a complete correction."""

        run = await self.get_run(run_id, tenant_id)
        if run.status is not WorkflowStatus.PENDING_REVIEW:
            raise ResourceConflictError("Extraction run is not pending human review")
        state = await self._require_workflow().get_state(run.identity)
        extraction = state.get("extraction")
        if not isinstance(extraction, dict):
            raise WorkflowPersistenceError("Pending review has no extraction state")
        return cast(dict[str, JsonValue], extraction)

    async def submit_review(
        self,
        run_id: str,
        correction: HumanCorrection,
        idempotency_key: str | None,
        tenant_id: str,
        trace_id: str | None = None,
    ) -> ExtractionRunRecord:
        """Resume a pending review through Command(resume=...) behind the gateway."""

        normalized_key = normalize_idempotency_key(idempotency_key)
        run = await self.get_run(run_id, tenant_id)
        operation = f"submit_review:{tenant_id}:{run_id}"
        fingerprint = self._correction_fingerprint(correction)
        existing = await self._idempotency_repository.get_idempotency(
            operation,
            normalized_key,
        )
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise IdempotencyConflictError(
                    "Idempotency key is already bound to a different review submission"
                )
            if existing.status is IdempotencyStatus.COMPLETED:
                return await self.get_run(run_id, tenant_id)
            raise IdempotencyInProgressError(
                "A review submission with this idempotency key is already in progress"
            )

        if run.status is not WorkflowStatus.PENDING_REVIEW:
            raise ResourceConflictError("Extraction run is not pending human review")
        submission_id = f"submission_{uuid4().hex}"
        claim = await self._idempotency_repository.claim_idempotency(
            operation=operation,
            key=normalized_key,
            request_hash=fingerprint,
            resource_id=submission_id,
        )
        if claim.status is IdempotencyStatus.COMPLETED:
            return await self.get_run(run_id, tenant_id)
        if claim.resource_id != submission_id:
            raise IdempotencyInProgressError(
                "A review submission with this idempotency key is already in progress"
            )
        workflow = self._require_workflow()
        span = (
            self._privacy_telemetry.span(
                trace_id=trace_id,
                tenant_id=tenant_id,
                stage=TraceStage.HUMAN_REVIEW,
                operation="submit_human_review",
                attributes={"run_id": run_id, "document_id": run.identity.document_id},
            )
            if self._privacy_telemetry is not None and trace_id is not None
            else nullcontext()
        )
        try:
            with span:
                workflow_state = await workflow.resume(run.identity, correction)
        except Exception:
            await self._idempotency_repository.release_idempotency(
                operation=operation,
                key=normalized_key,
                request_hash=fingerprint,
                resource_id=submission_id,
            )
            raise
        if workflow_state.get("review_fact_persistence_failed") is True:
            await self._idempotency_repository.release_idempotency(
                operation=operation,
                key=normalized_key,
                request_hash=fingerprint,
                resource_id=submission_id,
            )
            return await self.get_run(run_id, tenant_id)
        await self._idempotency_repository.complete_idempotency(
            operation=operation,
            key=normalized_key,
            request_hash=fingerprint,
            response_payload={"run_id": run_id},
        )
        return await self.get_run(run_id, tenant_id)

    def _require_workflow(self) -> WorkflowExecutionGateway:
        if self._workflow is None:
            raise ServiceUnavailableError("Invoice extraction workflow is not configured")
        return self._workflow

    @staticmethod
    def _correction_fingerprint(correction: HumanCorrection) -> str:
        payload: dict[str, JsonValue] = {
            "corrected_invoice": (
                dict(correction.corrected_invoice)
                if correction.corrected_invoice is not None
                else None
            ),
            "reviewer_id": correction.reviewer_id,
            "document_type": correction.document_type,
            "fields": [
                {
                    "field_path": item.field_path,
                    "action": item.action.value,
                    "reason": item.reason,
                    "rejected_value": item.rejected_value,
                }
                for item in correction.fields
            ],
            "field_bindings": [
                {
                    "evidence_id": item.evidence_id,
                    "selected_canonical_field_path": (item.selected_canonical_field_path),
                    "reason": item.reason,
                }
                for item in correction.field_bindings
            ],
        }
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()
