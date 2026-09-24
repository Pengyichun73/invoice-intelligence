"""Workflow validation, persistence, and correction-memory boundaries."""

from collections.abc import Mapping
from typing import Protocol, TypeVar

from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.extraction import ExtractionResult
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    HumanCorrection,
    JsonValue,
    ReviewRequest,
    ValidationOutcome,
    ValidationRoute,
    WorkflowIdentity,
    WorkflowStatus,
)

InvoiceT = TypeVar("InvoiceT")


class ExtractionValidator(Protocol):
    """Apply deterministic Python validation rules to an extraction."""

    def validate(self, result: ExtractionResult[InvoiceT]) -> ValidationOutcome:
        """Return field decisions and the accepted/review_required/rejected route."""

        ...


class ExtractionStateCodec(Protocol):
    """Convert extraction results to checkpoint-safe primitive state."""

    def dump(self, result: ExtractionResult[InvoiceT]) -> dict[str, JsonValue]:
        """Serialize without raw document bytes or model reasoning."""

        ...

    def load(
        self,
        payload: dict[str, JsonValue],
        output_schema: type[InvoiceT],
    ) -> ExtractionResult[InvoiceT]:
        """Restore and validate an extraction result from checkpoint state."""

        ...


class HumanCorrectionApplier(Protocol):
    """Validate and apply a human correction to an extraction."""

    def apply(
        self,
        result: ExtractionResult[InvoiceT],
        correction: HumanCorrection,
        review_request: ReviewRequest,
        output_schema: type[InvoiceT],
        document: DocumentReference,
    ) -> tuple[ExtractionResult[InvoiceT], tuple[CorrectionEvent, ...]]:
        """Return a schema-valid corrected result and correction events."""

        ...


class WorkflowRunRepository(Protocol):
    """Persist workflow status separately from LangGraph checkpoints."""

    async def upsert_status(
        self,
        identity: WorkflowIdentity,
        status: WorkflowStatus,
        tenant_id: str,
        failure_message: str | None = None,
        validation_route: ValidationRoute | None = None,
    ) -> None:
        """Idempotently persist one run identity and its current status."""

        ...


class ReviewTaskRepository(Protocol):
    """Persist the current human-review task independently of checkpoints."""

    async def upsert_pending_review(
        self,
        identity: WorkflowIdentity,
        request: ReviewRequest,
    ) -> None:
        """Idempotently create or reopen the current review task."""

        ...

    async def resolve_review(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        reviewer_id: str,
        trace_id: str | None,
    ) -> None:
        """Mark the task submitted after immutable review facts are durable."""

        ...


class HumanCorrectionRepository(Protocol):
    """Persist schema-valid human submissions separately from correction events."""

    async def save_human_correction(
        self,
        identity: WorkflowIdentity,
        correction: HumanCorrection,
    ) -> None:
        """Write one immutable correction payload idempotently."""

        ...


class WorkflowExecutionGateway(Protocol):
    """Application-facing boundary implemented by the LangGraph runner."""

    async def start(
        self,
        identity: WorkflowIdentity,
        document: DocumentReference,
        tenant_id: str,
        trace_id: str | None = None,
    ) -> Mapping[str, object]:
        """Start one deterministic workflow execution."""

        ...

    async def resume(
        self,
        identity: WorkflowIdentity,
        correction: HumanCorrection,
    ) -> Mapping[str, object]:
        """Resume one interrupted workflow with a human correction."""

        ...

    async def get_state(self, identity: WorkflowIdentity) -> Mapping[str, object]:
        """Return the current checkpoint state for an authorized workflow identity."""

        ...


class ExtractionResultRepository(Protocol):
    """Persist final invoice extraction results idempotently."""

    async def upsert_result(
        self,
        identity: WorkflowIdentity,
        result: ExtractionResult[InvoiceT],
    ) -> None:
        """Write once by run_id; accept only an identical replay."""

        ...
