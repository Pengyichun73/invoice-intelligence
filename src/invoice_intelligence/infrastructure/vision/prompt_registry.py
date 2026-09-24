"""Fail-safe prompt registry adapters."""

import logging
from collections.abc import Awaitable, Callable, Mapping

from invoice_intelligence.application.ports.prompt_registry import (
    VisionPromptRegistry,
    VisionPromptTemplate,
)

_LOGGER = logging.getLogger(__name__)
PromptPuller = Callable[[str, str], Awaitable[Mapping[str, str]]]


class LocalVisionPromptRegistry(VisionPromptRegistry):
    """Use code-owned prompts when no external registry is configured."""

    async def resolve(
        self,
        prompt_version: str,
        fallback: VisionPromptTemplate,
    ) -> VisionPromptTemplate:
        if fallback.prompt_version != prompt_version:
            raise ValueError("Local vision prompt version does not match the request")
        return fallback


class LangSmithVisionPromptRegistry(VisionPromptRegistry):
    """Optional LangSmith seam; the injected puller owns SDK-specific access."""

    def __init__(self, *, prompt_identifier: str, puller: PromptPuller) -> None:
        if not prompt_identifier.strip() or prompt_identifier != prompt_identifier.strip():
            raise ValueError("LangSmith prompt identifier must be normalized")
        self._prompt_identifier = prompt_identifier
        self._puller = puller

    async def resolve(
        self,
        prompt_version: str,
        fallback: VisionPromptTemplate,
    ) -> VisionPromptTemplate:
        try:
            payload = await self._puller(self._prompt_identifier, prompt_version)
            template = VisionPromptTemplate(
                prompt_version=payload["prompt_version"],
                system=payload["system"],
                user_instruction=payload["user_instruction"],
                mandatory_rules=payload["mandatory_rules"],
                source="langsmith",
            )
            if template.prompt_version != prompt_version:
                raise ValueError("LangSmith prompt version mismatch")
            return template
        except Exception as exc:
            _LOGGER.warning(
                "vision_prompt_registry_fallback",
                extra={
                    "provider": "langsmith",
                    "prompt_version": prompt_version,
                    "error_type": type(exc).__name__,
                },
            )
            return fallback
