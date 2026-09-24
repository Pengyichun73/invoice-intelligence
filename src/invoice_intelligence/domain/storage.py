"""Framework-independent object-storage value objects."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ObjectKind(StrEnum):
    ORIGINAL = "original"
    RENDERED = "rendered"
    DERIVED_TEXT = "derived_text"


class StoredObjectStatus(StrEnum):
    MIGRATION_PENDING = "migration_pending"
    PENDING = "pending"
    AVAILABLE = "available"
    DELETE_PENDING = "delete_pending"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StorageObjectMetadata:
    storage_uri: str
    kind: ObjectKind
    media_type: str
    size_bytes: int
    checksum: str
    native_checksum_verified: bool = False


@dataclass(frozen=True, slots=True)
class StorageWriteRequest:
    object_id: str
    tenant_id: str
    kind: ObjectKind
    content: bytes
    media_type: str
    checksum: str
    page_number: int | None = None
    producer_version: str | None = None


@dataclass(frozen=True, slots=True)
class StoredObjectRecord:
    object_id: str
    tenant_id: str
    parent_document_id: str | None
    kind: ObjectKind
    storage_uri: str
    checksum: str
    media_type: str
    size_bytes: int | None
    status: StoredObjectStatus
    revision: int
    retention_until: datetime | None = None
    delete_after: datetime | None = None

