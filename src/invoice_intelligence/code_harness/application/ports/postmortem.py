"""Postmortem facts and admission persistence boundary."""

from typing import Protocol

from ...domain.postmortem import (
    AdmissionStatus,
    Postmortem,
    PostmortemSourceEvent,
)


class PostmortemRepository(Protocol):
    async def record_source_event(
        self,
        event: PostmortemSourceEvent,
    ) -> Postmortem:
        ...

    async def set_admission_status(
        self,
        *,
        postmortem_id: str,
        expected_revision: int,
        status: AdmissionStatus,
        actor_id: str,
        reason_code: str,
    ) -> Postmortem:
        ...

    async def get(
        self,
        *,
        tenant_id: str,
        postmortem_id: str,
    ) -> Postmortem | None:
        ...
