"""OpenAI Responses API structured vision extraction adapter."""

import base64
import logging
from collections.abc import Sequence
from typing import Any, Literal, TypeVar
from uuid import uuid4

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from invoice_intelligence.application.errors import VisionExtractionError
from invoice_intelligence.application.ports.prompt_registry import VisionPromptRegistry
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import ExtractionResult, VisionPromptContext
from invoice_intelligence.infrastructure.remote.safety import (
    RemoteCallSafety,
    RemoteCircuitOpenError,
)
from invoice_intelligence.infrastructure.vision.structured import (
    build_extraction_result,
    build_vision_prompt_set,
    build_vision_response_model,
    compile_prompt_context_sections,
    render_untrusted_prompt_section,
    schema_retry_instruction,
    validation_error_diagnostics,
    vision_error_diagnostics,
)

InvoiceT = TypeVar("InvoiceT")
_LOGGER = logging.getLogger(__name__)
_MAX_SCHEMA_DIAGNOSTICS = 20


class _StructuredResponseError(Exception):
    """Internal response-contract failure containing only safe diagnostics."""

    def __init__(self, diagnostics: Sequence[str]) -> None:
        normalized = tuple(diagnostics[:_MAX_SCHEMA_DIAGNOSTICS])
        super().__init__("OpenAI structured response validation failed")
        self.diagnostics = normalized


class OpenAIVisionExtractionProvider:
    """Use official Responses Structured Outputs with a supplied Pydantic schema."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        schema_max_retries: int,
        image_detail: Literal["auto", "high", "low", "original"],
        max_concurrency: int,
        requests_per_minute: int,
        circuit_failure_threshold: int,
        circuit_recovery_seconds: float,
        prompt_version: str = "invoice-vision-extraction-v2",
        prompt_registry: VisionPromptRegistry | None = None,
    ) -> None:
        if schema_max_retries < 0:
            raise ValueError("OpenAI vision Schema retries must not be negative")
        self._model = model
        self._image_detail = image_detail
        self._schema_max_retries = schema_max_retries
        self._prompt_set = build_vision_prompt_set(prompt_version)
        self._prompt_registry = prompt_registry
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self._remote_safety = RemoteCallSafety(
            max_concurrency=max_concurrency,
            requests_per_minute=requests_per_minute,
            circuit_failure_threshold=circuit_failure_threshold,
            circuit_recovery_seconds=circuit_recovery_seconds,
        )

    async def extract(
        self,
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
        prompt_context: VisionPromptContext,
        document_id: str,
    ) -> ExtractionResult[InvoiceT]:
        """Parse one or more images into the exact caller-supplied schema."""

        if not images:
            raise VisionExtractionError("At least one vision image is required")
        response_model = build_vision_response_model(output_schema)
        prompt_set = (
            await self._prompt_registry.resolve(
                self._prompt_set.prompt_version,
                self._prompt_set,
            )
            if self._prompt_registry is not None
            else self._prompt_set
        )
        image_content: list[dict[str, Any]] = []
        for image in images:
            if image.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise VisionExtractionError("Provider received an unsupported image MIME type")
            encoded = base64.b64encode(image.content).decode("ascii")
            image_content.append(
                {
                    "type": "input_text",
                    "text": f"Document page {image.page_number}",
                }
            )
            image_content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{image.mime_type};base64,{encoded}",
                    "detail": self._image_detail,
                }
            )

        context_content = [
            {
                "type": "input_text",
                "text": render_untrusted_prompt_section(section_name, payload),
            }
            for section_name, payload in compile_prompt_context_sections(prompt_context)
        ]
        user_content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt_set.user_instruction},
            *image_content,
            {
                "type": "input_text",
                "text": "MANDATORY_BUSINESS_RULES\n" + prompt_set.mandatory_rules,
            },
            *context_content,
        ]
        trace_id = uuid4().hex
        max_attempts = self._schema_max_retries + 1
        last_error: _StructuredResponseError | None = None
        for attempt in range(1, max_attempts + 1):
            attempt_content = list(user_content)
            if last_error is not None:
                attempt_content.append(
                    {
                        "type": "input_text",
                        "text": schema_retry_instruction(
                            last_error.diagnostics,
                            prompt_version=prompt_set.prompt_version,
                        ),
                    }
                )
            request_input: Any = [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": prompt_set.system}],
                },
                {"role": "user", "content": attempt_content},
            ]
            try:
                async with self._remote_safety.call(
                    provider="openai",
                    operation="vision_extraction",
                    model=self._model,
                ):
                    response = await self._client.responses.parse(
                        model=self._model,
                        input=request_input,
                        text_format=response_model,
                        store=False,
                    )
            except RemoteCircuitOpenError as exc:
                raise VisionExtractionError(
                    "OpenAI vision extraction is temporarily unavailable",
                    reason_code="vision.openai.circuit_open",
                    trace_id=trace_id,
                ) from exc
            except ValidationError as exc:
                last_error = _StructuredResponseError(
                    validation_error_diagnostics(exc)
                )
            except OpenAIError as exc:
                raise VisionExtractionError(
                    "OpenAI vision extraction request failed",
                    reason_code="vision.openai.remote_error",
                    trace_id=trace_id,
                ) from exc
            else:
                try:
                    return self._parse_response(
                        response.output_parsed,
                        images,
                        document_id,
                    )
                except _StructuredResponseError as exc:
                    last_error = exc

            if last_error is None:
                raise RuntimeError("OpenAI Schema retry state was not initialized")
            exhausted = attempt >= max_attempts
            _LOGGER.warning(
                "openai_vision_schema_validation",
                extra={
                    "provider": "openai",
                    "trace_id": trace_id,
                    "operation": "vision_extraction",
                    "model": self._model,
                    "outcome": "exhausted" if exhausted else "retry",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "schema_diagnostics": list(last_error.diagnostics),
                },
            )
            if exhausted:
                raise VisionExtractionError(
                    "OpenAI structured output exhausted the Schema retry budget",
                    reason_code="vision.openai.schema_retry_exhausted",
                    trace_id=trace_id,
                ) from last_error
        raise VisionExtractionError(
            "OpenAI structured output exhausted the Schema retry budget",
            reason_code="vision.openai.schema_retry_exhausted",
            trace_id=trace_id,
        )

    @staticmethod
    def _parse_response(
        parsed: BaseModel | None,
        images: Sequence[VisionImage],
        document_id: str,
    ) -> ExtractionResult[InvoiceT]:
        if parsed is None:
            raise _StructuredResponseError(("content:missing",))
        try:
            return build_extraction_result(
                parsed,
                images,
                document_id=document_id,
                provider_name="OpenAI",
            )
        except VisionExtractionError as exc:
            raise _StructuredResponseError(vision_error_diagnostics(exc)) from exc
