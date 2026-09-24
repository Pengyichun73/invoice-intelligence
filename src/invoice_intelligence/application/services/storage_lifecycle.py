"""Deterministic storage lifecycle processing."""

from invoice_intelligence.application.errors import (
    StorageObjectNotFoundError,
    StoragePermissionDeniedError,
)
from invoice_intelligence.application.ports.file_storage import FileStorage
from invoice_intelligence.application.ports.stored_objects import StoredObjectRepository


class StorageLifecycleService:
    def __init__(self, repository: StoredObjectRepository, storage: FileStorage) -> None:
        self._repository = repository
        self._storage = storage

    async def process_batch(
        self, worker_id: str, batch_size: int, lease_seconds: int
    ) -> int:
        records = await self._repository.claim_deletions(worker_id, batch_size, lease_seconds)
        for record in records:
            try:
                await self._storage.delete(record.storage_uri)
            except StorageObjectNotFoundError:
                pass
            except StoragePermissionDeniedError:
                await self._repository.mark_failure(
                    record.object_id,
                    worker_id,
                    record.revision,
                    "storage.permission_denied",
                    False,
                )
                continue
            except Exception:
                await self._repository.mark_failure(
                    record.object_id,
                    worker_id,
                    record.revision,
                    "storage.temporarily_unavailable",
                    True,
                )
                continue
            await self._repository.mark_deleted(
                record.object_id, worker_id, record.revision
            )
        return len(records)

