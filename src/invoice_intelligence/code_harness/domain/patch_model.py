"""Structured, snapshot-bound Patch proposals."""

from dataclasses import dataclass
from enum import StrEnum

from .errors import require_sha256, require_text


class PatchOperationKind(StrEnum):
    REPLACE = "replace"
    INSERT_BEFORE = "insert_before"
    INSERT_AFTER = "insert_after"
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"


@dataclass(frozen=True, slots=True)
class PatchOperation:
    path: str
    kind: PatchOperationKind
    start_byte: int
    end_byte: int
    payload_utf8: bytes
    base_checksum_sha256: str
    anchor: str | None = None

    def __post_init__(self) -> None:
        require_text("path", self.path, max_length=4096)
        require_sha256("base_checksum_sha256", self.base_checksum_sha256)
        if self.start_byte < 0 or self.end_byte < self.start_byte:
            raise ValueError("Patch byte range is invalid")
        if self.kind is PatchOperationKind.REPLACE and self.start_byte == self.end_byte:
            raise ValueError("replace requires a non-empty range")
        if self.kind in {
            PatchOperationKind.INSERT_BEFORE,
            PatchOperationKind.INSERT_AFTER,
        }:
            if not self.anchor:
                raise ValueError("anchored operations require an anchor")
            if self.start_byte != self.end_byte:
                raise ValueError("insert operations require an empty byte range")
        if self.kind is PatchOperationKind.CREATE_FILE and (
            self.start_byte != 0 or self.end_byte != 0
        ):
            raise ValueError("create_file must use an empty byte range")
        if self.kind is PatchOperationKind.DELETE_FILE and self.payload_utf8:
            raise ValueError("delete_file must not contain a payload")
        if self.anchor is not None:
            require_text("anchor", self.anchor, max_length=512)
        self.payload_utf8.decode("utf-8")


@dataclass(frozen=True, slots=True)
class PatchProposal:
    patch_id: str
    tenant_id: str
    repository_id: str
    snapshot_id: str
    snapshot_revision: int
    operations: tuple[PatchOperation, ...]
    patch_checksum_sha256: str
    fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "patch_id",
            "tenant_id",
            "repository_id",
            "snapshot_id",
            "fingerprint",
        ):
            require_text(name, getattr(self, name), max_length=512)
        require_sha256("patch_checksum_sha256", self.patch_checksum_sha256)
        if self.snapshot_revision < 1 or not self.operations:
            raise ValueError("Patch must bind to a positive snapshot revision and operations")
        if tuple(sorted(self.operations, key=lambda item: (item.path, -item.start_byte))) != (
            self.operations
        ):
            raise ValueError("Patch operations must be sorted by path and descending byte offset")
