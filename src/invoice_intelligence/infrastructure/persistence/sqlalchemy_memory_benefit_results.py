"""Write value-free paired judgments and summary in one PostgreSQL transaction."""

import asyncio
import math
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError
from invoice_intelligence.application.ports.memory_effectiveness import PairedBenefitRun
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.infrastructure.persistence.sqlalchemy_memory_effectiveness import (
    SQLAlchemyMemoryEffectivenessRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    MemoryBenefitJudgmentRow,
    MemoryBenefitRunRow,
)


class SQLAlchemyMemoryBenefitResultRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
        self._reader = SQLAlchemyMemoryEffectivenessRepository(engine)

    async def get_run(self, tenant_id: str, run_id: str) -> PairedBenefitRun | None:
        return await self._reader.get_run(tenant_id, run_id)

    async def save_completed(
        self, run: PairedBenefitRun, judgments: Sequence[dict[str, Any]]
    ) -> None:
        if run.status != "completed" or run.completed_at is None:
            raise ValueError("Only completed benefit runs can be persisted")
        if len(judgments) != run.case_count * 3:
            raise ValueError("Benefit run requires all three judgments per case")
        safe_rows = tuple(self._safe_judgment(item) for item in judgments)
        try:
            await asyncio.to_thread(self._save_sync, run, safe_rows)
        except IntegrityError as exc:
            existing = await self.get_run(run.tenant_id, run.run_id)
            if existing is None or not self._same(existing, run):
                raise ResourceConflictError("Benefit run changed during completion") from exc

    def _save_sync(
        self, run: PairedBenefitRun, judgments: tuple[dict[str, Any], ...]
    ) -> None:
        with self._sessions.begin() as session:
            existing = session.scalar(select(MemoryBenefitRunRow).where(
                MemoryBenefitRunRow.tenant_id == run.tenant_id,
                MemoryBenefitRunRow.run_id == run.run_id,
            ).with_for_update())
            if existing is not None:
                mapped = self._reader._run_sync(run.tenant_id, run.run_id)
                if mapped is None or not self._same(mapped, run):
                    raise ResourceConflictError("Benefit run has conflicting evidence")
                return
            session.add(MemoryBenefitRunRow(
                run_id=run.run_id, tenant_id=run.tenant_id, status="completed",
                dataset_digest=run.dataset_digest,
                schema_version=run.schema_version,
                catalog_version=run.catalog_version,
                index_version=run.index_version,
                model_version=run.model_version,
                prompt_version=run.prompt_version,
                case_count=run.case_count,
                template_group_count=run.template_group_count,
                metrics_json=run.metrics,
                scenarios_json=list(run.scenarios),
                blocker_codes_json=list(run.blocker_codes),
                created_at=run.created_at, completed_at=run.completed_at,
            ))
            session.flush()
            for item in judgments:
                key = f"{run.run_id}:{item['case_id']}:{item['variant']}"
                session.add(MemoryBenefitJudgmentRow(
                    judgment_id=sha256(key.encode()).hexdigest(),
                    tenant_id=run.tenant_id, run_id=run.run_id,
                    case_id=item["case_id"], variant=item["variant"],
                    document_checksum=item["document_checksum"],
                    template_group=item["template_group"],
                    elapsed_ms=item["elapsed_ms"], fields_json=item["fields"],
                ))

    @staticmethod
    def _same(existing: PairedBenefitRun, new: PairedBenefitRun) -> bool:
        return (
            existing.status == new.status == "completed"
            and existing.dataset_digest == new.dataset_digest
            and existing.case_count == new.case_count
            and existing.template_group_count == new.template_group_count
            and existing.schema_version == new.schema_version
            and existing.catalog_version == new.catalog_version
            and existing.index_version == new.index_version
            and existing.model_version == new.model_version
            and existing.prompt_version == new.prompt_version
            and existing.metrics == new.metrics
            and existing.scenarios == new.scenarios
            and existing.blocker_codes == new.blocker_codes
        )

    @staticmethod
    def _safe_judgment(row: dict[str, Any]) -> dict[str, Any]:
        fields = row.get("fields")
        if not isinstance(fields, dict) or set(fields) != set(InvoiceExtraction.model_fields):
            raise ValueError("Benefit judgment has incomplete fields")
        safe_fields = {}
        for path, decision in fields.items():
            if not isinstance(decision, dict) or any(
                type(decision.get(key)) is not bool
                for key in ("correct", "review_required", "auto_accepted")
            ):
                raise ValueError("Benefit judgment has invalid field decisions")
            page = decision.get("evidence_page_number")
            if page is not None and (type(page) is not int or page <= 0):
                raise ValueError("Benefit judgment has invalid evidence page")
            safe_fields[path] = {
                "correct": decision["correct"],
                "review_required": decision["review_required"],
                "auto_accepted": decision["auto_accepted"],
                "evidence_page_number": page,
            }
        identifiers = ("case_id", "variant", "document_checksum", "template_group")
        if any(not isinstance(row.get(key), str) or not row[key] for key in identifiers):
            raise ValueError("Benefit judgment has incomplete identifiers")
        if row["variant"] not in {"vision", "vision_ocr", "vision_ocr_memory"}:
            raise ValueError("Benefit judgment has invalid variant")
        elapsed = row.get("elapsed_ms")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or (
            not math.isfinite(elapsed) or elapsed < 0
        ):
            raise ValueError("Benefit judgment has invalid elapsed time")
        return {
            **{key: row[key] for key in identifiers},
            "elapsed_ms": elapsed, "fields": safe_fields,
        }
