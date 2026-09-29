"""HTTP adapters for bounded multi-invoice extraction."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, Path, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from invoice_intelligence.api.dependencies import (
    ApiDependencyBundle, MaxUploadBytesDependency, TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.invoice_batches import InvoiceBatchResponse, InvoiceBatchSummaryResponse
from invoice_intelligence.application.errors import DocumentTooLargeError, ServiceUnavailableError
from invoice_intelligence.application.services.invoice_batches import InvoiceBatchService
from invoice_intelligence.domain.document import UploadDocument
from invoice_intelligence.domain.invoice_batch import InvoiceGroup, InvoiceRegion

router = APIRouter(prefix="/invoice-batches", tags=["invoice-batches"])


def _service(dependencies: ApiDependencyBundle) -> InvoiceBatchService:
    service = dependencies.invoice_batch_service
    if service is None:
        raise ServiceUnavailableError("Invoice batch extraction is unavailable")
    return service


BatchServiceDependency = Annotated[InvoiceBatchService, Depends(_service)]


class _Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class _Region(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regions: list[_Region] = Field(min_length=1, max_length=20)


class _Confirmation(_Revision):
    groups: list[_Group] = Field(min_length=1, max_length=5)


class _Retry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_id: str = Field(min_length=1)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvoiceBatchResponse)
async def create_batch(
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvoiceBatchResponse:
    return InvoiceBatchResponse.model_validate(await service.create(context.tenant_id, idempotency_key, context.actor_id))


@router.get("", response_model=list[InvoiceBatchSummaryResponse])
async def list_batches(
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
) -> list[InvoiceBatchSummaryResponse]:
    return [InvoiceBatchSummaryResponse.model_validate(item)
            for item in await service.list_recent(context.tenant_id, context.actor_id)]


@router.post("/{batch_id}/files", status_code=status.HTTP_201_CREATED, response_model=InvoiceBatchResponse)
async def upload_batch_file(
    batch_id: Annotated[str, Path(min_length=1)],
    file: Annotated[UploadFile, File()],
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
    max_upload_bytes: MaxUploadBytesDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvoiceBatchResponse:
    try:
        content = await file.read(max_upload_bytes + 1)
    finally:
        await file.close()
    if len(content) > max_upload_bytes:
        raise DocumentTooLargeError("Uploaded document exceeds the configured size limit")
    result = await service.add_file(
        batch_id, context.tenant_id,
        UploadDocument(file.filename, file.content_type, content),
        idempotency_key, context.trace_id,
    )
    return InvoiceBatchResponse.model_validate(result)


@router.post("/{batch_id}/submit", status_code=status.HTTP_202_ACCEPTED, response_model=InvoiceBatchResponse)
async def submit_batch(
    batch_id: Annotated[str, Path(min_length=1)],
    body: _Revision,
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvoiceBatchResponse:
    return InvoiceBatchResponse.model_validate(
        await service.submit(batch_id, context.tenant_id, body.expected_revision)
    )


@router.get("/{batch_id}", response_model=InvoiceBatchResponse)
async def get_batch(
    batch_id: Annotated[str, Path(min_length=1)],
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
) -> InvoiceBatchResponse:
    return InvoiceBatchResponse.model_validate(await service.get(batch_id, context.tenant_id))


@router.get("/{batch_id}/files/{file_id}/pages/{page_number}", response_class=Response)
async def preview_page(
    batch_id: Annotated[str, Path(min_length=1)],
    file_id: Annotated[str, Path(min_length=1)],
    page_number: Annotated[int, Path(ge=1)],
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
) -> Response:
    content = await service.preview_page(batch_id, file_id, context.tenant_id, page_number)
    return Response(content=content, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/{batch_id}/files/{file_id}/confirm", response_model=InvoiceBatchResponse)
async def confirm_boundaries(
    batch_id: Annotated[str, Path(min_length=1)],
    file_id: Annotated[str, Path(min_length=1)],
    body: _Confirmation,
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvoiceBatchResponse:
    groups = tuple(InvoiceGroup(tuple(InvoiceRegion(**region.model_dump())
                                      for region in group.regions)) for group in body.groups)
    return InvoiceBatchResponse.model_validate(
        await service.confirm(batch_id, file_id, context.tenant_id, body.expected_revision, groups)
    )


@router.post("/{batch_id}/items/{item_id}/retry", response_model=InvoiceBatchResponse)
async def retry_failed_item(
    batch_id: Annotated[str, Path(min_length=1)],
    item_id: Annotated[str, Path(min_length=1)],
    body: _Retry,
    service: BatchServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvoiceBatchResponse:
    return InvoiceBatchResponse.model_validate(await service.retry_item(
        batch_id, item_id, context.tenant_id, body.expected_run_id, idempotency_key,
    ))
