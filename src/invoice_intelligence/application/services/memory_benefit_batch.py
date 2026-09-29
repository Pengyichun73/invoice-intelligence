"""Execute immutable frozen-gold pairs and persist only value-free judgments."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from invoice_intelligence.application.errors import ResourceConflictError, ResourceNotFoundError
from invoice_intelligence.application.ports.document_repository import DocumentReferenceRepository
from invoice_intelligence.application.ports.memory_benefit_results import (
    MemoryBenefitResultRepository,
)
from invoice_intelligence.application.ports.memory_effectiveness import PairedBenefitRun
from invoice_intelligence.application.ports.memory_gold import (
    GoldAnnotationRepository,
    GoldCaseFact,
    GoldObjectStore,
)
from invoice_intelligence.application.services.memory_benefit_evaluation import (
    FrozenInvoiceGold,
    MemoryBenefitEvaluationService,
)
from invoice_intelligence.application.services.memory_benefit_summary import (
    summarize_paired_judgments,
)


def frozen_dataset_digest(cases: Sequence[GoldCaseFact]) -> str:
    payload = [
        {
            "document_id": case.document_id,
            "document_checksum": case.document_checksum,
            "gold_checksum": case.gold_checksum,
            "template_group": case.template_group,
            "versions": case.versions,
        }
        for case in sorted(cases, key=lambda item: item.document_id)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


class MemoryBenefitBatchService:
    def __init__(
        self, documents: DocumentReferenceRepository,
        gold_repository: GoldAnnotationRepository,
        gold_objects: GoldObjectStore,
        evaluator: MemoryBenefitEvaluationService,
        results: MemoryBenefitResultRepository,
    ) -> None:
        self._documents = documents
        self._gold_repository = gold_repository
        self._gold_objects = gold_objects
        self._evaluator = evaluator
        self._results = results

    async def run(
        self, tenant_id: str, run_id: str, document_ids: Sequence[str],
        expected_dataset_digest: str,
    ) -> PairedBenefitRun:
        if not tenant_id.strip() or not run_id.strip() or len(run_id) > 64:
            raise ValueError("Benefit run identity is invalid")
        if not document_ids or len(document_ids) != len(set(document_ids)):
            raise ValueError("Benefit run requires distinct source documents")
        existing = await self._results.get_run(tenant_id, run_id)
        if existing is not None:
            if existing.status != "completed" or (
                existing.dataset_digest != expected_dataset_digest
            ):
                raise ResourceConflictError("Benefit run binding changed")
            return existing
        gold_cases: list[FrozenInvoiceGold] = []
        gold_facts: list[GoldCaseFact] = []
        checksums: set[str] = set()
        for document_id in document_ids:
            case = await self._gold_repository.get_case(tenant_id, document_id)
            document = await self._documents.get_document(document_id, tenant_id)
            if case is None or document is None:
                raise ResourceNotFoundError("Frozen gold case was not found")
            if case.status != "frozen" or not case.gold_ref or not case.gold_checksum:
                raise ResourceConflictError("Gold case is not frozen")
            if case.document_checksum != document.checksum:
                raise ResourceConflictError("Gold source checksum changed")
            if document.checksum in checksums:
                raise ResourceConflictError("Benefit dataset contains repeated source bytes")
            checksums.add(document.checksum)
            gold_facts.append(case)
            try:
                row: Any = json.loads(await self._gold_objects.get(
                    tenant_id, case.gold_ref, case.gold_checksum
                ))
                if not isinstance(row, dict):
                    raise ValueError("Gold object is not a JSON record")
                gold_cases.append(FrozenInvoiceGold.from_row(row, document))
            except (ValueError, UnicodeError) as exc:
                raise ResourceConflictError("Frozen gold object is invalid") from exc
        if frozen_dataset_digest(gold_facts) != expected_dataset_digest:
            raise ResourceConflictError("Frozen gold dataset changed")
        rows: list[dict[str, Any]] = []
        for gold in gold_cases:
            rows.extend(await self._evaluator.run_case(tenant_id=tenant_id, gold=gold))
        summary = summarize_paired_judgments(rows)
        versions = summary["versions"]
        now = datetime.now(UTC)
        run = PairedBenefitRun(
            run_id=run_id, tenant_id=tenant_id, status="completed",
            dataset_digest=expected_dataset_digest,
            schema_version=versions["schema_version"],
            catalog_version=versions["catalog_version"],
            index_version=versions["index_version"],
            model_version=versions["model_version"],
            prompt_version=versions["prompt_version"],
            case_count=summary["case_count"],
            template_group_count=summary["template_group_count"],
            metrics={**summary["metrics"], "judgments_digest": summary["dataset_digest"]},
            scenarios=tuple(summary["scenarios"]),
            blocker_codes=tuple(summary["blocker_codes"]),
            created_at=now, completed_at=now,
        )
        await self._results.save_completed(run, rows)
        return run
