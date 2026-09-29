"""Private gold objects and immutable annotation facts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GoldAnnotationFact:
    actor_id: str
    slot: str
    object_ref: str
    checksum: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GoldCaseFact:
    document_id: str
    document_checksum: str
    template_group: str
    versions: dict[str, str]
    status: str
    annotations: tuple[GoldAnnotationFact, ...]
    gold_ref: str | None
    gold_checksum: str | None
    adjudicator_id: str | None
    field_choices: dict[str, str] | None
    frozen_at: datetime | None


class GoldObjectStore(Protocol):
    async def put(self, tenant_id: str, content: bytes, checksum: str) -> str: ...

    async def get(self, tenant_id: str, object_ref: str, checksum: str) -> bytes: ...


class GoldAnnotationRepository(Protocol):
    async def get_case(self, tenant_id: str, document_id: str) -> GoldCaseFact | None: ...

    async def add_annotation(
        self, tenant_id: str, document_id: str, document_checksum: str,
        template_group: str, versions: dict[str, str], actor_id: str,
        object_ref: str, checksum: str,
    ) -> GoldCaseFact: ...

    async def freeze(
        self, tenant_id: str, document_id: str, expected_checksums: tuple[str, str],
        adjudicator_id: str, field_choices: dict[str, str],
        gold_ref: str, gold_checksum: str,
    ) -> GoldCaseFact: ...
