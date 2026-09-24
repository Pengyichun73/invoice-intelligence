"""PostgreSQL-owned object lifecycle boundary."""

from typing import Protocol

from invoice_intelligence.domain.storage import StoredObjectRecord


class StoredObjectRepository(Protocol):
    async def register_available(self, record: StoredObjectRecord) -> None: ...

    async def claim_deletions(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> tuple[StoredObjectRecord, ...]: ...

    async def mark_deleted(self, object_id: str, worker_id: str, revision: int) -> None: ...

    async def mark_failure(
        self,
        object_id: str,
        worker_id: str,
        revision: int,
        error_code: str,
        retryable: bool,
    ) -> None: ...
