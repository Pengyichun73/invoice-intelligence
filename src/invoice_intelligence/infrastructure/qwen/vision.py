"""Qwen OpenAI-compatible structured vision extraction adapter."""

import base64
import json
import logging
from collections.abc import Sequence
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from invoice_intelligence.application.errors import (
    VisionExtractionError,
    VisionProviderConfigurationError,
)
from invoice_intelligence.application.ports.prompt_registry import VisionPromptRegistry
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import ExtractionResult, VisionPromptContext
from invoice_intelligence.infrastructure.qwen.client import (
    QwenRemoteAccessError,
    QwenRemoteClient,
    QwenRemoteError,
    QwenRemoteRequestError,
    QwenRemoteUnavailableError,
)
from invoice_intelligence.infrastructure.qwen.redaction import (
    QwenPayloadGuard,
    QwenPayloadPolicyError,
)
from invoice_intelligence.infrastructure.vision.structured import (
    build_extraction_result,
    build_qwen_response_format,
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
    """Internal response-contract failure containing only non-sensitive diagnostics."""

    def __init__(self, diagnostics: Sequence[str]) -> None:
        normalized = tuple(diagnostics[:_MAX_SCHEMA_DIAGNOSTICS])
        super().__init__("Qwen structured response validation failed")
        self.diagnostics = normalized


class QwenVisionExtractionProvider:
    """Extract current-image facts through Qwen JSON Schema output."""

    def __init__(
        self,
        *,
        client: QwenRemoteClient,
        payload_guard: QwenPayloadGuard,
        model: str,
        schema_max_retries: int,
        prompt_version: str = "invoice-vision-extraction-v2",
        prompt_registry: VisionPromptRegistry | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("Qwen vision model must not be empty")
        if schema_max_retries < 0:
            raise ValueError("Qwen vision Schema retries must not be negative")
        self._client = client
        self._payload_guard = payload_guard
        self._model = model
        self._schema_max_retries = schema_max_retries
        self._prompt_set = build_vision_prompt_set(prompt_version)
        self._prompt_registry = prompt_registry

    async def extract(
        self,
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
        prompt_context: VisionPromptContext,
        document_id: str,
    ) -> ExtractionResult[InvoiceT]:
        """Send transient page images and parse the exact supplied business Schema."""

        trace_id = uuid4().hex
        try:
            self._payload_guard.validate_vision_images(images)
        except QwenPayloadPolicyError as exc:
            raise VisionProviderConfigurationError(
                str(exc),
                reason_code="vision.qwen.payload_policy",
                trace_id=trace_id,
            ) from exc
        response_model = build_vision_response_model(output_schema)
        response_format = build_qwen_response_format(output_schema)
        prompt_set = (
            await self._prompt_registry.resolve(
                self._prompt_set.prompt_version,
                self._prompt_set,
            )
            if self._prompt_registry is not None
            else self._prompt_set
        )
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    prompt_set.user_instruction
                ),
            }
        ]
        for image in images:
            encoded = base64.b64encode(image.content).decode("ascii")
            user_content.extend(
                (
                    {"type": "text", "text": f"Document page {image.page_number}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.mime_type};base64,{encoded}",
                        },
                    },
                )
            )

        user_content.append(
            {
                "type": "text",
                "text": "MANDATORY_BUSINESS_RULES\n" + prompt_set.mandatory_rules,
            }
        )

        prepared_context = self._payload_guard.prepare_vision_prompt_context(
            prompt_context
        )
        for section_name, serialized_examples in compile_prompt_context_sections(
            prepared_context
        ):
            self._payload_guard.validate_redacted_text(
                serialized_examples,
                field_name=section_name,
            )
            user_content.append(
                {
                    "type": "text",
                    "text": (
                        render_untrusted_prompt_section(
                            section_name,
                            serialized_examples,
                        )
                    ),
                }
            )
        max_attempts = self._schema_max_retries + 1
        last_error: _StructuredResponseError | None = None
        for attempt in range(1, max_attempts + 1):
            attempt_content = list(user_content)
            if last_error is not None:
                attempt_content.append(
                    {
                        "type": "text",
                        "text": schema_retry_instruction(
                            last_error.diagnostics,
                            prompt_version=prompt_set.prompt_version,
                        ),
                    }
                )
            try:
                completion = await self._client.create_chat(
                    operation="vision_extraction",
                    model=self._model,
                    messages=(
                        {"role": "system", "content": prompt_set.system},
                        {"role": "user", "content": attempt_content},
                    ),
                    response_format=response_format,
                    trace_id=trace_id,
                )
            except QwenRemoteError as exc:
                reason_code = self._remote_reason_code(exc)
                _LOGGER.warning(
                    "qwen_vision_request_failed",
                    extra={
                        "provider": "qwen",
                        "trace_id": trace_id,
                        "operation": "vision_extraction",
                        "model": self._model,
                        "outcome": reason_code,
                        "error_type": type(exc).__name__,
                        "document_id": document_id,
                    },
                )
                raise VisionExtractionError(
                    "Qwen vision extraction request failed",
                    reason_code=reason_code,
                    trace_id=trace_id,
                ) from exc
            try:
                return self._parse_completion(
                    completion,
                    response_model,
                    images,
                    document_id,
                )
            except _StructuredResponseError as exc:
                last_error = exc
                exhausted = attempt >= max_attempts
                _LOGGER.warning(
                    "qwen_vision_schema_validation",
                    extra={
                        "provider": "qwen",
                        "trace_id": trace_id,
                        "operation": "vision_extraction",
                        "model": self._model,
                        "outcome": "exhausted" if exhausted else "retry",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "schema_diagnostics": list(exc.diagnostics),
                    },
                )
                if exhausted:
                    raise VisionExtractionError(
                        "Qwen structured output exhausted the Schema retry budget",
                        reason_code="vision.qwen.schema_retry_exhausted",
                        trace_id=trace_id,
                    ) from exc
        raise VisionExtractionError(
            "Qwen structured output exhausted the Schema retry budget",
            reason_code="vision.qwen.schema_retry_exhausted",
            trace_id=trace_id,
        )

    @staticmethod
    def _remote_reason_code(error: QwenRemoteError) -> str:
        if isinstance(error, QwenRemoteAccessError):
            return "vision.qwen.access_denied"
        if isinstance(error, QwenRemoteRequestError):
            return "vision.qwen.request_rejected"
        if isinstance(error, QwenRemoteUnavailableError):
            return "vision.qwen.unavailable"
        return "vision.qwen.remote_error"

    @staticmethod
    def _parse_completion(
        completion: object,
        response_model: type[BaseModel],
        images: Sequence[VisionImage],
        document_id: str,
    ) -> ExtractionResult[InvoiceT]:
        choices = getattr(completion, "choices", None)
        content: str | None = None
        if choices:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise _StructuredResponseError(("content:missing",))
        try:
            payload = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise _StructuredResponseError(("content:invalid_json",)) from exc
        try:
            parsed = response_model.model_validate(payload)
        except ValidationError as exc:
            raise _StructuredResponseError(validation_error_diagnostics(exc)) from exc
        try:
            return build_extraction_result(
                parsed,
                images,
                document_id=document_id,
                provider_name="Qwen",
            )
        except VisionExtractionError as exc:
            raise _StructuredResponseError(vision_error_diagnostics(exc)) from exc
