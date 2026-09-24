"""Document upload and extraction-start protocol adapters."""

from typing import Annotated

from fastapi import APIRouter, File, Header, Path, Query, Response, UploadFile, status

from invoice_intelligence.api.dependencies import (
    DocumentAccessDependency,
    DocumentIngestionDependency,
    ExtractionWorkflowServiceDependency,
    MaxUploadBytesDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.presenters import present_run
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.documents import DocumentDownloadResponse, DocumentResponse
from invoice_intelligence.api.schemas.workflows import ExtractionRunResponse
from invoice_intelligence.application.errors import DocumentTooLargeError
from invoice_intelligence.domain.document import UploadDocument

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get(
    "/{document_id}/download-url",
    response_model=DocumentDownloadResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def create_document_download_url(
    document_id: Annotated[str, Path(min_length=1)],
    service: DocumentAccessDependency,
    context: TrustedTenantContextDependency,
) -> DocumentDownloadResponse:
    url, expires = await service.create_download_url(document_id, context.tenant_id)
    return DocumentDownloadResponse(url=url, expires_in_seconds=expires)


@router.get("/{document_id}/content", response_class=Response, include_in_schema=False)
async def download_local_document(
    document_id: Annotated[str, Path(min_length=1)],
    token: Annotated[str, Query(min_length=16)],
    service: DocumentAccessDependency,
    context: TrustedTenantContextDependency,
) -> Response:
    result = await service.read_local_content(document_id, context.tenant_id, token)
    return Response(content=result.content, media_type=result.media_type)


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
)
async def upload_document(
    file: Annotated[UploadFile, File(description="PNG, JPG, JPEG, WEBP, or PDF")],
    ingestion_service: DocumentIngestionDependency,
    max_upload_bytes: MaxUploadBytesDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> DocumentResponse:
    """Validate and persist an upload under a required idempotency key."""

    filename = file.filename
    content_type = file.content_type
    try:
        content = await file.read(max_upload_bytes + 1)
    finally:
        await file.close()
    if len(content) > max_upload_bytes:
        raise DocumentTooLargeError("Uploaded document exceeds the configured size limit")

    reference = await ingestion_service.ingest(
        UploadDocument(
            filename=filename,
            declared_mime_type=content_type,
            content=content,
        ),
        idempotency_key=idempotency_key,
        tenant_id=context.tenant_id,
        trace_id=context.trace_id,
    )
    return DocumentResponse(
        document_id=reference.document_id,
        storage_uri=reference.storage_uri,
        mime_type=reference.mime_type,
        checksum=reference.checksum,
        size_bytes=reference.size_bytes,
        storage_status=reference.storage_status,
    )


@router.post(
    "/{document_id}/extract",
    response_model=ExtractionRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=STANDARD_ERROR_RESPONSES,
)
async def extract_document(
    document_id: Annotated[str, Path(min_length=1)],
    service: ExtractionWorkflowServiceDependency,
    context: TrustedTenantContextDependency,
) -> ExtractionRunResponse:
    """Start a new deterministic extraction run through the Service Layer."""

    return present_run(
        await service.extract_document(
            document_id,
            context.tenant_id,
            trace_id=context.trace_id,
        )
    )
