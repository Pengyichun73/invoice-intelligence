"""Stable, value-free batch and invoice-provenance responses."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InvoiceRegionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)


class InvoiceGroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regions: tuple[InvoiceRegionResponse, ...]


class InvoiceBatchFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str
    document_id: str
    filename: str | None
    ordinal: int
    status: str
    revision: int
    page_count: int | None
    groups: tuple[InvoiceGroupResponse, ...]
    error_code: str | None


class InvoiceBatchItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    file_id: str
    ordinal: int
    regions: tuple[InvoiceRegionResponse, ...]
    document_id: str | None
    run_id: str | None
    run_attempt: int = Field(ge=1)
    derived_pages: tuple[dict[str, int], ...]


class InvoiceBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    status: Literal[
        "open", "segmenting", "needs_boundary_review", "too_many_invoices",
        "extracting", "dispatched", "pending_review", "completed", "failed",
    ]
    revision: int
    files: tuple[InvoiceBatchFileResponse, ...]
    items: tuple[InvoiceBatchItemResponse, ...]


class InvoiceBatchSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    status: str
    created_at: datetime
