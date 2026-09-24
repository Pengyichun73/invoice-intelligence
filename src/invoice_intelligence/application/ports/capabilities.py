"""Static capability declarations for infrastructure adapters."""

from dataclasses import dataclass
from enum import StrEnum


class ProviderCapability(StrEnum):
    """Positive capabilities that may cross an Application Port."""

    VISION_EXTRACTION = "vision_extraction"
    OCR_OBSERVATION = "ocr_observation"
    DENSE_EMBEDDING = "dense_embedding"
    RERANKING = "reranking"
    QUERY_REWRITE = "query_rewrite"
    MEMORY_QUALITY_ADVISORY = "memory_quality_advisory"
    DERIVED_INDEX_READ = "derived_index_read"
    DERIVED_INDEX_WRITE = "derived_index_write"


@dataclass(frozen=True, slots=True)
class ProviderCapabilityManifest:
    """Auditable, code-owned least-privilege declaration for one adapter."""

    provider_name: str
    allowed_capabilities: frozenset[ProviderCapability]
    network_access: bool
    accepts_untrusted_content: bool
    requires_redacted_input: bool
    advisory_only: bool = False
    may_persist_business_facts: bool = False
    may_route_workflow: bool = False
    may_approve_governance: bool = False
    may_mutate_tenant_scope: bool = False
    may_mutate_entity_schema: bool = False
    may_execute_dynamic_tools: bool = False

    def __post_init__(self) -> None:
        if not self.provider_name.strip() or self.provider_name != self.provider_name.strip():
            raise ValueError("Provider capability name must be non-empty and normalized")
        if not self.allowed_capabilities:
            raise ValueError("Provider capability manifest must allow at least one capability")
        forbidden = (
            self.may_persist_business_facts,
            self.may_route_workflow,
            self.may_approve_governance,
            self.may_mutate_tenant_scope,
            self.may_mutate_entity_schema,
            self.may_execute_dynamic_tools,
        )
        if any(forbidden):
            raise ValueError("Infrastructure providers cannot receive privileged capabilities")
