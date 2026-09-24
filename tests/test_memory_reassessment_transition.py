import pytest

from invoice_intelligence.application.errors import ResourceConflictError
from invoice_intelligence.application.services.memory_governance import (
    MemoryGovernanceService,
)
from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.governance import GovernanceAction


def test_only_quarantined_admissions_can_be_requeued_for_reassessment() -> None:
    MemoryGovernanceService._validate_human_admission_transition(
        MemoryAdmissionStatus.QUARANTINED,
        MemoryAdmissionStatus.PENDING,
    )
    assert (
        MemoryGovernanceService._admission_action(MemoryAdmissionStatus.PENDING)
        is GovernanceAction.REQUEUE_ADMISSION
    )
    with pytest.raises(ResourceConflictError):
        MemoryGovernanceService._validate_human_admission_transition(
            MemoryAdmissionStatus.REJECTED,
            MemoryAdmissionStatus.PENDING,
        )
