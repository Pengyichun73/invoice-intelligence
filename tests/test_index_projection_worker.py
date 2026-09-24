from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from invoice_intelligence.domain.examples import IndexVersion
from invoice_intelligence.workers.index_projection import _process_tenant


@pytest.mark.asyncio
async def test_worker_records_failed_verification_without_switching_alias(caplog) -> None:
    version = IndexVersion("examples-v1")

    class ExampleService:
        def __init__(self) -> None:
            self.verified = False

        async def list_index_versions(self, tenant_id):
            return (version,)

        async def requeue_stale(self, tenant_id, index_version, stale_before):
            return 0

        async def project_pending(self, tenant_id, index_version, **kwargs):
            return SimpleNamespace(indexed=0, failed=0)

        async def verify_index_version(self, tenant_id, index_version):
            self.verified = True
            return False

    class FieldService:
        async def list_index_versions(self, tenant_id):
            return ()

    example_service = ExampleService()
    container = SimpleNamespace(
        example_index_projection_service=example_service,
        field_semantic_index_projection_service=FieldService(),
    )

    with caplog.at_level("WARNING"):
        processed = await _process_tenant(
            container,
            "tenant-a",
            "reviewed_examples",
            1,
            True,
            datetime.now(UTC),
            "worker-a",
            30.0,
        )

    assert processed == 0
    assert example_service.verified is True
    assert "verification failed" in caplog.text
    assert any(
        record.projection_status == "blocked"
        for record in caplog.records
        if record.name == "invoice_intelligence.workers.index_projection"
    )
