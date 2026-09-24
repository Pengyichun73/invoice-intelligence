"""Document ingestion use case."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from invoice_intelligence.application.errors import (
    IdempotencyInProgressError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.business_persistence import (
    IdempotencyRepository,
    IdempotencyStatus,
)
from invoice_intelligence.application.ports.document_processor import DocumentProcessor
from invoice_intelligence.application.ports.document_repository import (
    DocumentReferenceRepository,
)
from invoice_intelligence.application.ports.file_storage import FileStorage
from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.application.services.idempotency import (
    normalize_idempotency_key,
)
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    DocumentReference,
    UploadDocument,
)
from invoice_intelligence.domain.storage import ObjectKind, StorageWriteRequest


class DocumentIngestionService:
    """Validate and durably store one uploaded source document."""

    def __init__(
        self,
        file_storage: FileStorage,
        document_processor: DocumentProcessor,
        document_repository: DocumentReferenceRepository,
        idempotency_repository: IdempotencyRepository,
        limits: DocumentProcessingLimits,
        privacy_telemetry: PrivacyTelemetry | None = None,
        original_retention_days: int | None = None,
    ) -> None:
        self._file_storage = file_storage
        self._document_processor = document_processor
        self._document_repository = document_repository
        self._idempotency_repository = idempotency_repository
        self._limits = limits
        self._privacy_telemetry = privacy_telemetry
        self._original_retention_days = original_retention_days

    async def ingest(
        self,
        document: UploadDocument,
        idempotency_key: str | None,
        tenant_id: str,
        trace_id: str | None = None,
    ) -> DocumentReference:
        """Validate before storage and return workflow-safe metadata."""

        if self._privacy_telemetry is None or trace_id is None:
            return await self._ingest(document, idempotency_key, tenant_id)
        with self._privacy_telemetry.span(
            trace_id=trace_id,
            tenant_id=tenant_id,
            stage=TraceStage.INGESTION,
            operation="ingest_document",
        ):
            return await self._ingest(document, idempotency_key, tenant_id)

    async def _ingest(
        self,
        document: UploadDocument,
        idempotency_key: str | None,
        tenant_id: str,
    ) -> DocumentReference:

        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id or normalized_tenant_id != tenant_id:
            raise ValueError("tenant_id must be non-empty and normalized")
        normalized_key = normalize_idempotency_key(idempotency_key)
        inspected = await self._document_processor.inspect(document, self._limits)
        document_id = str(uuid4())
        checksum = sha256(document.content).hexdigest()
        operation = f"upload_document:{normalized_tenant_id}"
        fingerprint = sha256(
            f"{inspected.mime_type}\0{checksum}".encode()
        ).hexdigest()
        claim = await self._idempotency_repository.claim_idempotency(
            operation=operation,
            key=normalized_key,
            request_hash=fingerprint,
            resource_id=document_id,
        )
        if claim.status is IdempotencyStatus.COMPLETED:
            existing = await self._document_repository.get_document(
                claim.resource_id,
                normalized_tenant_id,
            )
            if existing is None:
                raise WorkflowPersistenceError(
                    "Completed upload idempotency record references a missing document"
                )
            return existing
        if claim.resource_id != document_id:
            raise IdempotencyInProgressError(
                "An upload with this idempotency key is already in progress"
            )

        stored = await self._file_storage.save_object(
            StorageWriteRequest(
                object_id=document_id,
                tenant_id=normalized_tenant_id,
                kind=ObjectKind.ORIGINAL,
                content=document.content,
                media_type=inspected.mime_type,
                checksum=checksum,
            )
        )
        verified = await self._file_storage.head(stored.storage_uri)
        if verified.checksum != checksum or verified.size_bytes != inspected.size_bytes:
            raise WorkflowPersistenceError("Stored object failed integrity verification")
        reference = DocumentReference(
            document_id=document_id,
            storage_uri=stored.storage_uri,
            mime_type=inspected.mime_type,
            checksum=checksum,
            size_bytes=inspected.size_bytes,
            retention_until=(
                datetime.now(UTC) + timedelta(days=self._original_retention_days)
                if self._original_retention_days is not None
                else None
            ),
        )
        await self._document_repository.save_document(reference, normalized_tenant_id)
        await self._idempotency_repository.complete_idempotency(
            operation=operation,
            key=normalized_key,
            request_hash=fingerprint,
            response_payload={"document_id": reference.document_id},
        )
        return reference
