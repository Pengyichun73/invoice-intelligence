"""Fail-closed static capability allowlist for built-in adapters."""

from types import MappingProxyType

from invoice_intelligence.application.ports.capabilities import (
    ProviderCapability,
    ProviderCapabilityManifest,
)
from invoice_intelligence.infrastructure.embeddings.openai import OpenAIEmbeddingProvider
from invoice_intelligence.infrastructure.indexing.milvus_examples import (
    MilvusExampleIndexStore,
)
from invoice_intelligence.infrastructure.indexing.milvus_field_semantics import (
    MilvusFieldSemanticIndexStore,
)
from invoice_intelligence.infrastructure.ocr.paddlex_http import PaddleXOCRHttpAdapter
from invoice_intelligence.infrastructure.qwen.embeddings import QwenDenseEmbeddingProvider
from invoice_intelligence.infrastructure.qwen.memory_quality import (
    QwenMemoryQualityAssessmentProvider,
)
from invoice_intelligence.infrastructure.qwen.query_rewrite import QwenQueryRewriteProvider
from invoice_intelligence.infrastructure.qwen.reranking import QwenRerankingProvider
from invoice_intelligence.infrastructure.qwen.vision import QwenVisionExtractionProvider
from invoice_intelligence.infrastructure.vision.openai import OpenAIVisionExtractionProvider


def _manifest(
    provider_name: str,
    *capabilities: ProviderCapability,
    accepts_untrusted_content: bool = False,
    requires_redacted_input: bool = True,
    advisory_only: bool = False,
) -> ProviderCapabilityManifest:
    return ProviderCapabilityManifest(
        provider_name=provider_name,
        allowed_capabilities=frozenset(capabilities),
        network_access=True,
        accepts_untrusted_content=accepts_untrusted_content,
        requires_redacted_input=requires_redacted_input,
        advisory_only=advisory_only,
    )


BUILTIN_PROVIDER_CAPABILITIES = MappingProxyType(
    {
        OpenAIVisionExtractionProvider: _manifest(
            "openai_vision",
            ProviderCapability.VISION_EXTRACTION,
            accepts_untrusted_content=True,
            requires_redacted_input=False,
        ),
        QwenVisionExtractionProvider: _manifest(
            "qwen_vision",
            ProviderCapability.VISION_EXTRACTION,
            accepts_untrusted_content=True,
            requires_redacted_input=False,
        ),
        PaddleXOCRHttpAdapter: _manifest(
            "paddlex_ocr_http",
            ProviderCapability.OCR_OBSERVATION,
            accepts_untrusted_content=True,
            requires_redacted_input=False,
        ),
        OpenAIEmbeddingProvider: _manifest(
            "openai_embedding",
            ProviderCapability.DENSE_EMBEDDING,
        ),
        QwenDenseEmbeddingProvider: _manifest(
            "qwen_embedding",
            ProviderCapability.DENSE_EMBEDDING,
        ),
        QwenRerankingProvider: _manifest(
            "qwen_reranking",
            ProviderCapability.RERANKING,
        ),
        QwenQueryRewriteProvider: _manifest(
            "qwen_query_rewrite",
            ProviderCapability.QUERY_REWRITE,
            advisory_only=True,
        ),
        QwenMemoryQualityAssessmentProvider: _manifest(
            "qwen_memory_quality",
            ProviderCapability.MEMORY_QUALITY_ADVISORY,
            accepts_untrusted_content=True,
            advisory_only=True,
        ),
        MilvusExampleIndexStore: _manifest(
            "milvus_example_index",
            ProviderCapability.DERIVED_INDEX_READ,
            ProviderCapability.DERIVED_INDEX_WRITE,
        ),
        MilvusFieldSemanticIndexStore: _manifest(
            "milvus_field_semantic_index",
            ProviderCapability.DERIVED_INDEX_READ,
            ProviderCapability.DERIVED_INDEX_WRITE,
        ),
    }
)


def require_builtin_capability(
    adapter: object,
    capability: ProviderCapability,
) -> ProviderCapabilityManifest:
    """Reject an unregistered built-in adapter or an excessive use at startup."""

    manifest = BUILTIN_PROVIDER_CAPABILITIES.get(type(adapter))
    if manifest is None:
        raise ValueError(
            f"Built-in provider type {type(adapter).__name__} is not capability-registered"
        )
    if capability not in manifest.allowed_capabilities:
        raise ValueError(
            f"Provider {manifest.provider_name} does not allow capability {capability.value}"
        )
    return manifest
