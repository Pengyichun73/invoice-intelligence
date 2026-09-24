"""Stable, non-sensitive Harness failure codes."""

from enum import StrEnum


class HarnessErrorCode(StrEnum):
    TASK_NOT_FOUND = "task_not_found"
    TENANT_SCOPE_MISMATCH = "tenant_scope_mismatch"
    PATH_OUTSIDE_REPOSITORY = "path_outside_repository"
    SNAPSHOT_CAPTURE_CONFLICT = "snapshot_capture_conflict"
    SNAPSHOT_NOT_FOUND = "snapshot_not_found"
    REVISION_CONFLICT = "revision_conflict"
    LEASE_FENCING_REJECTED = "lease_fencing_rejected"
    PATCH_INVALID = "patch_invalid"
    PATCH_SYNTAX_INVALID = "patch_syntax_invalid"
    PATCH_AST_INVALID = "patch_ast_invalid"
    PATCH_OVERLAP = "patch_overlap"
    PATCH_BASE_CHECKSUM_MISMATCH = "patch_base_checksum_mismatch"
    PARSER_NOT_CONFIGURED = "parser_not_configured"
    MODEL_PROVIDER_NOT_CONFIGURED = "model_provider_not_configured"
    INDEX_PROVIDER_NOT_CONFIGURED = "index_provider_not_configured"
    CODE_INDEX_UNAVAILABLE = "code_index_unavailable"
    CODE_INDEX_MISCONFIGURED = "code_index_misconfigured"
    SANDBOX_NOT_CONFIGURED = "sandbox_not_configured"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HARD_FAILURE = "hard_failure"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    REPAIR_STALLED = "repair_stalled"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class HarnessError(Exception):
    """Base error carrying a stable safe code."""

    def __init__(self, code: HarnessErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code


def require_text(name: str, value: str, *, max_length: int = 256) -> None:
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{name} must be normalized and at most {max_length} characters")


def require_sha256(name: str, value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
