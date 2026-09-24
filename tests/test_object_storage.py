from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from invoice_intelligence.application.errors import StorageChecksumMismatchError
from invoice_intelligence.application.services.document_artifacts import DocumentArtifactService
from invoice_intelligence.config.settings import Settings
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.storage import ObjectKind, StorageWriteRequest
from invoice_intelligence.infrastructure.storage.local import LocalFileStorage


class _ArtifactRepository:
    def __init__(self) -> None:
        self.records = []

    async def register_available(self, record) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_local_storage_separates_kinds_and_verifies_checksum(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    content = b"%PDF-1.7\n"
    checksum = sha256(content).hexdigest()
    metadata = await storage.save_object(
        StorageWriteRequest(
            object_id=str(uuid4()),
            tenant_id="tenant-a",
            kind=ObjectKind.ORIGINAL,
            content=content,
            media_type="application/pdf",
            checksum=checksum,
        )
    )

    assert metadata.storage_uri.startswith("local://original/")
    assert metadata.checksum == checksum
    assert (await storage.head(metadata.storage_uri)).size_bytes == len(content)
    assert await storage.read(metadata.storage_uri) == content

    await storage.delete(metadata.storage_uri)
    await storage.delete(metadata.storage_uri)


@pytest.mark.asyncio
async def test_local_storage_rejects_incorrect_checksum(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(StorageChecksumMismatchError):
        await storage.save_object(
            StorageWriteRequest(
                object_id=str(uuid4()),
                tenant_id="tenant-a",
                kind=ObjectKind.DERIVED_TEXT,
                content=b"{}",
                media_type="application/json",
                checksum="0" * 64,
            )
        )


def test_object_storage_bucket_names_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="three distinct"):
        Settings(
            object_storage_originals_bucket="same",
            object_storage_rendered_bucket="same",
            object_storage_derived_text_bucket="different",
        )


@pytest.mark.asyncio
async def test_rendered_artifact_uses_independent_storage_class(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    repository = _ArtifactRepository()
    service = DocumentArtifactService(storage, repository, 7, 30)  # type: ignore[arg-type]

    await service.store_rendered(
        "tenant-a",
        str(uuid4()),
        (
            VisionImage(
                content=b"png-page",
                mime_type="image/png",
                page_number=1,
                width=10,
                height=10,
            ),
        ),
        "renderer-v1",
    )

    assert len(repository.records) == 1
    assert repository.records[0].kind is ObjectKind.RENDERED
    assert repository.records[0].storage_uri.startswith("local://rendered/")
