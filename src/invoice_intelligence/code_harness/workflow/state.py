"""Bounded state for the fixed Harness workflow."""

from dataclasses import dataclass
from enum import StrEnum

from ..domain.errors import HarnessErrorCode
from ..domain.execution import ExecutionBudget
from ..domain.execution import HarnessTaskStatus
from ..domain.versions import ExecutionVersionBinding


class HarnessStage(StrEnum):
    PREPARE_TASK = "prepare_task"
    INSPECT_REPOSITORY = "inspect_repository"
    RETRIEVE_CODE_CONTEXT = "retrieve_code_context"
    GENERATE_PATCH = "generate_patch"
    VALIDATE_PATCH = "validate_patch"
    EXECUTE_IN_SANDBOX = "execute_in_sandbox"
    EVALUATE_RESULT = "evaluate_result"
    REPAIR_OR_FINISH = "repair_or_finish"


@dataclass(frozen=True, slots=True)
class HarnessState:
    task_id: str
    tenant_id: str
    trace_id: str | None
    repository_id: str
    snapshot_id: str | None
    snapshot_revision: int | None
    versions: ExecutionVersionBinding
    budget: ExecutionBudget
    attempt_count: int
    stage: HarnessStage
    source_revision: str | None = None
    status: HarnessTaskStatus = HarnessTaskStatus.PENDING
    context_refs: tuple[str, ...] = ()
    patch_id: str | None = None
    affected_language: str | None = None
    affected_symbol_kind: str | None = None
    patch_shape: str | None = None
    error_code: HarnessErrorCode | None = None
    error_signature: str | None = None
    prior_patch_fingerprints: tuple[str, ...] = ()
    error_signatures: tuple[str, ...] = ()
    changed_ast_fingerprint: bool = False
    changed_symbols: bool = False
    changed_diagnostics: bool = False

    def next(self, stage: HarnessStage, **changes: object) -> "HarnessState":
        values = {
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "repository_id": self.repository_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_revision": self.snapshot_revision,
            "source_revision": self.source_revision,
            "versions": self.versions,
            "budget": self.budget,
            "attempt_count": self.attempt_count,
            "stage": stage,
            "status": self.status,
            "context_refs": self.context_refs,
            "patch_id": self.patch_id,
            "affected_language": self.affected_language,
            "affected_symbol_kind": self.affected_symbol_kind,
            "patch_shape": self.patch_shape,
            "error_code": self.error_code,
            "error_signature": self.error_signature,
            "prior_patch_fingerprints": self.prior_patch_fingerprints,
            "error_signatures": self.error_signatures,
            "changed_ast_fingerprint": self.changed_ast_fingerprint,
            "changed_symbols": self.changed_symbols,
            "changed_diagnostics": self.changed_diagnostics,
        }
        values.update(changes)
        return HarnessState(**values)
