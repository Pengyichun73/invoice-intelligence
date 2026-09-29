"""Structured Qwen layout proposals; no invoice field values are requested."""

import base64
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.invoice_batch import InvoiceGroup, InvoiceRegion, validate_groups
from invoice_intelligence.infrastructure.qwen.client import QwenRemoteClient
from invoice_intelligence.infrastructure.qwen.redaction import QwenPayloadGuard


class _Region(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regions: list[_Region] = Field(max_length=20)
    uncertain: bool


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[_Group] = Field(max_length=100)
    uncertain: bool


class QwenInvoiceSegmentationProvider:
    def __init__(self, client: QwenRemoteClient, guard: QwenPayloadGuard, model: str) -> None:
        self._client = client
        self._guard = guard
        self._model = model

    async def detect(self, images: tuple[VisionImage, ...]) -> tuple[tuple[InvoiceGroup, ...], bool]:
        self._guard.validate_vision_images(images)
        content: list[dict] = [{"type": "text", "text": (
            "Identify every distinct invoice, including multiple invoices on one page "
            "and invoices continuing across pages. Return only page numbers, normalized "
            "rectangles and grouping. Do not return invoice values, OCR text or names. "
            "Cover each page. Mark uncertain whenever an invoice boundary or cross-page "
            "grouping is not unambiguous. Never infer an invoice from historical data."
        )}]
        for image in images:
            content.extend((
                {"type": "text", "text": f"Page {image.page_number}"},
                {"type": "image_url", "image_url": {
                    "url": f"data:{image.mime_type};base64,{base64.b64encode(image.content).decode('ascii')}"
                }},
            ))
        response = await self._client.create_chat(
            operation="invoice_segmentation", model=self._model,
            messages=(
                {"role": "system", "content": "Return a complete JSON invoice layout, not field values."},
                {"role": "user", "content": content},
            ),
            response_format={"type": "json_schema", "json_schema": {
                "name": "invoice_layout", "strict": True,
                "schema": _Proposal.model_json_schema(),
            }},
            trace_id=uuid4().hex,
        )
        choices = getattr(response, "choices", None)
        raw = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if not isinstance(raw, str):
            raise ValueError("Invoice boundary provider returned no structured content")
        proposal = _Proposal.model_validate(json.loads(raw))
        groups = tuple(InvoiceGroup(tuple(InvoiceRegion(**region.model_dump())
                                          for region in group.regions)) for group in proposal.groups)
        validate_groups(groups, len(images))
        return groups, proposal.uncertain or any(group.uncertain for group in proposal.groups)
