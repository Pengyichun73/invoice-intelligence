"""Patch validation against an immutable snapshot manifest."""

import ast
from collections.abc import Mapping
from hashlib import sha256

from ...application.ports.patch_validator import PatchValidationResult
from ...domain.errors import HarnessErrorCode
from ...domain.patch_model import PatchOperation, PatchOperationKind, PatchProposal
from ...domain.repository import RepositorySnapshot
from .path_policy import validate_relative_path


def validate_patch(
    *,
    snapshot: RepositorySnapshot,
    proposal: PatchProposal,
    contents: Mapping[str, bytes] | None = None,
    max_patch_bytes: int = 256_000,
    max_changed_files: int = 20,
    max_changed_lines: int = 1_000,
    max_patch_operations: int = 100,
    allow_create_files: bool = False,
    allow_delete_files: bool = False,
) -> PatchValidationResult:
    if (
        proposal.tenant_id != snapshot.tenant_id
        or proposal.repository_id != snapshot.repository_id
        or proposal.snapshot_id != snapshot.snapshot_id
        or proposal.snapshot_revision != snapshot.revision
    ):
        return _invalid(HarnessErrorCode.REVISION_CONFLICT.value, proposal)

    files = {item.path: item for item in snapshot.files}
    if len(files_for(proposal)) > max_changed_files:
        return _invalid(HarnessErrorCode.RESOURCE_EXHAUSTED.value, proposal)
    if len(proposal.operations) > max_patch_operations:
        return _invalid(HarnessErrorCode.RESOURCE_EXHAUSTED.value, proposal)
    if sum(len(operation.payload_utf8) for operation in proposal.operations) > max_patch_bytes:
        return _invalid(HarnessErrorCode.RESOURCE_EXHAUSTED.value, proposal)
    if proposal.patch_checksum_sha256 != _patch_checksum(proposal):
        return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
    ranges: dict[str, list[tuple[int, int]]] = {}
    changed_lines = 0
    operations_by_path: dict[str, list[PatchOperation]] = {}
    ast_changed_files: list[str] = []
    for operation in proposal.operations:
        try:
            path = validate_relative_path(operation.path)
        except Exception:
            return _invalid(HarnessErrorCode.PATH_OUTSIDE_REPOSITORY.value, proposal)
        file = files.get(path)
        if operation.kind is PatchOperationKind.CREATE_FILE:
            if not allow_create_files or file is not None:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
            if operation.base_checksum_sha256 != sha256(b"").hexdigest():
                return _invalid(HarnessErrorCode.PATCH_BASE_CHECKSUM_MISMATCH.value, proposal)
        elif file is None or operation.base_checksum_sha256 != file.checksum_sha256:
            return _invalid(HarnessErrorCode.PATCH_BASE_CHECKSUM_MISMATCH.value, proposal)
        if operation.kind is PatchOperationKind.DELETE_FILE:
            if not allow_delete_files or file is None:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
            if operation.start_byte != 0 or operation.end_byte != file.size_bytes:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
        elif file is not None and file.kind.value != "text":
            return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
        if path != operation.path:
            return _invalid(HarnessErrorCode.PATH_OUTSIDE_REPOSITORY.value, proposal)
        if file is not None and (
            operation.end_byte > file.size_bytes or operation.start_byte > file.size_bytes
        ):
            return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
        changed_lines += _operation_line_cost(
            operation,
            contents.get(path),
            file.size_bytes if file is not None else 0,
        )
        if operation.kind in {
            PatchOperationKind.INSERT_BEFORE,
            PatchOperationKind.INSERT_AFTER,
        } and contents is not None:
            original = contents.get(path)
            if original is None or operation.anchor.encode("utf-8") not in original:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
            if original.count(operation.anchor.encode("utf-8")) != 1:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
        if _is_protected_path(path):
            return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
        if operation.kind in {
            PatchOperationKind.REPLACE,
            PatchOperationKind.DELETE_FILE,
        }:
            existing = ranges.setdefault(path, [])
            if any(
                operation.start_byte < end and start < operation.end_byte
                for start, end in existing
            ):
                return _invalid(HarnessErrorCode.PATCH_OVERLAP.value, proposal)
            existing.append((operation.start_byte, operation.end_byte))
        operations_by_path.setdefault(path, []).append(operation)

    if changed_lines > max_changed_lines:
        return _invalid(HarnessErrorCode.RESOURCE_EXHAUSTED.value, proposal)

    for path, operations in operations_by_path.items():
        original = contents.get(path) if contents is not None else None
        if any(operation.kind is PatchOperationKind.CREATE_FILE for operation in operations):
            if len(operations) != 1 or original is not None:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
            patched = operations[0].payload_utf8
        elif any(operation.kind is PatchOperationKind.DELETE_FILE for operation in operations):
            if len(operations) != 1 or original is None:
                return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
            patched = b""
        else:
            patched = _apply_operations(operations=operations, original=original)
        if patched is None:
            return _invalid(HarnessErrorCode.PATCH_INVALID.value, proposal)
        if not path.lower().endswith(".py") or not patched:
            continue
        try:
            patched_text = patched.decode("utf-8")
            patched_tree = ast.parse(patched_text, filename=path)
        except (UnicodeDecodeError, SyntaxError):
            # Existing incomplete files may continue through tolerant analysis.
            if original is None or _is_valid_python(original, path):
                return _invalid(HarnessErrorCode.PATCH_SYNTAX_INVALID.value, proposal)
            continue
        if original is not None and _is_valid_python(original, path):
            try:
                original_tree = ast.parse(original.decode("utf-8"), filename=path)
            except (UnicodeDecodeError, SyntaxError):
                return _invalid(HarnessErrorCode.PATCH_AST_INVALID.value, proposal)
            if ast.dump(original_tree, include_attributes=False) != ast.dump(
                patched_tree,
                include_attributes=False,
            ):
                ast_changed_files.append(path)

    changed_files = tuple(sorted(files_for(proposal)))
    return PatchValidationResult(
        valid=True,
        error_code=None,
        changed_files=changed_files,
        patch_fingerprint=proposal.fingerprint,
        changed_lines=changed_lines,
        ast_changed_files=tuple(sorted(ast_changed_files)),
    )


def files_for(proposal: PatchProposal) -> set[str]:
    return {operation.path for operation in proposal.operations}


def _invalid(code: str, proposal: PatchProposal) -> PatchValidationResult:
    return PatchValidationResult(
        valid=False,
        error_code=code,
        changed_files=tuple(sorted(files_for(proposal))),
        patch_fingerprint=proposal.fingerprint,
    )


def _operation_line_cost(
    operation: PatchOperation,
    original: bytes | None,
    file_size: int,
) -> int:
    payload_lines = max(1, operation.payload_utf8.count(b"\n") + 1)
    if original is None or operation.kind is PatchOperationKind.CREATE_FILE:
        return payload_lines
    start = min(operation.start_byte, file_size)
    end = min(operation.end_byte, file_size)
    if operation.kind in {
        PatchOperationKind.INSERT_BEFORE,
        PatchOperationKind.INSERT_AFTER,
    } and operation.anchor:
        anchor = operation.anchor.encode("utf-8")
        anchor_start = original.find(anchor)
        if anchor_start >= 0:
            start = anchor_start
            end = anchor_start + len(anchor)
    original_lines = max(1, original[start:end].count(b"\n") + 1)
    return max(original_lines, payload_lines)


def _apply_operations(
    *,
    operations: list[PatchOperation],
    original: bytes | None,
) -> bytes | None:
    if original is None:
        return None
    replacements: list[tuple[int, int, bytes]] = []
    insertions: list[tuple[int, bytes]] = []
    replacement_ranges: list[tuple[int, int]] = []
    insertion_positions: set[int] = set()

    for operation in operations:
        if operation.kind in {
            PatchOperationKind.REPLACE,
            PatchOperationKind.DELETE_FILE,
        }:
            if operation.end_byte > len(original):
                return None
            if any(
                operation.start_byte < end and start < operation.end_byte
                for start, end in replacement_ranges
            ):
                return None
            replacement_ranges.append((operation.start_byte, operation.end_byte))
            replacements.append(
                (operation.start_byte, operation.end_byte, operation.payload_utf8)
            )
            continue

        anchor = operation.anchor.encode("utf-8") if operation.anchor else b""
        position = original.find(anchor)
        if position < 0 or original.find(anchor, position + 1) >= 0:
            return None
        insert_at = (
            position
            if operation.kind is PatchOperationKind.INSERT_BEFORE
            else position + len(anchor)
        )
        if insert_at in insertion_positions:
            return None
        if any(start < insert_at < end for start, end in replacement_ranges):
            return None
        insertion_positions.add(insert_at)
        insertions.append((insert_at, operation.payload_utf8))

    for insert_at, _ in insertions:
        if any(insert_at == start or insert_at == end for start, end in replacement_ranges):
            return None

    edits: list[tuple[int, int, bytes]] = [
        *replacements,
        *((position, position, payload) for position, payload in insertions),
    ]
    edits.sort(key=lambda item: (item[0], item[1]))
    result = bytearray()
    cursor = 0
    for start, end, payload in edits:
        if start < cursor:
            return None
        result.extend(original[cursor:start])
        result.extend(payload)
        cursor = end
    result.extend(original[cursor:])
    return bytes(result)


def _is_valid_python(source: bytes, path: str) -> bool:
    try:
        ast.parse(source.decode("utf-8"), filename=path)
    except (UnicodeDecodeError, SyntaxError):
        return False
    return True


def _patch_checksum(proposal: PatchProposal) -> str:
    canonical = "\n".join(
        "\0".join(
            (
                operation.path,
                operation.kind.value,
                str(operation.start_byte),
                str(operation.end_byte),
                operation.base_checksum_sha256,
                operation.anchor or "",
                sha256(operation.payload_utf8).hexdigest(),
            )
        )
        for operation in proposal.operations
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _is_protected_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        name in {".env", ".env.local", ".env.production", "id_rsa"}
        or normalized.startswith(".github/workflows/")
        or normalized.endswith((".pem", ".key", ".p12", ".pfx"))
    )
