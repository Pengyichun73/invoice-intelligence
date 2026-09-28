"""Defense-in-depth guards for payloads sent to Qwen."""

from collections.abc import Sequence

from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import FieldEvidence, VisionPromptContext
from invoice_intelligence.domain.workflow import CorrectionEvent, JsonValue
from invoice_intelligence.infrastructure.corrections.redaction import (
    PatternSensitiveDataRedactor,
)


class QwenPayloadPolicyError(ValueError):
    """A payload violates the configured remote-data policy."""


_MAX_INLINE_IMAGE_DATA_URI_BYTES = 20_000_000


class QwenPayloadGuard:
    """Enforce pre-redacted text and explicit unredacted-image authorization."""

    def __init__(
        self,
        *,
        correction_redactor: PatternSensitiveDataRedactor,
        allow_vision_images: bool,
    ) -> None:
        self._correction_redactor = correction_redactor
        self._allow_vision_images = allow_vision_images

    def prepare_correction_context(
        self,
        events: Sequence[CorrectionEvent],
    ) -> tuple[CorrectionEvent, ...]:
        """Reapply the configured field-path policy before a remote Prompt."""

        return tuple(
            self._correction_redactor.redact(event)
            for event in events
            if event.is_reviewed and event.is_valid
        )

    def prepare_vision_prompt_context(
        self,
        context: VisionPromptContext,
    ) -> VisionPromptContext:
        """重新脱敏旧纠错记忆，并保留已脱敏的最小案例引用。"""

        return VisionPromptContext(
            correction_events=self.prepare_correction_context(
                context.correction_events
            ),
            reviewed_examples=context.reviewed_examples,
            focus_field_paths=context.focus_field_paths,
            field_semantic_catalog=context.field_semantic_catalog,
            budget=context.budget,
        )

    def validate_redacted_text(self, text: str, *, field_name: str) -> str:
        """Reject empty or inline-binary text at the infrastructure boundary."""

        normalized = text.strip()
        if not normalized:
            raise QwenPayloadPolicyError(f"{field_name} must not be empty")
        lowered = normalized.casefold()
        if "base64," in lowered or "data:image" in lowered or "data:application/pdf" in lowered:
            raise QwenPayloadPolicyError(f"{field_name} must not contain inline file data")
        return normalized

    def prepare_memory_quality_value(
        self,
        field_path: str,
        value: JsonValue,
    ) -> JsonValue:
        """Reapply field-level redaction before a quality-assessment request."""

        return self._correction_redactor.redact_field_value(field_path, value)

    def prepare_memory_quality_evidence(
        self,
        evidence: FieldEvidence,
    ) -> dict[str, JsonValue]:
        """Serialize only current evidence, redacting candidate values by field policy."""

        return {
            "field_path": evidence.field_path,
            "source": evidence.source.value,
            "page_number": evidence.page_number,
            "candidate_values": [
                self._correction_redactor.redact_field_value(
                    evidence.field_path,
                    candidate,
                )
                for candidate in evidence.candidate_values
            ],
            "readability": evidence.readability.value,
            "validation_signals": list(evidence.validation_signals),
            "ambiguous": evidence.ambiguous,
        }

    def validate_vision_images(self, images: Sequence[VisionImage]) -> None:
        """Require an explicit policy because invoice pixels cannot be safely text-masked."""

        if not self._allow_vision_images:
            raise QwenPayloadPolicyError(
                "Remote Qwen vision transfer is disabled by the data policy"
            )
        if not images:
            raise QwenPayloadPolicyError("At least one vision image is required")
        for image in images:
            if not image.content:
                raise QwenPayloadPolicyError("Vision images must not be empty")
            if image.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise QwenPayloadPolicyError("Qwen received an unsupported image MIME type")
            encoded_size = (
                len(f"data:{image.mime_type};base64,")
                + 4 * ((len(image.content) + 2) // 3)
            )
            if encoded_size > _MAX_INLINE_IMAGE_DATA_URI_BYTES:
                raise QwenPayloadPolicyError(
                    "Qwen inline image Data URI exceeds the documented size limit"
                )
            data_uri_size = (
                len(f"data:{image.mime_type};base64,")
                + ((len(image.content) + 2) // 3) * 4
            )
            if data_uri_size > _MAX_INLINE_IMAGE_DATA_URI_BYTES:
                raise QwenPayloadPolicyError(
                    "Qwen inline image exceeds the documented Data URI limit"
                )
