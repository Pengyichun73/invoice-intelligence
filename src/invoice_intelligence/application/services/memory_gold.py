"""Three-person, current-image gold annotation and adjudication."""

import json
from hashlib import sha256
from typing import Any

from invoice_intelligence.application.errors import (
    BadRequestError,
    ForbiddenError,
    ResourceConflictError,
    ResourceNotFoundError,
    UnprocessableEntityError,
)
from invoice_intelligence.application.ports.document_repository import DocumentReferenceRepository
from invoice_intelligence.application.ports.memory_gold import (
    GoldAnnotationRepository,
    GoldCaseFact,
    GoldObjectStore,
)
from invoice_intelligence.application.services.memory_benefit_evaluation import (
    FIELD_PATHS,
    VERSION_KEYS,
    FrozenInvoiceGold,
)
from invoice_intelligence.domain.governance import MemoryPermission, TrustedTenantContext


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class MemoryGoldService:
    def __init__(
        self, documents: DocumentReferenceRepository,
        repository: GoldAnnotationRepository,
        objects: GoldObjectStore,
    ) -> None:
        self._documents = documents
        self._repository = repository
        self._objects = objects

    async def submit(
        self, context: TrustedTenantContext, document_id: str,
        template_group: str, versions: dict[str, str],
        fields: dict[str, Any],
    ) -> dict[str, object]:
        self._require(context, MemoryPermission.SUBMIT_GOLD)
        document = await self._documents.get_document(document_id, context.tenant_id)
        if document is None:
            raise ResourceNotFoundError("Document was not found")
        self._validate_bindings(template_group, versions)
        row = {
            "case_id": document_id, "document_checksum": document.checksum,
            "template_group": template_group, "gold_complete": True,
            "fields": fields, **versions,
        }
        try:
            FrozenInvoiceGold.from_row(row, document)
        except ValueError as exc:
            raise UnprocessableEntityError("Gold annotation is incomplete or invalid") from exc
        try:
            content = _canonical(fields)
        except (TypeError, ValueError) as exc:
            raise UnprocessableEntityError("Gold annotation JSON is invalid") from exc
        if len(content) > 262_144:
            raise UnprocessableEntityError("Gold annotation exceeds the size limit")
        checksum = sha256(content).hexdigest()
        object_ref = await self._objects.put(context.tenant_id, content, checksum)
        case = await self._repository.add_annotation(
            context.tenant_id, document_id, document.checksum,
            template_group, versions, context.actor_id, object_ref, checksum,
        )
        return self._public_case(case)

    async def adjudicate(
        self, context: TrustedTenantContext, document_id: str,
        field_choices: dict[str, str],
    ) -> dict[str, object]:
        self._require(context, MemoryPermission.ADJUDICATE_GOLD)
        document = await self._documents.get_document(document_id, context.tenant_id)
        if document is None:
            raise ResourceNotFoundError("Document was not found")
        case = await self._repository.get_case(context.tenant_id, document_id)
        if case is None:
            raise ResourceNotFoundError("Gold case was not found")
        if case.status != "open" or len(case.annotations) != 2:
            raise ResourceConflictError("Gold case is not ready for adjudication")
        if context.actor_id in {item.actor_id for item in case.annotations}:
            raise ForbiddenError("Adjudicator must be a third authorized person")
        if case.document_checksum != document.checksum:
            raise ResourceConflictError("Source document checksum changed")
        first, second = sorted(case.annotations, key=lambda item: item.slot)
        try:
            first_fields = json.loads(await self._objects.get(
                context.tenant_id, first.object_ref, first.checksum
            ))
            second_fields = json.loads(await self._objects.get(
                context.tenant_id, second.object_ref, second.checksum
            ))
        except (ValueError, UnicodeError) as exc:
            raise ResourceConflictError("Annotation objects are invalid") from exc
        if not isinstance(first_fields, dict) or not isinstance(second_fields, dict):
            raise ResourceConflictError("Annotation objects are invalid")
        try:
            for fields in (first_fields, second_fields):
                FrozenInvoiceGold.from_row({
                    "case_id": document_id, "document_checksum": document.checksum,
                    "template_group": case.template_group, "gold_complete": True,
                    "fields": fields, **case.versions,
                }, document)
        except ValueError as exc:
            raise ResourceConflictError("Annotation objects are invalid") from exc
        disagreements = {
            path for path in FIELD_PATHS
            if _canonical(first_fields[path]) != _canonical(second_fields[path])
        }
        if set(field_choices) != disagreements or any(
            choice not in {"first", "second"} for choice in field_choices.values()
        ):
            raise ResourceConflictError("Every disagreement requires an explicit choice")
        selected = {
            path: (first_fields if field_choices.get(path) != "second" else second_fields)[path]
            for path in sorted(FIELD_PATHS)
        }
        frozen = {
            "case_id": document_id, "document_checksum": document.checksum,
            "template_group": case.template_group, "gold_complete": True,
            "fields": selected, **case.versions,
        }
        try:
            FrozenInvoiceGold.from_row(frozen, document)
        except ValueError as exc:
            raise ResourceConflictError("Adjudicated gold is invalid") from exc
        content = _canonical(frozen)
        checksum = sha256(content).hexdigest()
        object_ref = await self._objects.put(context.tenant_id, content, checksum)
        result = await self._repository.freeze(
            context.tenant_id, document_id, (first.checksum, second.checksum),
            context.actor_id, field_choices, object_ref, checksum,
        )
        return self._public_case(result)

    async def get(self, context: TrustedTenantContext, document_id: str) -> dict[str, object]:
        self._require(context, MemoryPermission.READ_EVALUATION)
        case = await self._repository.get_case(context.tenant_id, document_id)
        if case is None:
            raise ResourceNotFoundError("Gold case was not found")
        return self._public_case(case)

    @staticmethod
    def _validate_bindings(template_group: str, versions: dict[str, str]) -> None:
        if not template_group.strip() or template_group != template_group.strip():
            raise BadRequestError("Template group is required")
        if set(versions) != set(VERSION_KEYS) or any(
            not isinstance(value, str) or not value.strip() for value in versions.values()
        ):
            raise BadRequestError("Gold case requires every version binding")
        if versions["schema_version"] != "3.0.0":
            raise BadRequestError("Gold case Schema version is not supported")

    @staticmethod
    def _public_case(case: GoldCaseFact) -> dict[str, object]:
        return {
            "document_id": case.document_id,
            "status": case.status,
            "annotation_count": len(case.annotations),
            "annotator_ids": [item.actor_id for item in case.annotations],
            "adjudicator_id": case.adjudicator_id,
            "document_checksum": case.document_checksum,
            "template_group": case.template_group,
            "versions": case.versions,
            "gold_checksum": case.gold_checksum,
            "frozen_at": case.frozen_at,
        }

    @staticmethod
    def _require(context: TrustedTenantContext, permission: MemoryPermission) -> None:
        if not context.permits(permission):
            raise ForbiddenError("Gold annotation permission is required")
