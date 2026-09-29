"""Three independent identities freeze complete current-image gold evidence."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from invoice_intelligence.application.errors import ForbiddenError, ResourceNotFoundError
from invoice_intelligence.application.ports.memory_gold import (
    GoldAnnotationFact,
    GoldCaseFact,
)
from invoice_intelligence.application.services.memory_gold import MemoryGoldService
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.governance import MemoryPermission, TrustedTenantContext
from invoice_intelligence.domain.invoice import InvoiceExtraction

VERSIONS = {
    "schema_version": "3.0.0", "model_version": "model-a",
    "prompt_version": "prompt-a", "catalog_version": "catalog-a",
    "index_version": "field-pattern-v1-a",
}
DOCUMENT = DocumentReference("doc-1", "isolated://doc-1", "image/png", "a" * 64)


class _Documents:
    async def get_document(self, document_id, tenant_id):
        return DOCUMENT if (document_id, tenant_id) == ("doc-1", "tenant-a") else None


class _Objects:
    def __init__(self):
        self.content = {}

    async def put(self, tenant_id, content, checksum):
        assert tenant_id == "tenant-a"
        assert sha256(content).hexdigest() == checksum
        ref = f"isolated://{checksum}"
        self.content[ref] = content
        return ref

    async def get(self, tenant_id, object_ref, checksum):
        content = self.content[object_ref]
        assert tenant_id == "tenant-a" and sha256(content).hexdigest() == checksum
        return content


class _Repository:
    def __init__(self):
        self.case = None

    async def get_case(self, tenant_id, document_id):
        return self.case if tenant_id == "tenant-a" and document_id == "doc-1" else None

    async def add_annotation(
        self, tenant_id, document_id, document_checksum, template_group,
        versions, actor_id, object_ref, checksum,
    ):
        assert tenant_id == "tenant-a"
        if self.case is None:
            self.case = GoldCaseFact(
                document_id, document_checksum, template_group, versions, "open",
                (), None, None, None, None, None,
            )
        slot = "first" if not self.case.annotations else "second"
        self.case = replace(self.case, annotations=(*self.case.annotations,
            GoldAnnotationFact(actor_id, slot, object_ref, checksum, datetime.now(UTC))))
        return self.case

    async def freeze(
        self, tenant_id, document_id, expected_checksums,
        adjudicator_id, field_choices, gold_ref, gold_checksum,
    ):
        assert tenant_id == "tenant-a"
        assert tuple(item.checksum for item in self.case.annotations) == expected_checksums
        self.case = replace(
            self.case, status="frozen", adjudicator_id=adjudicator_id,
            field_choices=field_choices, gold_ref=gold_ref,
            gold_checksum=gold_checksum, frozen_at=datetime.now(UTC),
        )
        return self.case


def _context(actor: str, permission: MemoryPermission) -> TrustedTenantContext:
    return TrustedTenantContext("tenant-a", actor, frozenset({permission}))


def _fields(value: str):
    fields = {path: {"state": "absent", "value": None}
              for path in InvoiceExtraction.model_fields}
    fields["invoice_number"] = {
        "state": "present", "value": value,
        "page_number": 1, "bounding_box": [0, 0, 10, 10],
        "observed_text": value,
    }
    return fields


@pytest.mark.asyncio
async def test_three_people_freeze_without_exposing_values() -> None:
    objects = _Objects()
    service = MemoryGoldService(_Documents(), _Repository(), objects)
    await service.submit(
        _context("reviewer-a", MemoryPermission.SUBMIT_GOLD),
        "doc-1", "group-a", VERSIONS, _fields("A-001"),
    )
    await service.submit(
        _context("reviewer-b", MemoryPermission.SUBMIT_GOLD),
        "doc-1", "group-a", VERSIONS, _fields("B-002"),
    )
    with pytest.raises(ForbiddenError):
        await service.adjudicate(
            _context("reviewer-b", MemoryPermission.ADJUDICATE_GOLD),
            "doc-1", {"invoice_number": "first"},
        )
    result = await service.adjudicate(
        _context("reviewer-c", MemoryPermission.ADJUDICATE_GOLD),
        "doc-1", {"invoice_number": "first"},
    )
    assert result["status"] == "frozen"
    assert result["adjudicator_id"] == "reviewer-c"
    assert "A-001" not in str(result) and "B-002" not in str(result)
    frozen = json.loads(objects.content[f"isolated://{result['gold_checksum']}"])
    assert frozen["fields"]["invoice_number"]["value"] == "A-001"


@pytest.mark.asyncio
async def test_cross_tenant_document_is_hidden() -> None:
    service = MemoryGoldService(_Documents(), _Repository(), _Objects())
    context = TrustedTenantContext(
        "tenant-b", "reviewer-a", frozenset({MemoryPermission.SUBMIT_GOLD})
    )
    with pytest.raises(ResourceNotFoundError):
        await service.submit(context, "doc-1", "group-a", VERSIONS, _fields("A-001"))
