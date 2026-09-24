"""Versioned field semantic catalog composed from Schema and approved tenant metadata."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Generic, TypeVar

from invoice_intelligence.application.ports.field_semantics import (
    FieldAliasRepository,
    FieldSemanticSchemaReader,
)
from invoice_intelligence.domain.field_semantics import (
    FieldAlias,
    FieldAliasStatus,
    FieldBindingCandidate,
    FieldBindingEvidence,
    FieldContextAnchor,
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    FieldSemanticPromptCatalog,
    FieldSemanticPromptDefinition,
    normalize_field_label,
)

SchemaT = TypeVar("SchemaT")


class FieldSemanticCatalog(Generic[SchemaT]):
    """Expose Schema-authoritative fields plus approved tenant-specific aliases."""

    def __init__(
        self,
        *,
        schema_reader: FieldSemanticSchemaReader,
        alias_repository: FieldAliasRepository,
        output_schema: type[SchemaT],
        schema_version: str,
        base_catalog_version: FieldSemanticCatalogVersion,
    ) -> None:
        if not schema_version.strip() or schema_version != schema_version.strip():
            raise ValueError("schema_version must be non-empty and normalized")
        self._schema_reader = schema_reader
        self._aliases = alias_repository
        self._output_schema = output_schema
        self._schema_version = schema_version
        self._base_catalog_version = base_catalog_version

    async def list_definitions(
        self,
        tenant_id: str,
        *,
        document_type: str | None = None,
        catalog_version: FieldSemanticCatalogVersion | None = None,
    ) -> tuple[FieldSemanticDefinition, ...]:
        """Compose a tenant view without persisting or mutating Entity Schema fields."""

        tenant = self._normalize_scope(tenant_id)
        version = catalog_version or await self._aliases.get_active_catalog_version(
            tenant,
            self._schema_version,
        )
        version = version or self._base_catalog_version
        if (
            catalog_version is not None
            and version != self._base_catalog_version
            and not await self._aliases.is_catalog_version_valid(
                tenant,
                self._schema_version,
                version,
            )
        ):
            raise ValueError("Field semantic catalog version is absent or invalidated")
        base = self._schema_reader.read_field_semantics(
            self._output_schema,
            self._schema_version,
            version,
        )
        if document_type is not None:
            normalized_document_type = self._normalize_scope(document_type)
            base = tuple(
                item for item in base if item.document_type == normalized_document_type
            )
        aliases = await self._aliases.list_approved_aliases(
            tenant,
            self._schema_version,
            version,
            document_type=document_type,
            canonical_field_paths=tuple(
                sorted({item.canonical_field_path for item in base})
            ),
        )
        aliases_by_scope: dict[tuple[str, str], list[FieldAlias]] = {}
        for alias in aliases:
            aliases_by_scope.setdefault(
                (alias.document_type, alias.canonical_field_path),
                [],
            ).append(alias)

        known_scopes = {
            (item.document_type, item.canonical_field_path) for item in base
        }
        orphan_scopes = set(aliases_by_scope).difference(known_scopes)
        if orphan_scopes:
            raise ValueError("Approved aliases reference fields absent from the Entity Schema")

        definitions: list[FieldSemanticDefinition] = []
        for item in base:
            scoped_aliases = tuple(
                sorted(
                    aliases_by_scope.get(
                        (item.document_type, item.canonical_field_path),
                        (),
                    ),
                    key=lambda alias: (alias.normalized_alias, alias.alias_id),
                )
            )
            positive = tuple(alias for alias in scoped_aliases if not alias.is_negative)
            negative = tuple(alias for alias in scoped_aliases if alias.is_negative)
            anchors = self._unique_anchors(scoped_aliases)
            definitions.append(
                replace(
                    item,
                    aliases=positive,
                    negative_aliases=negative,
                    context_anchors=anchors,
                    tenant_scope=tenant,
                )
            )
        return tuple(
            sorted(
                definitions,
                key=lambda item: (item.document_type, item.canonical_field_path),
            )
        )

    async def get_definition(
        self,
        tenant_id: str,
        document_type: str,
        canonical_field_path: str,
        *,
        catalog_version: FieldSemanticCatalogVersion | None = None,
    ) -> FieldSemanticDefinition | None:
        """Read one exact field; fuzzy field-path fallback is deliberately absent."""

        field_path = self._normalize_scope(canonical_field_path)
        definitions = await self.list_definitions(
            tenant_id,
            document_type=document_type,
            catalog_version=catalog_version,
        )
        return next(
            (item for item in definitions if item.canonical_field_path == field_path),
            None,
        )

    async def prompt_catalog(
        self,
        tenant_id: str,
    ) -> FieldSemanticPromptCatalog:
        """Build an approved, versioned prompt projection for all Schema variants."""

        definitions = await self.list_definitions(tenant_id)
        if not definitions:
            raise ValueError("Field semantic catalog has no current Schema definitions")
        versions = {item.catalog_version for item in definitions}
        if len(versions) != 1:
            raise ValueError("Field semantic prompt catalog contains mixed versions")
        return FieldSemanticPromptCatalog(
            schema_version=self._schema_version,
            catalog_version=next(iter(versions)),
            definitions=tuple(
                FieldSemanticPromptDefinition(
                    document_type=item.document_type,
                    canonical_field_path=item.canonical_field_path,
                    display_name=item.display_name,
                    description=item.description,
                    value_type=item.value_type,
                    approved_aliases=tuple(alias.alias_text for alias in item.aliases),
                    negative_aliases=tuple(
                        alias.alias_text for alias in item.negative_aliases
                    ),
                    context_anchors=tuple(
                        anchor.text for anchor in item.context_anchors
                    ),
                )
                for item in definitions
                if item.is_valid
            ),
        )

    async def find_binding_candidates(
        self,
        tenant_id: str,
        document_type: str,
        evidence: FieldBindingEvidence,
        *,
        catalog_version: FieldSemanticCatalogVersion | None = None,
    ) -> tuple[FieldBindingCandidate, ...]:
        """Return every plausible exact/family match; never select an invoice field."""

        definitions = await self.list_definitions(
            tenant_id,
            document_type=document_type,
            catalog_version=catalog_version,
        )
        observed = normalize_field_label(evidence.normalized_label)
        nearby = tuple(normalize_field_label(item) for item in evidence.nearby_text)
        direct: dict[str, tuple[FieldSemanticDefinition, tuple[str, ...], float]] = {}
        for definition in definitions:
            negative_matches = tuple(
                alias
                for alias in definition.negative_aliases
                if normalize_field_label(alias.alias_text) == observed
            )
            if negative_matches:
                continue
            alias_ids = tuple(
                alias.alias_id
                for alias in definition.aliases
                if normalize_field_label(alias.alias_text) == observed
            )
            base_labels = {
                normalize_field_label(definition.display_name),
                normalize_field_label(definition.canonical_field_path),
                normalize_field_label(definition.canonical_field_path.rsplit(".", 1)[-1]),
            }
            base_match = observed in base_labels
            if base_match or alias_ids:
                direct[definition.canonical_field_path] = (
                    definition,
                    alias_ids,
                    1.0 if base_match else 0.9,
                )
        if not direct:
            return ()

        candidate_definitions = dict(direct)
        direct_families = {
            self._field_family(field_path)
            for field_path in direct
            if self._field_family(field_path) is not None
        }
        for definition in definitions:
            family = self._field_family(definition.canonical_field_path)
            if family in direct_families and definition.canonical_field_path not in direct:
                candidate_definitions[definition.canonical_field_path] = (
                    definition,
                    (),
                    0.0,
                )

        paths = tuple(sorted(candidate_definitions))
        candidates: list[FieldBindingCandidate] = []
        for field_path, (definition, alias_ids, alias_score) in candidate_definitions.items():
            positive_anchors = tuple(
                anchor for anchor in definition.context_anchors if not anchor.is_negative
            )
            negative_anchors = tuple(
                anchor for anchor in definition.context_anchors if anchor.is_negative
            )
            matched_positive = self._matched_anchors(positive_anchors, nearby)
            matched_negative = self._matched_anchors(negative_anchors, nearby)
            if matched_negative:
                context_score = 0.0
            elif positive_anchors:
                context_score = len(matched_positive) / len(positive_anchors)
            else:
                context_score = 0.0
            combined_score = (
                alias_score
                if not definition.context_anchors
                else (alias_score * 0.7) + (context_score * 0.3)
            )
            candidates.append(
                FieldBindingCandidate(
                    evidence_id=evidence.evidence_id,
                    schema_version=definition.schema_version,
                    document_type=definition.document_type,
                    canonical_field_path=field_path,
                    catalog_version=definition.catalog_version,
                    matched_alias_ids=alias_ids,
                    matched_context_anchor_ids=tuple(
                        item.anchor_id for item in matched_positive + matched_negative
                    ),
                    conflicting_field_paths=tuple(
                        item for item in paths if item != field_path
                    ),
                    alias_match_score=alias_score,
                    context_match_score=context_score,
                    combined_match_score=combined_score,
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (-item.combined_match_score, item.canonical_field_path),
            )
        )

    async def submit_alias(self, alias: FieldAlias) -> FieldAlias:
        """Validate a pending tenant alias against the current immutable Schema."""

        if alias.status is not FieldAliasStatus.PENDING:
            raise ValueError("New tenant aliases must start in pending status")
        if alias.schema_version != self._schema_version:
            raise ValueError("Tenant alias schema_version is not active")
        if alias.normalized_alias != normalize_field_label(alias.alias_text):
            raise ValueError("Tenant alias normalized text is not canonical")
        if any(
            anchor.normalized_text != normalize_field_label(anchor.text)
            for anchor in alias.context_anchors
        ):
            raise ValueError("Context anchor normalized text is not canonical")
        base = self._schema_reader.read_field_semantics(
            self._output_schema,
            self._schema_version,
            alias.catalog_version,
        )
        if not any(
            item.document_type == alias.document_type
            and item.canonical_field_path == alias.canonical_field_path
            for item in base
        ):
            raise ValueError("Tenant alias target is absent from the Entity Schema")
        return await self._aliases.submit_alias(alias)

    async def decide_alias(
        self,
        tenant_id: str,
        alias_id: str,
        target_status: FieldAliasStatus,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        decided_at: datetime,
    ) -> FieldAlias:
        """Apply an attributable human decision; no model authority is accepted."""

        if target_status is FieldAliasStatus.PENDING:
            raise ValueError("Alias review cannot transition back to pending")
        return await self._aliases.decide_alias(
            self._normalize_scope(tenant_id),
            self._normalize_scope(alias_id),
            target_status,
            self._normalize_scope(reviewer_id),
            self._normalize_scope(reason),
            self._normalize_scope(idempotency_key),
            decided_at,
        )

    async def register_catalog_version(
        self,
        tenant_id: str,
        catalog_version: FieldSemanticCatalogVersion,
        created_by: str,
        created_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        return await self._aliases.register_catalog_version(
            self._normalize_scope(tenant_id),
            self._schema_version,
            catalog_version,
            self._normalize_scope(created_by),
            created_at,
        )

    async def activate_catalog_version(
        self,
        tenant_id: str,
        catalog_version: FieldSemanticCatalogVersion,
        approved_by: str,
        reason: str,
        idempotency_key: str,
        approved_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        return await self._aliases.activate_catalog_version(
            self._normalize_scope(tenant_id),
            self._schema_version,
            catalog_version,
            self._normalize_scope(approved_by),
            self._normalize_scope(reason),
            self._normalize_scope(idempotency_key),
            approved_at,
        )

    @staticmethod
    def _normalize_scope(value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized != value:
            raise ValueError("Catalog scope values must be non-empty and normalized")
        return normalized

    @staticmethod
    def _unique_anchors(aliases: Sequence[FieldAlias]) -> tuple[FieldContextAnchor, ...]:
        by_id: dict[str, FieldContextAnchor] = {}
        for alias in aliases:
            for anchor in alias.context_anchors:
                existing = by_id.setdefault(anchor.anchor_id, anchor)
                if existing != anchor:
                    raise ValueError("Context anchor ID is bound to different metadata")
        return tuple(sorted(by_id.values(), key=lambda item: item.anchor_id))

    @staticmethod
    def _matched_anchors(
        anchors: Sequence[FieldContextAnchor],
        nearby: Sequence[str],
    ) -> tuple[FieldContextAnchor, ...]:
        return tuple(
            anchor
            for anchor in anchors
            if any(anchor.normalized_text in context for context in nearby)
        )

    @staticmethod
    def _field_family(field_path: str) -> str | None:
        leaf = field_path.rsplit(".", 1)[-1]
        if "_" not in leaf:
            return None
        family = leaf.rsplit("_", 1)[-1]
        return family if len(family) >= 3 else None
