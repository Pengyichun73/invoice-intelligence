"""Fail-closed sandbox boundary."""

from dataclasses import dataclass
from typing import Protocol

from ...domain.patch_model import PatchProposal
from ...domain.repository import RepositorySnapshot


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    policy_version: str
    max_wall_time_seconds: int
    max_cpu_seconds: int
    max_memory_bytes: int
    max_disk_bytes: int
    network_enabled: bool = False


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    succeeded: bool
    error_code: str | None
    diagnostics: tuple[str, ...]
    output_checksum_sha256: str | None


class SandboxExecutor(Protocol):
    async def execute(
        self,
        *,
        snapshot: RepositorySnapshot,
        patch: PatchProposal,
        policy: SandboxPolicy,
    ) -> SandboxExecutionResult:
        ...

