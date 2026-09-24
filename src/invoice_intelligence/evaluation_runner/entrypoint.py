"""隔离部署的 ASGI 工厂；缺少真实引擎或只读证据配置时拒绝启动。"""

import importlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from fastapi import FastAPI

from invoice_intelligence.domain.evaluation import EvaluationVariant
from invoice_intelligence.evaluation_runner.service import (
    IsolatedEvaluationService,
    IsolatedVariantEngine,
    ReadOnlyEvidenceSnapshot,
    create_isolated_evaluation_app,
)


def create_app() -> FastAPI:
    token_path = Path(_required("EVALUATION_SERVICE_TOKEN_FILE"))
    token = token_path.read_text(encoding="utf-8").strip()
    manifest_path = Path(_required("EVALUATION_SERVICE_EVIDENCE_MANIFEST_FILE"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or any(
        not isinstance(tenant, str)
        or not isinstance(entries, dict)
        or any(
            not isinstance(ref, str) or not isinstance(digest, str)
            for ref, digest in entries.items()
        )
        for tenant, entries in manifest.items()
    ):
        raise ValueError("Invalid isolated evidence checksum manifest")
    factory_name = _required("EVALUATION_SERVICE_ENGINE_FACTORY")
    module_name, separator, attribute = factory_name.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("Evaluation engine factory must be module:function")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise ValueError("Evaluation engine factory is not callable")
    engines = factory()
    if not isinstance(engines, Mapping):
        raise ValueError("Evaluation engine factory must return a mapping")
    service = IsolatedEvaluationService(
        token,
        ReadOnlyEvidenceSnapshot(
            Path(_required("EVALUATION_SERVICE_EVIDENCE_ROOT")),
            cast(Mapping[str, Mapping[str, str]], manifest),
        ),
        cast(Mapping[EvaluationVariant, IsolatedVariantEngine], engines),
    )
    return create_isolated_evaluation_app(service)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing isolated evaluation setting: {name}")
    return value
