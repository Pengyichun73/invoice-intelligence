"""PostgreSQL queries for operational memory facts and paired benefit runs."""

import asyncio
from datetime import datetime

from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.ports.memory_effectiveness import (
    EffectivenessFacts,
    PairedBenefitRun,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    DocumentRow,
    EvaluationJobRow,
    ExampleIndexProjectionRow,
    ExtractionRunRow,
    IndexVersionRow,
    MemoryAdmissionRecordRow,
    MemoryBenefitRunRow,
    MemoryGoldCaseRow,
    RetrievalTraceRow,
    ReviewedExampleRow,
)


class SQLAlchemyMemoryBenefitLeakageRepository:
    """Reject evaluated bytes already present in the candidate index."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def has_indexed_source(
        self, tenant_id: str, index_version: str, document_checksum: str
    ) -> bool:
        return await asyncio.to_thread(
            self._has_indexed_source_sync, tenant_id, index_version, document_checksum
        )

    def _has_indexed_source_sync(
        self, tenant_id: str, index_version: str, document_checksum: str
    ) -> bool:
        with self._sessions() as session:
            statement = (
                select(ExampleIndexProjectionRow.projection_id)
                .join(ReviewedExampleRow, (
                    ReviewedExampleRow.example_id == ExampleIndexProjectionRow.example_id
                ) & (ReviewedExampleRow.tenant_id == ExampleIndexProjectionRow.tenant_id))
                .join(DocumentRow, (
                    DocumentRow.document_id == ReviewedExampleRow.document_id
                ) & (DocumentRow.tenant_id == ReviewedExampleRow.tenant_id))
                .join(IndexVersionRow, (
                    IndexVersionRow.index_version_id == ExampleIndexProjectionRow.index_version_id
                ) & (IndexVersionRow.tenant_id == ExampleIndexProjectionRow.tenant_id))
                .where(
                    ExampleIndexProjectionRow.tenant_id == tenant_id,
                    ExampleIndexProjectionRow.indexed_at.is_not(None),
                    IndexVersionRow.version == index_version,
                    DocumentRow.checksum == document_checksum,
                )
                .limit(1)
            )
            return session.scalar(statement) is not None


class SQLAlchemyMemoryEffectivenessRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def facts(
        self, tenant_id: str, since: datetime, until: datetime
    ) -> EffectivenessFacts:
        return await asyncio.to_thread(self._facts_sync, tenant_id, since, until)

    async def latest_run(self, tenant_id: str) -> PairedBenefitRun | None:
        return await asyncio.to_thread(self._run_sync, tenant_id, None)

    async def get_run(self, tenant_id: str, run_id: str) -> PairedBenefitRun | None:
        return await asyncio.to_thread(self._run_sync, tenant_id, run_id)

    def _facts_sync(
        self, tenant_id: str, since: datetime, until: datetime
    ) -> EffectivenessFacts:
        with self._sessions() as session:
            documents = session.execute(
                select(func.count(DocumentRow.document_id), func.max(DocumentRow.created_at)).where(
                    DocumentRow.tenant_id == tenant_id,
                    DocumentRow.created_at >= since,
                    DocumentRow.created_at < until,
                )
            ).one()
            extraction_runs = session.execute(
                select(func.count(ExtractionRunRow.run_id),
                       func.max(ExtractionRunRow.created_at)).where(
                    ExtractionRunRow.tenant_id == tenant_id,
                    ExtractionRunRow.created_at >= since,
                    ExtractionRunRow.created_at < until,
                )
            ).one()
            reviewed = session.execute(
                select(func.count(ReviewedExampleRow.example_id),
                       func.max(ReviewedExampleRow.created_at)).where(
                    ReviewedExampleRow.tenant_id == tenant_id,
                    ReviewedExampleRow.is_reviewed.is_(True),
                    ReviewedExampleRow.created_at >= since,
                    ReviewedExampleRow.created_at < until,
                )
            ).one()
            admission = session.execute(
                select(
                    func.sum(case((MemoryAdmissionRecordRow.status == "pending", 1), else_=0)),
                    func.sum(case((
                        (MemoryAdmissionRecordRow.status == "approved")
                        & (ReviewedExampleRow.is_reviewed.is_(True))
                        & (ReviewedExampleRow.is_valid.is_(True)), 1
                    ), else_=0)),
                    func.max(MemoryAdmissionRecordRow.updated_at),
                ).join(ReviewedExampleRow, (
                    ReviewedExampleRow.example_id == MemoryAdmissionRecordRow.example_id
                ) & (ReviewedExampleRow.tenant_id == MemoryAdmissionRecordRow.tenant_id)).where(
                    MemoryAdmissionRecordRow.tenant_id == tenant_id,
                    MemoryAdmissionRecordRow.updated_at >= since,
                    MemoryAdmissionRecordRow.updated_at < until,
                )
            ).one()
            active = session.scalar(
                select(IndexVersionRow).where(
                    IndexVersionRow.tenant_id == tenant_id,
                    IndexVersionRow.is_active.is_(True),
                    IndexVersionRow.is_valid.is_(True),
                )
            )
            indexed = 0
            latest_projection_at = None
            if active is not None:
                indexed_result = session.execute(
                    select(func.count(ExampleIndexProjectionRow.projection_id),
                           func.max(ExampleIndexProjectionRow.indexed_at))
                    .join(ReviewedExampleRow, (
                        ReviewedExampleRow.example_id == ExampleIndexProjectionRow.example_id
                    ) & (ReviewedExampleRow.tenant_id == ExampleIndexProjectionRow.tenant_id))
                    .join(MemoryAdmissionRecordRow, (
                        MemoryAdmissionRecordRow.example_id
                        == ExampleIndexProjectionRow.example_id
                    ) & (
                        MemoryAdmissionRecordRow.tenant_id
                        == ExampleIndexProjectionRow.tenant_id
                    )).where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.index_version_id == active.index_version_id,
                        ExampleIndexProjectionRow.status == "indexed",
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.status == "approved",
                    )
                ).one()
                indexed = int(indexed_result[0] or 0)
                latest_projection_at = indexed_result[1]
            traces = session.execute(
                select(
                    func.count(RetrievalTraceRow.trace_id),
                    func.sum(case((
                        (RetrievalTraceRow.succeeded.is_(True))
                        & (RetrievalTraceRow.empty_retrieval.is_(False)), 1
                    ), else_=0)),
                    func.sum(case((RetrievalTraceRow.review_required.is_(True), 1), else_=0)),
                    func.max(RetrievalTraceRow.created_at),
                ).where(
                    RetrievalTraceRow.tenant_id == tenant_id,
                    RetrievalTraceRow.created_at >= since,
                    RetrievalTraceRow.created_at < until,
                )
            ).one()
            evaluation = session.execute(
                select(
                    func.count(EvaluationJobRow.job_id),
                    func.sum(case((EvaluationJobRow.status == "completed", 1), else_=0)),
                    func.max(EvaluationJobRow.updated_at),
                ).where(
                    EvaluationJobRow.tenant_id == tenant_id,
                    EvaluationJobRow.created_at >= since,
                    EvaluationJobRow.created_at < until,
                )
            ).one()
            gold = session.execute(
                select(
                    func.count(MemoryGoldCaseRow.document_id),
                    func.sum(case((MemoryGoldCaseRow.status == "frozen", 1), else_=0)),
                    func.count(func.distinct(case((
                        MemoryGoldCaseRow.status == "frozen", MemoryGoldCaseRow.template_group
                    )))),
                    func.max(MemoryGoldCaseRow.frozen_at),
                ).where(
                    MemoryGoldCaseRow.tenant_id == tenant_id,
                    MemoryGoldCaseRow.created_at >= since,
                    MemoryGoldCaseRow.created_at < until,
                )
            ).one()
            return EffectivenessFacts(
                document_count=int(documents[0] or 0),
                extraction_run_count=int(extraction_runs[0] or 0),
                reviewed_example_count=int(reviewed[0] or 0),
                pending_admission_count=int(admission[0] or 0),
                approved_admission_count=int(admission[1] or 0),
                indexed_example_count=indexed,
                retrieval_count=int(traces[0] or 0),
                nonempty_retrieval_count=int(traces[1] or 0),
                review_required_retrieval_count=int(traces[2] or 0),
                active_index_version=active.version if active else None,
                latest_document_at=documents[1],
                latest_extraction_at=extraction_runs[1],
                latest_review_at=reviewed[1],
                latest_admission_at=admission[2],
                latest_projection_at=latest_projection_at,
                latest_retrieval_at=traces[3],
                evaluation_job_count=int(evaluation[0] or 0),
                completed_evaluation_job_count=int(evaluation[1] or 0),
                latest_evaluation_job_at=evaluation[2],
                gold_case_count=int(gold[0] or 0),
                frozen_gold_case_count=int(gold[1] or 0),
                frozen_template_group_count=int(gold[2] or 0),
                latest_frozen_gold_at=gold[3],
            )

    def _run_sync(self, tenant_id: str, run_id: str | None) -> PairedBenefitRun | None:
        with self._sessions() as session:
            statement = select(MemoryBenefitRunRow).where(
                MemoryBenefitRunRow.tenant_id == tenant_id
            )
            if run_id is not None:
                statement = statement.where(MemoryBenefitRunRow.run_id == run_id)
            else:
                statement = statement.order_by(
                    MemoryBenefitRunRow.created_at.desc(), MemoryBenefitRunRow.run_id.desc()
                ).limit(1)
            row = session.scalar(statement)
            if row is None:
                return None
            return PairedBenefitRun(
                run_id=row.run_id,
                tenant_id=row.tenant_id,
                status=row.status,
                dataset_digest=row.dataset_digest,
                schema_version=row.schema_version,
                catalog_version=row.catalog_version,
                index_version=row.index_version,
                model_version=row.model_version,
                prompt_version=row.prompt_version,
                case_count=row.case_count,
                template_group_count=row.template_group_count,
                metrics=dict(row.metrics_json),
                scenarios=tuple(dict(item) for item in row.scenarios_json),
                blocker_codes=tuple(row.blocker_codes_json),
                created_at=row.created_at,
                completed_at=row.completed_at,
            )
