"""索引激活前的派生投影完整性边界。"""

from typing import Protocol

from invoice_intelligence.domain.examples import IndexVersion


class IndexIntegrityVerifier(Protocol):
    async def verify_examples(self, tenant_id: str, index_version: IndexVersion) -> bool: ...

    async def verify_field_semantics(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        expected_sources: dict[str, str],
    ) -> bool: ...
