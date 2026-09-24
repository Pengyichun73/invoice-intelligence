"""Deterministic Patch validation boundary."""

from dataclasses import dataclass
from typing import Protocol

from ...domain.patch_model import PatchProposal
from ...domain.repository import RepositorySnapshot


@dataclass(frozen=True, slots=True)
class PatchValidationResult:
    valid: bool
    error_code: str | None
    changed_files: tuple[str, ...]
    patch_fingerprint: str
    changed_lines: int = 0
    ast_changed_files: tuple[str, ...] = ()


class PatchValidator(Protocol):
    async def validate(
        self,
        *,
        snapshot: RepositorySnapshot,
        proposal: PatchProposal,
    ) -> PatchValidationResult:
        ...
