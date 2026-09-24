"""Production-safe default: no code execution without an approved sandbox."""

from ...application.ports.sandbox import (
    SandboxExecutionResult,
    SandboxExecutor,
    SandboxPolicy,
)
from ...domain.errors import HarnessErrorCode
from ...domain.patch_model import PatchProposal
from ...domain.repository import RepositorySnapshot


class FailClosedSandboxExecutor(SandboxExecutor):
    """Never executes generated code or starts a local subprocess."""

    async def execute(
        self,
        *,
        snapshot: RepositorySnapshot,
        patch: PatchProposal,
        policy: SandboxPolicy,
    ) -> SandboxExecutionResult:
        return SandboxExecutionResult(
            succeeded=False,
            error_code=HarnessErrorCode.SANDBOX_NOT_CONFIGURED.value,
            diagnostics=(),
            output_checksum_sha256=None,
        )

