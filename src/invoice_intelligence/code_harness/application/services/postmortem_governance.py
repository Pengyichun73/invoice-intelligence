"""Application service for tenant-scoped postmortem governance."""

from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.postmortem import AdmissionStatus, Postmortem
from ...domain.tenant_boundary import HarnessTenantContext
from ..ports.postmortem import PostmortemRepository


class PostmortemGovernanceService:
    """Keep postmortem reads and admission transitions behind one trust boundary."""

    def __init__(self, repository: PostmortemRepository) -> None:
        self._repository = repository

    async def get(
        self,
        *,
        context: HarnessTenantContext,
        postmortem_id: str,
    ) -> Postmortem:
        postmortem = await self._repository.get(
            tenant_id=context.tenant_id,
            postmortem_id=postmortem_id,
        )
        if postmortem is None:
            raise HarnessError(HarnessErrorCode.TASK_NOT_FOUND, "postmortem not found")
        context.assert_tenant(postmortem.tenant_id)
        return postmortem

    async def change_admission(
        self,
        *,
        context: HarnessTenantContext,
        postmortem_id: str,
        expected_revision: int,
        status: AdmissionStatus,
        reason_code: str,
    ) -> Postmortem:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if not reason_code or reason_code != reason_code.strip():
            raise ValueError("reason_code must be normalized")
        current = await self.get(context=context, postmortem_id=postmortem_id)
        if current.revision != expected_revision:
            raise HarnessError(HarnessErrorCode.REVISION_CONFLICT)
        if status is AdmissionStatus.PENDING:
            raise ValueError("pending is not an actionable admission transition")
        return await self._repository.set_admission_status(
            postmortem_id=current.postmortem_id,
            expected_revision=expected_revision,
            status=status,
            actor_id=context.actor_id,
            reason_code=reason_code,
        )
