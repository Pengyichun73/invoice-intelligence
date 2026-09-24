"""File storage boundary."""

from typing import Protocol

from invoice_intelligence.domain.storage import (
    StorageObjectMetadata,
    StorageWriteRequest,
)


class FileStorage(Protocol):
    """Persist and retrieve immutable original document bytes."""

    async def save(self, document_id: str, content: bytes, mime_type: str) -> str:
        """Persist content and return an opaque storage URI."""

        ...

    async def read(self, storage_uri: str) -> bytes:
        """Read content identified by an opaque storage URI."""

        ...

    async def save_object(self, request: StorageWriteRequest) -> StorageObjectMetadata:
        """Persist one immutable object in its fixed storage class."""

        ...

    async def head(self, storage_uri: str) -> StorageObjectMetadata:
        """Return safe metadata without returning object content."""

        ...

    async def delete(self, storage_uri: str) -> None:
        """Idempotently delete an object owned by this adapter."""

        ...


class DownloadAccessIssuer(Protocol):
    """Issue short-lived access without exposing storage credentials."""

    async def issue(self, storage_uri: str, expires_seconds: int) -> str:
        """Return a non-persistent short-lived download URL."""

        ...
