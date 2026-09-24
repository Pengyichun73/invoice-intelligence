"""Pydantic Schema inspection for exact correction and field-semantic scopes."""

import json
from types import UnionType
from typing import TypeVar, cast, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from invoice_intelligence.application.ports.admission import MemoryFieldSchemaInspection
from invoice_intelligence.application.ports.memory import (
    CorrectionMemoryScope,
    CorrectionQueryFacts,
)
from invoice_intelligence.domain.field_semantics import (
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
)
from invoice_intelligence.domain.json_types import JsonValue

InvoiceT = TypeVar("InvoiceT")


class PydanticCorrectionScopeResolver:
    """Enumerate concrete invoice variants and their declared leaf fields."""

    def __init__(
        self,
        *,
        vendor_feature_fields: tuple[str, ...],
        template_feature_fields: tuple[str, ...],
    ) -> None:
        self._vendor_feature_fields = vendor_feature_fields
        self._template_feature_fields = template_feature_fields

    def resolve(
        self,
        output_schema: type[InvoiceT],
        schema_version: str,
    ) -> tuple[CorrectionMemoryScope, ...]:
        if not isinstance(output_schema, type) or not issubclass(output_schema, BaseModel):
            raise ValueError("Correction-memory output_schema must be a Pydantic model")
        variants = self._root_variants(output_schema)
        scopes = {
            CorrectionMemoryScope(
                document_type=variant.__name__,
                field_path=field_path,
                schema_version=schema_version,
            )
            for variant in variants
            for field_path in self._leaf_fields(variant)
        }
        return tuple(
            sorted(scopes, key=lambda item: (item.document_type, item.field_path))
        )

    def inspect_field(
        self,
        output_schema: type[object],
        document_type: str,
        field_path: str,
        value: JsonValue,
    ) -> MemoryFieldSchemaInspection:
        """Validate one current Entity leaf, including indexed detail rows."""

        if not isinstance(output_schema, type) or not issubclass(output_schema, BaseModel):
            raise ValueError("Memory quality output_schema must be a Pydantic model")
        variants = self._root_variants(output_schema)
        variant = next(
            (candidate for candidate in variants if candidate.__name__ == document_type),
            None,
        )
        if variant is None:
            return MemoryFieldSchemaInspection(
                document_type_exists=False,
                field_path_exists=False,
                value_type_valid=False,
                error_code="schema.document_type_unknown",
            )
        field = self._resolve_field_info(variant, field_path)
        if field is None:
            return MemoryFieldSchemaInspection(
                document_type_exists=True,
                field_path_exists=False,
                value_type_valid=False,
                error_code="schema.field_path_unknown",
            )
        try:
            TypeAdapter(field.annotation).validate_python(value)
        except (TypeError, ValueError, ValidationError):
            return MemoryFieldSchemaInspection(
                document_type_exists=True,
                field_path_exists=True,
                value_type_valid=False,
                error_code="schema.value_type_invalid",
            )
        return MemoryFieldSchemaInspection(
            document_type_exists=True,
            field_path_exists=True,
            value_type_valid=True,
        )

    def describe_field(
        self,
        output_schema: type[object],
        document_type: str,
        field_path: str,
    ) -> str:
        """Resolve the declared field description without mutating the Entity Schema."""

        if not isinstance(output_schema, type) or not issubclass(output_schema, BaseModel):
            raise ValueError("Memory quality output_schema must be a Pydantic model")
        variants = self._root_variants(output_schema)
        variant = next(
            (candidate for candidate in variants if candidate.__name__ == document_type),
            None,
        )
        if variant is None:
            raise ValueError("Document type is absent from the current Entity Schema")

        resolved_field = self._resolve_field_info(variant, field_path)
        if resolved_field is None:
            raise ValueError("Field path is absent from the current Entity Schema")
        description = (resolved_field.description or resolved_field.title or field_path).strip()
        if not description:
            description = field_path
        return f"{document_type}.{field_path}: {description}"

    def read_field_semantics(
        self,
        output_schema: type[InvoiceT],
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> tuple[FieldSemanticDefinition, ...]:
        """Read base semantic metadata exclusively from the Pydantic Entity Schema."""

        if not isinstance(output_schema, type) or not issubclass(output_schema, BaseModel):
            raise ValueError("Field semantic output_schema must be a Pydantic model")
        definitions: list[FieldSemanticDefinition] = []
        for variant in self._root_variants(cast(type[BaseModel], output_schema)):
            for field_path, field_info in self._leaf_field_infos(variant):
                description = (field_info.description or "").strip()
                if not description:
                    raise ValueError(
                        "Entity Schema fields require descriptions for semantic catalog use"
                    )
                display_name = (field_info.title or description).strip()
                value_schema = TypeAdapter(field_info.annotation).json_schema(
                    mode="validation"
                )
                definitions.append(
                    FieldSemanticDefinition(
                        schema_version=schema_version,
                        document_type=variant.__name__,
                        canonical_field_path=field_path,
                        display_name=display_name,
                        description=description,
                        value_type=json.dumps(
                            value_schema,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        aliases=(),
                        negative_aliases=(),
                        context_anchors=(),
                        catalog_version=catalog_version,
                        tenant_scope=None,
                        is_valid=True,
                    )
                )
        return tuple(
            sorted(
                definitions,
                key=lambda item: (item.document_type, item.canonical_field_path),
            )
        )

    def current_facts(self, invoice: object | None) -> CorrectionQueryFacts | None:
        if not isinstance(invoice, BaseModel):
            return None
        business_invoice = self._business_model(invoice)
        payload = business_invoice.model_dump(mode="json")
        if not isinstance(payload, dict):
            return None
        field_values = self._flatten(payload)
        return CorrectionQueryFacts(
            document_type=type(business_invoice).__name__,
            field_values=field_values,
            vendor_features={
                path: field_values[path]
                for path in self._vendor_feature_fields
                if path in field_values and field_values[path] is not None
            },
            template_features={
                path: field_values[path]
                for path in self._template_feature_fields
                if path in field_values and field_values[path] is not None
            },
        )

    @classmethod
    def _root_variants(cls, output_schema: type[BaseModel]) -> tuple[type[BaseModel], ...]:
        root_field = output_schema.model_fields.get("root")
        if root_field is None:
            return (output_schema,)
        variants = cls._model_types(root_field.annotation)
        if not variants:
            raise ValueError("Root invoice Schema does not declare a Pydantic variant")
        return variants

    @classmethod
    def _model_types(cls, annotation: object) -> tuple[type[BaseModel], ...]:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return (annotation,)
        origin = get_origin(annotation)
        if origin in (UnionType, list, tuple, set) or get_args(annotation):
            models: list[type[BaseModel]] = []
            for argument in get_args(annotation):
                models.extend(cls._model_types(argument))
            return tuple(dict.fromkeys(models))
        return ()

    @classmethod
    def _is_model_collection(cls, annotation: object) -> bool:
        origin = get_origin(annotation)
        if origin in (list, tuple, set):
            return bool(cls._model_types(annotation))
        return any(cls._is_model_collection(argument) for argument in get_args(annotation))

    @classmethod
    def _resolve_field_info(
        cls,
        model: type[BaseModel],
        field_path: str,
    ) -> FieldInfo | None:
        parts = field_path.split(".") if field_path else []
        current = model
        resolved: FieldInfo | None = None
        index = 0
        while index < len(parts):
            resolved = current.model_fields.get(parts[index])
            if resolved is None:
                return None
            index += 1
            if index == len(parts):
                return resolved
            nested = cls._model_types(resolved.annotation)
            if len(nested) != 1:
                return None
            if cls._is_model_collection(resolved.annotation):
                if parts[index] != "*" and not parts[index].isdecimal():
                    return None
                index += 1
                if index == len(parts):
                    return None
            current = nested[0]
        return None

    @classmethod
    def _leaf_fields(cls, model: type[BaseModel], prefix: str = "") -> tuple[str, ...]:
        paths: list[str] = []
        for name, field in model.model_fields.items():
            field_path = f"{prefix}.{name}" if prefix else name
            nested_models = cls._model_types(field.annotation)
            if nested_models:
                nested_path = (
                    f"{field_path}.*"
                    if cls._is_model_collection(field.annotation)
                    else field_path
                )
                for nested_model in nested_models:
                    paths.extend(cls._leaf_fields(nested_model, nested_path))
            else:
                paths.append(field_path)
        return tuple(paths)

    @classmethod
    def _leaf_field_infos(
        cls,
        model: type[BaseModel],
        prefix: str = "",
    ) -> tuple[tuple[str, FieldInfo], ...]:
        fields: list[tuple[str, FieldInfo]] = []
        for name, field_info in model.model_fields.items():
            field_path = f"{prefix}.{name}" if prefix else name
            nested_models = cls._model_types(field_info.annotation)
            if nested_models:
                nested_path = (
                    f"{field_path}.*"
                    if cls._is_model_collection(field_info.annotation)
                    else field_path
                )
                for nested_model in nested_models:
                    fields.extend(cls._leaf_field_infos(nested_model, nested_path))
            else:
                fields.append((field_path, field_info))
        return tuple(fields)

    @staticmethod
    def _business_model(invoice: BaseModel) -> BaseModel:
        root = getattr(invoice, "root", None)
        return root if isinstance(root, BaseModel) else invoice

    @classmethod
    def _flatten(cls, value: object, prefix: str = "") -> dict[str, JsonValue]:
        if isinstance(value, dict):
            flattened: dict[str, JsonValue] = {}
            for name, item in value.items():
                path = f"{prefix}.{name}" if prefix else str(name)
                flattened.update(cls._flatten(item, path))
            return flattened
        if isinstance(value, list):
            flattened = {}
            for index, item in enumerate(value):
                path = f"{prefix}.{index}" if prefix else str(index)
                flattened.update(cls._flatten(item, path))
            return flattened
        return {prefix: cast(JsonValue, value)}
