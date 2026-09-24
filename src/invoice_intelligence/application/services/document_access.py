"""Tenant-scoped document download access."""

from dataclasses import dataclass
from typing import Protocol

from invoice_intelligence.application.errors import ResourceNotFoundError
from invoice_intelligence.application.ports.document_repository import DocumentReferenceRepository
from invoice_intelligence.application.ports.file_storage import DownloadAccessIssuer, FileStorage


class DownloadAccessVerifier(Protocol):
    async def verify(self, token: str, expected_storage_uri: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DownloadContent:
    content: bytes
    media_type: str


class DocumentAccessService:
    def __init__(
        self,
        repository: DocumentReferenceRepository,
        storage: FileStorage,
        issuer: DownloadAccessIssuer,
        ttl_seconds: int,
        verifier: DownloadAccessVerifier | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._issuer = issuer
        self._ttl_seconds = ttl_seconds
        self._verifier = verifier

    async def create_download_url(self, document_id: str, tenant_id: str) -> tuple[str, int]:
        document = await self._get(document_id, tenant_id)
        metadata = await self._storage.head(document.storage_uri)
        if metadata.checksum != document.checksum:
            raise ResourceNotFoundError("Document content is not available")
        return await self._issuer.issue(document.storage_uri, self._ttl_seconds), self._ttl_seconds

    async def read_local_content(
        self, document_id: str, tenant_id: str, token: str
    ) -> DownloadContent:
        if self._verifier is None:
            raise ResourceNotFoundError("Local content endpoint is not enabled")
        document = await self._get(document_id, tenant_id)
        await self._verifier.verify(token, document.storage_uri)
        content = await self._storage.read(document.storage_uri)
        return DownloadContent(content=content, media_type=document.mime_type)

    async def _get(self, document_id: str, tenant_id: str):
        document = await self._repository.get_document(document_id, tenant_id)
        if document is None or document.storage_status != "available":
            raise ResourceNotFoundError("Document does not exist")
        return document

