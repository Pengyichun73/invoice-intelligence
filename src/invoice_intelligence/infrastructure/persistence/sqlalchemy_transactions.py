"""交易分析事实 Repository；所有读写都带 tenant_id。"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError, ResourceNotFoundError
from invoice_intelligence.domain.transactions import (
    DuplicateTransactionCase,
    RiskAssessment,
    SuspiciousTransactionCase,
    TransactionCandidate,
    TransactionClassification,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExtractionResultRow,
    ExtractionRunRow,
    TransactionAnalysisAuditRow,
    TransactionAnalysisRow,
    TransactionCandidateRow,
)


class SQLAlchemyTransactionAnalysisRepository:
    def __init__(self, engine: Engine):
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def save_analysis(
        self,
        candidate: TransactionCandidate,
        classification: TransactionClassification,
        duplicate: DuplicateTransactionCase | None,
        suspicious: SuspiciousTransactionCase | None,
        assessment: RiskAssessment,
    ) -> RiskAssessment:
        return await asyncio.to_thread(
            self._save_analysis_sync,
            candidate,
            classification,
            duplicate,
            suspicious,
            assessment,
        )

    def _save_analysis_sync(
        self,
        candidate: TransactionCandidate,
        classification: TransactionClassification,
        duplicate: DuplicateTransactionCase | None,
        suspicious: SuspiciousTransactionCase | None,
        assessment: RiskAssessment,
    ) -> RiskAssessment:
        try:
            return self._insert_analysis(
                candidate, classification, duplicate, suspicious, assessment
            )
        except IntegrityError:
            stored = self._get_candidate_sync(candidate.tenant_id, candidate.candidate_id)
            current = self._get_assessment_sync(candidate.tenant_id, candidate.candidate_id)
            if (
                stored is not None
                and current is not None
                and stored.model_dump(exclude={"created_at"})
                == candidate.model_dump(exclude={"created_at"})
            ):
                return current
            raise ResourceConflictError("Transaction candidate already exists") from None

    def _insert_analysis(
        self,
        candidate: TransactionCandidate,
        classification: TransactionClassification,
        duplicate: DuplicateTransactionCase | None,
        suspicious: SuspiciousTransactionCase | None,
        assessment: RiskAssessment,
    ) -> RiskAssessment:
        with self._sessions.begin() as session:
            source = session.scalar(
                select(ExtractionResultRow.run_id)
                .join(ExtractionRunRow, ExtractionRunRow.run_id == ExtractionResultRow.run_id)
                .where(
                    ExtractionRunRow.tenant_id == candidate.tenant_id,
                    ExtractionRunRow.run_id == candidate.run_id,
                    ExtractionRunRow.status == "completed",
                    ExtractionRunRow.document_id == candidate.document_id,
                    ExtractionResultRow.document_id == candidate.document_id,
                )
            )
            if source is None:
                raise ResourceNotFoundError("Completed transaction source was not found")
            existing = session.scalar(
                select(TransactionAnalysisRow).where(
                    TransactionAnalysisRow.tenant_id == candidate.tenant_id,
                    TransactionAnalysisRow.candidate_id == candidate.candidate_id,
                )
            )
            if existing is not None:
                stored = session.get(TransactionCandidateRow, candidate.candidate_id)
                if stored is None or TransactionCandidate.model_validate(
                    stored.payload_json
                ).model_dump(exclude={"created_at"}) != candidate.model_dump(
                    exclude={"created_at"}
                ):
                    raise ResourceConflictError(
                        "Existing transaction candidate has a different source"
                    )
                return RiskAssessment.model_validate(
                    {**existing.assessment_json, "revision": existing.revision}
                )
            stored_candidate = session.get(TransactionCandidateRow, candidate.candidate_id)
            if stored_candidate is not None and TransactionCandidate.model_validate(
                stored_candidate.payload_json
            ).model_dump(exclude={"created_at"}) != candidate.model_dump(exclude={"created_at"}):
                raise ResourceConflictError("Existing transaction candidate has a different source")
            if stored_candidate is None:
                session.add(
                    TransactionCandidateRow(
                        candidate_id=candidate.candidate_id,
                        tenant_id=candidate.tenant_id,
                        run_id=candidate.run_id,
                        document_id=candidate.document_id,
                        fingerprint=candidate.fingerprint,
                        payload_json=candidate.model_dump(mode="json"),
                        created_at=candidate.created_at,
                    )
                )
            session.add(
                TransactionAnalysisRow(
                    analysis_id=assessment.assessment_id,
                    tenant_id=candidate.tenant_id,
                    candidate_id=candidate.candidate_id,
                    classification_json=classification.model_dump(mode="json"),
                    duplicate_json=(duplicate.model_dump(mode="json") if duplicate else None),
                    suspicious_json=(suspicious.model_dump(mode="json") if suspicious else None),
                    assessment_json=assessment.model_dump(mode="json"),
                    idempotency_key_hash=assessment.idempotency_key_hash,
                    revision=assessment.revision,
                    created_at=assessment.created_at,
                    updated_at=assessment.created_at,
                )
            )
            session.add(
                TransactionAnalysisAuditRow(
                    audit_id=assessment.assessment_id,
                    tenant_id=candidate.tenant_id,
                    candidate_id=candidate.candidate_id,
                    actor_id="deterministic-policy",
                    decision=assessment.status.value,
                    idempotency_key_hash=assessment.idempotency_key_hash,
                    assessment_json=assessment.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            return assessment

    async def get_assessment(self, tenant_id: str, candidate_id: str) -> RiskAssessment | None:
        return await asyncio.to_thread(self._get_assessment_sync, tenant_id, candidate_id)

    async def get_candidate(self, tenant_id: str, candidate_id: str) -> TransactionCandidate | None:
        return await asyncio.to_thread(self._get_candidate_sync, tenant_id, candidate_id)

    def _get_candidate_sync(self, tenant_id: str, candidate_id: str) -> TransactionCandidate | None:
        with self._sessions() as session:
            row = session.scalar(
                select(TransactionCandidateRow).where(
                    TransactionCandidateRow.tenant_id == tenant_id,
                    TransactionCandidateRow.candidate_id == candidate_id,
                )
            )
            return TransactionCandidate.model_validate(row.payload_json) if row else None

    async def get_audit_result(self, tenant_id: str, key_hash: str) -> RiskAssessment | None:
        return await asyncio.to_thread(self._get_audit_result_sync, tenant_id, key_hash)

    def _get_audit_result_sync(self, tenant_id: str, key_hash: str) -> RiskAssessment | None:
        with self._sessions() as session:
            row = session.scalar(
                select(TransactionAnalysisAuditRow).where(
                    TransactionAnalysisAuditRow.tenant_id == tenant_id,
                    TransactionAnalysisAuditRow.idempotency_key_hash == key_hash,
                )
            )
            if row is None or row.assessment_json is None:
                return None
            return RiskAssessment.model_validate(row.assessment_json)

    async def find_duplicate(self, candidate: TransactionCandidate) -> TransactionCandidate | None:
        return await asyncio.to_thread(self._find_duplicate_sync, candidate)

    def _find_duplicate_sync(self, candidate: TransactionCandidate) -> TransactionCandidate | None:
        with self._sessions() as session:
            row = session.scalar(
                select(TransactionCandidateRow)
                .where(
                    TransactionCandidateRow.tenant_id == candidate.tenant_id,
                    TransactionCandidateRow.fingerprint == candidate.fingerprint,
                    TransactionCandidateRow.candidate_id != candidate.candidate_id,
                )
                .order_by(TransactionCandidateRow.created_at, TransactionCandidateRow.candidate_id)
                .limit(1)
            )
            return TransactionCandidate.model_validate(row.payload_json) if row else None

    async def list_pending_candidates(self, limit: int) -> tuple[TransactionCandidate, ...]:
        return await asyncio.to_thread(self._list_pending_candidates_sync, limit)

    def _list_pending_candidates_sync(self, limit: int) -> tuple[TransactionCandidate, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(TransactionCandidateRow)
                .outerjoin(
                    TransactionAnalysisRow,
                    TransactionAnalysisRow.candidate_id == TransactionCandidateRow.candidate_id,
                )
                .where(TransactionAnalysisRow.analysis_id.is_(None))
                .order_by(
                    TransactionCandidateRow.created_at,
                    TransactionCandidateRow.candidate_id,
                )
                .limit(max(1, min(limit, 100)))
            ).all()
            return tuple(TransactionCandidate.model_validate(row.payload_json) for row in rows)

    def _get_assessment_sync(self, tenant_id: str, candidate_id: str) -> RiskAssessment | None:
        with self._sessions() as session:
            row = session.scalar(
                select(TransactionAnalysisRow).where(
                    TransactionAnalysisRow.tenant_id == tenant_id,
                    TransactionAnalysisRow.candidate_id == candidate_id,
                )
            )
            return RiskAssessment.model_validate(
                {**row.assessment_json, "revision": row.revision}
            ) if row else None

    async def save_review(
        self, assessment: RiskAssessment, expected_revision: int
    ) -> RiskAssessment:
        return await asyncio.to_thread(self._save_review_sync, assessment, expected_revision)

    def _save_review_sync(
        self, assessment: RiskAssessment, expected_revision: int
    ) -> RiskAssessment:
        with self._sessions.begin() as session:
            changed = session.execute(
                update(TransactionAnalysisRow).where(
                    TransactionAnalysisRow.tenant_id == assessment.tenant_id,
                    TransactionAnalysisRow.candidate_id == assessment.candidate_transaction_id,
                    TransactionAnalysisRow.revision == expected_revision,
                ).values(
                    assessment_json=assessment.model_dump(mode="json"),
                    revision=expected_revision + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            if changed.rowcount != 1:
                exists = session.scalar(
                    select(TransactionAnalysisRow.analysis_id).where(
                        TransactionAnalysisRow.tenant_id == assessment.tenant_id,
                        TransactionAnalysisRow.candidate_id == assessment.candidate_transaction_id,
                    )
                )
                if exists is None:
                    raise ResourceNotFoundError("Transaction assessment was not found")
                raise ResourceConflictError("Transaction assessment revision changed")
            session.add(
                TransactionAnalysisAuditRow(
                    audit_id=assessment.idempotency_key_hash,
                    tenant_id=assessment.tenant_id,
                    candidate_id=assessment.candidate_transaction_id,
                    actor_id=assessment.reviewer_id or "deterministic-policy",
                    decision=assessment.status.value,
                    idempotency_key_hash=assessment.idempotency_key_hash,
                    assessment_json=assessment.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            return assessment
