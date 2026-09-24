"""Configurable redaction applied only to derived correction memory."""

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from typing import Literal

from invoice_intelligence.application.ports.examples import TenantRedactionPolicy
from invoice_intelligence.application.ports.memory import CorrectionQueryFacts
from invoice_intelligence.application.ports.training import (
    RedactedTrainingValue,
    TrainingRedactionProfile,
)
from invoice_intelligence.domain.examples import (
    ExampleCandidate,
    ExampleScope,
    IndexVersion,
    ReviewedExample,
)
from invoice_intelligence.domain.extraction import FieldEvidence
from invoice_intelligence.domain.workflow import CorrectionEvent, JsonValue

RedactionPolicy = Literal["mask", "hash", "none"]


class PatternSensitiveDataRedactor:
    """Redact configured field paths before embedding or Prompt construction."""

    def __init__(
        self,
        *,
        policy: RedactionPolicy,
        field_patterns: tuple[str, ...],
        hash_salt: str | None,
        redact_reason: bool,
    ) -> None:
        if policy == "hash" and not hash_salt:
            raise ValueError("hash_salt is required for hash redaction")
        self._policy = policy
        self._patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in field_patterns)
        self._hash_salt = hash_salt or ""
        self._redact_reason = redact_reason

    def redact(self, event: CorrectionEvent) -> CorrectionEvent:
        if self._policy == "none":
            return event
        field_is_sensitive = self._is_sensitive(event.field_path)
        return replace(
            event,
            model_value=(
                self._redact_value(event.model_value)
                if field_is_sensitive
                else event.model_value
            ),
            corrected_value=(
                self._redact_value(event.corrected_value)
                if field_is_sensitive
                else event.corrected_value
            ),
            correction_reason=(
                self._redact_text(event.correction_reason)
                if self._redact_reason
                else event.correction_reason
            ),
            vendor_features=self._redact_features(dict(event.vendor_features)),
            template_features=self._redact_features(dict(event.template_features)),
        )

    def redact_query_facts(self, facts: CorrectionQueryFacts) -> CorrectionQueryFacts:
        if self._policy == "none":
            return facts
        return CorrectionQueryFacts(
            document_type=facts.document_type,
            field_values=self._redact_features(dict(facts.field_values)),
            vendor_features=self._redact_features(dict(facts.vendor_features)),
            template_features=self._redact_features(dict(facts.template_features)),
        )

    def redact_field_value(self, field_path: str, value: JsonValue) -> JsonValue:
        """Reapply the configured field policy before remote inference."""

        if not field_path.strip() or field_path != field_path.strip():
            raise ValueError("field_path must be non-empty and normalized")
        if self._policy == "none" or not self._is_sensitive(field_path):
            return value
        return self._redact_value(value)

    def _redact_features(self, features: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            path: self._redact_value(value) if self._is_sensitive(path) else value
            for path, value in features.items()
        }

    def _is_sensitive(self, field_path: str) -> bool:
        return any(pattern.search(field_path) is not None for pattern in self._patterns)

    def _redact_value(self, value: JsonValue) -> JsonValue:
        if value is None:
            return None
        return self._redact_text(self._canonical(value))

    def _redact_text(self, value: str) -> str:
        if value == "[REDACTED]" or value.startswith("sha256:"):
            return value
        if self._policy == "mask":
            return "[REDACTED]"
        digest = sha256(f"{self._hash_salt}\0{value}".encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _canonical(value: JsonValue) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class TenantExampleRedactor:
    """Create tenant-scoped, non-raw candidates for a derived example index."""

    def __init__(
        self,
        *,
        default_policy: TenantRedactionPolicy,
        tenant_policies: Mapping[str, TenantRedactionPolicy] | None = None,
        field_patterns: tuple[str, ...] = (),
        redact_reason: bool = True,
    ) -> None:
        self._default_policy = default_policy
        self._tenant_policies = dict(tenant_policies or {})
        self._patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in field_patterns)
        self._redact_reason = redact_reason

    def redact_example(
        self,
        example: ReviewedExample,
        index_version: IndexVersion,
        redaction_policy_version: str,
    ) -> ExampleCandidate:
        """Return a sanitized candidate; raw values remain in PostgreSQL only."""

        policy = self._policy_for_tenant(example.tenant_id)
        if policy.policy_version != redaction_policy_version:
            raise ValueError("Redaction policy version does not match tenant policy")
        field_is_sensitive = self._is_sensitive(example.field_path)
        strategy = policy.strategy
        model_value = (
            self._redact_value(example.model_value, policy)
            if field_is_sensitive
            else example.model_value
        )
        reviewed_value = (
            self._redact_value(example.reviewed_value, policy)
            if field_is_sensitive
            else example.reviewed_value
        )
        reason = (
            self._redact_text(example.correction_reason, policy)
            if self._redact_reason and example.correction_reason is not None
            else example.correction_reason
        )
        if strategy == "drop" and self._redact_reason:
            reason = None
        vendor_features = self._redact_features(example.vendor_fingerprint, policy)
        template_features = self._redact_features(example.template_fingerprint, policy)
        # The Application Service replaces this deterministic seed with the full sectioned text.
        seed_text = self._canonical(
            {
                "document_type": example.document_type,
                "field_path": example.field_path,
                "catalog_version": example.catalog_version,
                "model_value": model_value,
                "reviewed_value": reviewed_value,
                "correction_reason": reason,
                "vendor_fingerprint": vendor_features,
                "template_fingerprint": template_features,
                "label_type": example.label_type.value,
            }
        )
        return ExampleCandidate(
            example_id=example.example_id,
            scope=example.scope,
            label_type=example.label_type,
            redacted_model_value=model_value,
            redacted_reviewed_value=reviewed_value,
            redacted_correction_reason=reason,
            vendor_fingerprint=vendor_features,
            template_fingerprint=template_features,
            evidence_reference=example.evidence_reference,
            redacted_index_text=seed_text,
            redaction_policy_version=redaction_policy_version,
            index_version=index_version,
            last_seen_at=example.last_seen_at,
        )

    def redact_query(
        self,
        scope: ExampleScope,
        field_evidence: FieldEvidence,
        vendor_fingerprint: str | None,
        template_fingerprint: str | None,
    ) -> str:
        """Build a stable query without exposing unredacted sensitive field values."""

        if field_evidence.field_path != scope.field_path:
            raise ValueError("Field evidence must match the exact retrieval scope")
        policy = self._policy_for_tenant(scope.tenant_id)
        if self._is_sensitive(scope.field_path):
            candidates = tuple(
                self._redact_value(value, policy)
                for value in field_evidence.candidate_values
                if policy.strategy != "drop"
            )
        else:
            candidates = field_evidence.candidate_values
        sections = (
            (
                "CURRENT_FIELD_EVIDENCE",
                {
                    "document_type": scope.document_type,
                    "field_path": scope.field_path,
                    "schema_version": scope.schema_version,
                    "catalog_version": scope.catalog_version,
                    "source": field_evidence.source.value,
                    "page_number": field_evidence.page_number,
                    "candidate_values": candidates,
                    "readability": field_evidence.readability.value,
                    "ambiguous": field_evidence.ambiguous,
                },
            ),
            (
                "VENDOR_TEMPLATE_FEATURES",
                {
                    "vendor_fingerprint": vendor_fingerprint,
                    "template_fingerprint": template_fingerprint,
                },
            ),
        )
        query_text = "\n".join(
            part
            for name, value in sections
            for part in (name, self._canonical(value))
        )
        lowered = query_text.casefold()
        if "base64," in lowered or "data:image" in lowered:
            raise ValueError("Retrieval query must not contain inline image data")
        return query_text

    def training_profile(self, tenant_id: str) -> TrainingRedactionProfile:
        """Expose only non-secret policy metadata to training-data services."""

        policy = self._policy_for_tenant(tenant_id)
        return TrainingRedactionProfile(
            strategy=policy.strategy,
            policy_version=policy.policy_version,
        )

    def redact_training_value(
        self,
        tenant_id: str,
        field_path: str,
        value: JsonValue,
        *,
        force_irreversible: bool,
    ) -> RedactedTrainingValue:
        """Redact a detached training value without permitting raw export."""

        policy = self._policy_for_tenant(tenant_id)
        if policy.strategy == "none":
            raise ValueError("Training export requires mask, hash, or drop redaction")
        if not force_irreversible and not self._is_sensitive(field_path):
            return RedactedTrainingValue(value=value, was_dropped=False)
        if policy.strategy == "drop":
            return RedactedTrainingValue(value=None, was_dropped=True)
        return RedactedTrainingValue(
            value=self._redact_value(value, policy),
            was_dropped=False,
        )

    @staticmethod
    def redact_training_identifier(
        tenant_id: str,
        value: str,
        *,
        namespace: str,
    ) -> str:
        """Create a one-way, tenant-isolated reference for exported provenance."""

        for name, text in (
            ("tenant_id", tenant_id),
            ("value", value),
            ("namespace", namespace),
        ):
            if not text.strip():
                raise ValueError(f"{name} must not be empty")
        digest = sha256(
            f"training-export\0{namespace}\0tenant:{tenant_id}\0{value}".encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    def _redact_features(
        self,
        fingerprint: str | None,
        policy: TenantRedactionPolicy,
    ) -> str | None:
        # Fingerprints are already non-reversible identifiers and never contain raw values.
        return fingerprint

    def _redact_value(self, value: JsonValue, policy: TenantRedactionPolicy) -> JsonValue:
        if policy.strategy == "drop" or value is None:
            return None
        if policy.strategy == "none":
            return value
        return self._redact_text(self._canonical(value), policy)

    @staticmethod
    def _redact_text(value: str, policy: TenantRedactionPolicy) -> str:
        if policy.strategy == "mask":
            return "[REDACTED]"
        if policy.strategy == "hash":
            if not policy.hash_salt:
                raise ValueError("Tenant hash redaction requires an isolated salt")
            digest = sha256(f"{policy.hash_salt}\0{value}".encode("utf-8")).hexdigest()
            return f"sha256:{digest}"
        if policy.strategy == "drop":
            return ""
        return value

    def _is_sensitive(self, field_path: str) -> bool:
        return any(pattern.search(field_path) is not None for pattern in self._patterns)

    def _policy_for_tenant(self, tenant_id: str) -> TenantRedactionPolicy:
        policy = self._tenant_policies.get(tenant_id, self._default_policy)
        if policy.strategy != "hash":
            return policy
        if not policy.hash_salt:
            raise ValueError("Tenant hash redaction requires an isolated salt")
        # Derive a stable tenant-specific salt from the configured secret; the raw
        # master salt is never sent to the index and is not stored in a candidate.
        isolated_salt = sha256(
            f"{policy.hash_salt}\0tenant:{tenant_id}".encode("utf-8")
        ).hexdigest()
        return replace(policy, hash_salt=isolated_salt)

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
