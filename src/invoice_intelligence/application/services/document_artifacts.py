"""Best-effort persistence for rebuildable document artifacts."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from invoice_intelligence.application.ports.file_storage import FileStorage
from invoice_intelligence.application.ports.stored_objects import StoredObjectRepository
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import RawOCRResult
from invoice_intelligence.domain.storage import (
    ObjectKind,
    StorageWriteRequest,
    StoredObjectRecord,
    StoredObjectStatus,
)


class DocumentArtifactService:
    def __init__(
        self,
        storage: FileStorage,
        repository: StoredObjectRepository,
        rendered_retention_days: int,
        derived_retention_days: int,
    ) -> None:
        self._storage = storage
        self._repository = repository
        self._rendered_retention_days = rendered_retention_days
        self._derived_retention_days = derived_retention_days

    async def store_rendered(
        self,
        tenant_id: str,
        document_id: str,
        images: tuple[VisionImage, ...],
        producer_version: str,
    ) -> None:
        for image in images:
            await self._store(
                tenant_id=tenant_id,
                document_id=document_id,
                kind=ObjectKind.RENDERED,
                content=image.content,
                media_type=image.mime_type,
                page_number=image.page_number,
                producer_version=producer_version,
                retention_days=self._rendered_retention_days,
            )

    async def store_ocr(
        self,
        tenant_id: str,
        document_id: str,
        results: tuple[RawOCRResult, ...],
        generation: str,
    ) -> None:
        payload = {
            "generation": generation,
            "results": [
                {
                    "status": result.status.value,
                    "observations": [
                        {
                            "source_id": item.source_id,
                            "provider_name": item.provider_name,
                            "provider_version": item.provider_version,
                            "model_version": item.model_version,
                            "page_number": item.page_number,
                            "observed_text": item.observed_text,
                            "normalized_text": item.normalized_text,
                            "anomalies": list(item.anomalies),
                        }
                        for item in result.observations
                    ],
                    "anomalies": list(result.anomalies),
                }
                for result in results
            ],
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        await self._store(
            tenant_id=tenant_id,
            document_id=document_id,
            kind=ObjectKind.DERIVED_TEXT,
            content=content,
            media_type="application/json",
            page_number=None,
            producer_version=generation,
            retention_days=self._derived_retention_days,
        )

    async def _store(
        self,
        *,
        tenant_id: str,
        document_id: str,
        kind: ObjectKind,
        content: bytes,
        media_type: str,
        page_number: int | None,
        producer_version: str,
        retention_days: int,
    ) -> None:
        identity = f"{tenant_id}:{document_id}:{kind.value}:{page_number}:{producer_version}"
        object_id = str(uuid5(NAMESPACE_URL, identity))
        checksum = sha256(content).hexdigest()
        metadata = await self._storage.save_object(
            StorageWriteRequest(
                object_id=object_id,
                tenant_id=tenant_id,
                kind=kind,
                content=content,
                media_type=media_type,
                checksum=checksum,
                page_number=page_number,
                producer_version=producer_version,
            )
        )
        await self._repository.register_available(
            StoredObjectRecord(
                object_id=object_id,
                tenant_id=tenant_id,
                parent_document_id=document_id,
                kind=kind,
                storage_uri=metadata.storage_uri,
                checksum=checksum,
                media_type=media_type,
                size_bytes=len(content),
                status=StoredObjectStatus.AVAILABLE,
                revision=1,
                retention_until=datetime.now(UTC) + timedelta(days=retention_days),
            )
        )
