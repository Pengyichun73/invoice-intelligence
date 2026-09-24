"""SQLAlchemy fact-source repository for controlled training-data preparation."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.domain.examples import RetrievalScore
from invoice_intelligence.domain.training import (
    CandidateProposalSource,
    CandidateReviewStatus,
    HardNegativeCandidate,
    HardNegativeReview,
    HumanRetrievalJudgment,
    MiningSignalType,
    TrainingDatasetRecord,
    TrainingDatasetStatus,
    TrainingDatasetVersion,
    TrainingRecord,
    TrainingRecordScope,
    TrainingSplit,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    HardNegativeCandidateRow,
    HardNegativeRetrievalJudgmentRow,
    ReviewedExampleRow,
    TrainingDatasetRecordRow,
    TrainingDatasetVersionRow,
)


class SQLAlchemyTrainingDataRepository:
    """Persist all labels and version bindings in PostgreSQL-owned business tables."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def save_retrieval_judgment(self, judgment: HumanRetrievalJudgment) -> None:
        await asyncio.to_thread(self._save_retrieval_judgment_sync, judgment)

    async def list_retrieval_judgments(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_judgment_id: str | None = None,
    ) -> tuple[HumanRetrievalJudgment, ...]:
        return await asyncio.to_thread(
            self._list_retrieval_judgments_sync,
            tenant_id,
            schema_version,
            limit,
            after_judgment_id,
        )

    async def upsert_candidate(
        self,
        candidate: HardNegativeCandidate,
    ) -> HardNegativeCandidate:
        return await asyncio.to_thread(self._upsert_candidate_sync, candidate)

    async def get_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> HardNegativeCandidate | None:
        return await asyncio.to_thread(
            self._get_candidate_sync,
            tenant_id,
            candidate_id,
        )

    async def review_candidate(
        self,
        review: HardNegativeReview,
    ) -> HardNegativeCandidate:
        return await asyncio.to_thread(self._review_candidate_sync, review)

    async def list_candidates(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        status: str,
        limit: int,
        after_candidate_id: str | None = None,
    ) -> tuple[HardNegativeCandidate, ...]:
        return await asyncio.to_thread(
            self._list_candidates_sync,
            tenant_id,
            schema_version,
            status,
            limit,
            after_candidate_id,
        )

    async def create_dataset(
        self,
        dataset: TrainingDatasetVersion,
        records: Sequence[TrainingDatasetRecord],
    ) -> TrainingDatasetVersion:
        return await asyncio.to_thread(
            self._create_dataset_sync,
            dataset,
            tuple(records),
        )

    async def finish_dataset(self, dataset: TrainingDatasetVersion) -> None:
        await asyncio.to_thread(self._finish_dataset_sync, dataset)

    async def get_dataset(
        self,
        tenant_scope: str,
        dataset_id: str,
        dataset_version: str,
    ) -> tuple[TrainingDatasetVersion, tuple[TrainingDatasetRecord, ...]] | None:
        return await asyncio.to_thread(
            self._get_dataset_sync,
            tenant_scope,
            dataset_id,
            dataset_version,
        )

    def _save_retrieval_judgment_sync(self, judgment: HumanRetrievalJudgment) -> None:
        payload = _judgment_payload(judgment)
        fingerprint = _fingerprint(
            {
                "tenant_id": judgment.tenant_id,
                "judgment_id": judgment.judgment_id,
                "query_example_id": judgment.query_example_id,
                "retrieved_example_id": judgment.retrieved_example_id,
                "reviewer_id": judgment.reviewer_id,
                "is_relevant": judgment.is_relevant,
            }
        )
        try:
            with self._sessions.begin() as session:
                self._validate_examples(
                    session,
                    judgment.tenant_id,
                    judgment.schema_version,
                    (judgment.query_example_id, judgment.retrieved_example_id),
                )
                existing = session.scalar(
                    select(HardNegativeRetrievalJudgmentRow).where(
                        HardNegativeRetrievalJudgmentRow.tenant_id == judgment.tenant_id,
                        HardNegativeRetrievalJudgmentRow.fingerprint == fingerprint,
                    )
                )
                if existing is not None:
                    if _canonical(existing.judgment_json) != _canonical(payload):
                        raise WorkflowPersistenceError(
                            "Retrieval judgment replay changed immutable content"
                        )
                    return
                by_id = session.get(
                    HardNegativeRetrievalJudgmentRow,
                    judgment.judgment_id,
                )
                if by_id is not None:
                    raise WorkflowPersistenceError(
                        "Retrieval judgment identifier already has different content"
                    )
                session.add(
                    HardNegativeRetrievalJudgmentRow(
                        judgment_id=judgment.judgment_id,
                        tenant_id=judgment.tenant_id,
                        schema_version=judgment.schema_version,
                        query_example_id=judgment.query_example_id,
                        retrieved_example_id=judgment.retrieved_example_id,
                        scores_json=cast(dict[str, Any], payload["scores"]),
                        rank=judgment.rank,
                        reviewer_id=judgment.reviewer_id,
                        rejection_reason=judgment.rejection_reason,
                        fingerprint=fingerprint,
                        is_relevant=False,
                        is_valid=judgment.is_valid,
                        judgment_json=payload,
                        created_at=judgment.created_at,
                    )
                )
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist retrieval judgment") from exc

    def _list_retrieval_judgments_sync(
        self,
        tenant_id: str,
        schema_version: str,
        limit: int,
        after_judgment_id: str | None,
    ) -> tuple[HumanRetrievalJudgment, ...]:
        _validate_page(tenant_id, schema_version, limit, after_judgment_id)
        try:
            with self._sessions() as session:
                statement = select(HardNegativeRetrievalJudgmentRow).where(
                    HardNegativeRetrievalJudgmentRow.tenant_id == tenant_id,
                    HardNegativeRetrievalJudgmentRow.schema_version == schema_version,
                    HardNegativeRetrievalJudgmentRow.is_valid.is_(True),
                    HardNegativeRetrievalJudgmentRow.is_relevant.is_(False),
                )
                if after_judgment_id is not None:
                    statement = statement.where(
                        HardNegativeRetrievalJudgmentRow.judgment_id > after_judgment_id
                    )
                rows = session.scalars(
                    statement.order_by(
                        HardNegativeRetrievalJudgmentRow.judgment_id
                    ).limit(limit)
                ).all()
                return tuple(_judgment_from_payload(row.judgment_json) for row in rows)
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read retrieval judgments") from exc

    def _upsert_candidate_sync(
        self,
        candidate: HardNegativeCandidate,
    ) -> HardNegativeCandidate:
        try:
            with self._sessions.begin() as session:
                self._validate_examples(
                    session,
                    candidate.tenant_id,
                    candidate.schema_version,
                    (candidate.positive_example_id, candidate.negative_example_id),
                )
                existing_row = session.scalar(
                    select(HardNegativeCandidateRow).where(
                        HardNegativeCandidateRow.tenant_id == candidate.tenant_id,
                        HardNegativeCandidateRow.fingerprint == candidate.fingerprint,
                    )
                )
                if existing_row is None:
                    if session.get(HardNegativeCandidateRow, candidate.candidate_id) is not None:
                        raise WorkflowPersistenceError(
                            "Hard-negative candidate identifier has different content"
                        )
                    session.add(_candidate_row(candidate))
                    return candidate
                existing = _candidate_from_payload(existing_row.candidate_json)
                merged = self._merge_candidate(existing, candidate)
                if merged != existing:
                    _apply_candidate(existing_row, merged)
                return merged
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to upsert hard-negative candidate") from exc

    def _get_candidate_sync(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> HardNegativeCandidate | None:
        _require_text("tenant_id", tenant_id)
        _require_text("candidate_id", candidate_id)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(HardNegativeCandidateRow).where(
                        HardNegativeCandidateRow.tenant_id == tenant_id,
                        HardNegativeCandidateRow.candidate_id == candidate_id,
                    )
                )
                return _candidate_from_payload(row.candidate_json) if row else None
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read hard-negative candidate") from exc

    def _review_candidate_sync(
        self,
        review: HardNegativeReview,
    ) -> HardNegativeCandidate:
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(HardNegativeCandidateRow).where(
                        HardNegativeCandidateRow.tenant_id == review.tenant_id,
                        HardNegativeCandidateRow.candidate_id == review.candidate_id,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Hard-negative candidate does not exist")
                candidate = _candidate_from_payload(row.candidate_json)
                if not candidate.is_valid:
                    raise WorkflowPersistenceError("Invalid candidate cannot be reviewed")
                if candidate.status is not CandidateReviewStatus.PENDING:
                    if candidate.review == review:
                        return candidate
                    raise WorkflowPersistenceError("Candidate already has a final human decision")
                reviewed = replace(
                    candidate,
                    status=review.decision,
                    review=review,
                    updated_at=review.reviewed_at,
                )
                _apply_candidate(row, reviewed)
                return reviewed
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to review hard-negative candidate") from exc

    def _list_candidates_sync(
        self,
        tenant_id: str,
        schema_version: str,
        status: str,
        limit: int,
        after_candidate_id: str | None,
    ) -> tuple[HardNegativeCandidate, ...]:
        _validate_page(tenant_id, schema_version, limit, after_candidate_id)
        try:
            parsed_status = CandidateReviewStatus(status)
            with self._sessions() as session:
                statement = select(HardNegativeCandidateRow).where(
                    HardNegativeCandidateRow.tenant_id == tenant_id,
                    HardNegativeCandidateRow.schema_version == schema_version,
                    HardNegativeCandidateRow.status == parsed_status.value,
                    HardNegativeCandidateRow.is_valid.is_(True),
                )
                if after_candidate_id is not None:
                    statement = statement.where(
                        HardNegativeCandidateRow.candidate_id > after_candidate_id
                    )
                rows = session.scalars(
                    statement.order_by(HardNegativeCandidateRow.candidate_id).limit(limit)
                ).all()
                return tuple(_candidate_from_payload(row.candidate_json) for row in rows)
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list hard-negative candidates") from exc

    def _create_dataset_sync(
        self,
        dataset: TrainingDatasetVersion,
        records: tuple[TrainingDatasetRecord, ...],
    ) -> TrainingDatasetVersion:
        if dataset.status is not TrainingDatasetStatus.BUILDING:
            raise WorkflowPersistenceError("New training dataset must be building")
        self._validate_dataset_records(dataset, records)
        dataset_key = _dataset_key(
            dataset.tenant_scope,
            dataset.dataset_id,
            dataset.version,
        )
        try:
            with self._sessions.begin() as session:
                existing = session.get(TrainingDatasetVersionRow, dataset_key)
                if existing is not None:
                    stored = _dataset_from_payload(existing.dataset_json)
                    if stored.fingerprint != dataset.fingerprint:
                        raise WorkflowPersistenceError(
                            "Training dataset version has different immutable bindings"
                        )
                    self._validate_stored_records(session, dataset_key, records)
                    return stored
                session.add(
                    TrainingDatasetVersionRow(
                        dataset_key=dataset_key,
                        tenant_scope=dataset.tenant_scope,
                        dataset_id=dataset.dataset_id,
                        dataset_version=dataset.version,
                        source_tenant_ids_json=list(dataset.source_tenant_ids),
                        schema_version=dataset.schema_version,
                        generation_rule_version=dataset.generation_rule_version,
                        redaction_policy_version=dataset.redaction_policy_version,
                        split_rule_version=dataset.split_rule_version,
                        split_salt_version=dataset.split_salt_version,
                        created_by=dataset.created_by,
                        cross_tenant=dataset.cross_tenant,
                        authorization_id=dataset.authorization_id,
                        status=dataset.status.value,
                        record_count=dataset.record_count,
                        fingerprint=dataset.fingerprint,
                        dataset_json=_dataset_payload(dataset),
                        created_at=dataset.created_at,
                        completed_at=None,
                    )
                )
                for record in records:
                    session.add(
                        TrainingDatasetRecordRow(
                            record_id=record.record_id,
                            dataset_key=dataset_key,
                            source_tenant_id=record.source_tenant_id,
                            split=record.split.value,
                            source_document_ids_json=list(record.source_document_ids),
                            candidate_ids_json=list(record.candidate_ids),
                            group_fingerprint=record.group_fingerprint,
                            fingerprint=record.fingerprint,
                            record_json=_dataset_record_payload(record),
                            created_at=dataset.created_at,
                        )
                    )
                return dataset
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to freeze training dataset") from exc

    def _finish_dataset_sync(self, dataset: TrainingDatasetVersion) -> None:
        if dataset.status not in {
            TrainingDatasetStatus.EXPORTED,
            TrainingDatasetStatus.FAILED,
        }:
            raise WorkflowPersistenceError("Dataset outcome must be exported or failed")
        dataset_key = _dataset_key(
            dataset.tenant_scope,
            dataset.dataset_id,
            dataset.version,
        )
        try:
            with self._sessions.begin() as session:
                row = session.get(TrainingDatasetVersionRow, dataset_key)
                if row is None:
                    raise WorkflowPersistenceError("Training dataset does not exist")
                existing = _dataset_from_payload(row.dataset_json)
                if existing.fingerprint != dataset.fingerprint:
                    raise WorkflowPersistenceError("Training dataset outcome changed its bindings")
                if existing.status is TrainingDatasetStatus.EXPORTED:
                    if existing != dataset:
                        raise WorkflowPersistenceError("Exported dataset outcome is immutable")
                    return
                transitions = {
                    TrainingDatasetStatus.BUILDING: {
                        TrainingDatasetStatus.EXPORTED,
                        TrainingDatasetStatus.FAILED,
                    },
                    TrainingDatasetStatus.FAILED: {
                        TrainingDatasetStatus.EXPORTED,
                        TrainingDatasetStatus.FAILED,
                    },
                }
                if dataset.status not in transitions[existing.status]:
                    raise WorkflowPersistenceError("Invalid dataset lifecycle transition")
                row.status = dataset.status.value
                row.dataset_json = _dataset_payload(dataset)
                row.completed_at = dataset.completed_at
        except WorkflowPersistenceError:
            raise
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to finish training dataset") from exc

    def _get_dataset_sync(
        self,
        tenant_scope: str,
        dataset_id: str,
        dataset_version: str,
    ) -> tuple[TrainingDatasetVersion, tuple[TrainingDatasetRecord, ...]] | None:
        for name, value in (
            ("tenant_scope", tenant_scope),
            ("dataset_id", dataset_id),
            ("dataset_version", dataset_version),
        ):
            _require_text(name, value)
        dataset_key = _dataset_key(tenant_scope, dataset_id, dataset_version)
        try:
            with self._sessions() as session:
                row = session.get(TrainingDatasetVersionRow, dataset_key)
                if row is None:
                    return None
                records = session.scalars(
                    select(TrainingDatasetRecordRow)
                    .where(TrainingDatasetRecordRow.dataset_key == dataset_key)
                    .order_by(TrainingDatasetRecordRow.record_id)
                ).all()
                return (
                    _dataset_from_payload(row.dataset_json),
                    tuple(_dataset_record_from_payload(item.record_json) for item in records),
                )
        except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read training dataset") from exc

    @staticmethod
    def _validate_examples(
        session: Session,
        tenant_id: str,
        schema_version: str,
        example_ids: tuple[str, ...],
    ) -> None:
        rows = session.scalars(
            select(ReviewedExampleRow).where(
                ReviewedExampleRow.example_id.in_(example_ids),
                ReviewedExampleRow.tenant_id == tenant_id,
                ReviewedExampleRow.schema_version == schema_version,
                ReviewedExampleRow.is_reviewed.is_(True),
                ReviewedExampleRow.is_valid.is_(True),
            )
        ).all()
        if {row.example_id for row in rows} != set(example_ids):
            raise WorkflowPersistenceError(
                "Training data references an unreviewed, invalid, or cross-tenant example"
            )

    @staticmethod
    def _merge_candidate(
        existing: HardNegativeCandidate,
        incoming: HardNegativeCandidate,
    ) -> HardNegativeCandidate:
        immutable_existing = (
            existing.candidate_id,
            existing.tenant_id,
            existing.schema_version,
            existing.positive_example_id,
            existing.negative_example_id,
            existing.fingerprint,
        )
        immutable_incoming = (
            incoming.candidate_id,
            incoming.tenant_id,
            incoming.schema_version,
            incoming.positive_example_id,
            incoming.negative_example_id,
            incoming.fingerprint,
        )
        if immutable_existing != immutable_incoming:
            raise WorkflowPersistenceError("Candidate fingerprint collision detected")
        if existing.status is not CandidateReviewStatus.PENDING:
            has_conflicting_review = (
                incoming.status is not CandidateReviewStatus.PENDING
                and incoming.review != existing.review
            )
            if has_conflicting_review:
                raise WorkflowPersistenceError("Conflicting human hard-negative decisions")
            return existing
        if incoming.status is not CandidateReviewStatus.PENDING:
            return replace(
                incoming,
                signal_types=tuple(
                    sorted(
                        set(existing.signal_types) | set(incoming.signal_types),
                        key=lambda item: item.value,
                    )
                ),
                rationale=" | ".join(
                    sorted({existing.rationale, incoming.rationale})
                ),
                created_at=existing.created_at,
                updated_at=max(existing.updated_at, incoming.updated_at),
            )
        merged_signals = tuple(
            sorted(
                set(existing.signal_types) | set(incoming.signal_types),
                key=lambda item: item.value,
            )
        )
        merged_rationale = " | ".join(
            sorted({existing.rationale, incoming.rationale})
        )
        if (
            merged_signals == existing.signal_types
            and merged_rationale == existing.rationale
        ):
            return existing
        return replace(
            existing,
            signal_types=merged_signals,
            rationale=merged_rationale,
            updated_at=max(existing.updated_at, incoming.updated_at),
        )

    @staticmethod
    def _validate_dataset_records(
        dataset: TrainingDatasetVersion,
        records: tuple[TrainingDatasetRecord, ...],
    ) -> None:
        if len(records) != dataset.record_count:
            raise WorkflowPersistenceError("Dataset record_count does not match contents")
        if len({item.record_id for item in records}) != len(records):
            raise WorkflowPersistenceError("Dataset record identifiers must be unique")
        for record in records:
            if record.dataset_id != dataset.dataset_id or record.dataset_version != dataset.version:
                raise WorkflowPersistenceError("Dataset record references another version")
            if record.source_tenant_id not in dataset.source_tenant_ids:
                raise WorkflowPersistenceError("Dataset record crosses source tenant scope")

    @staticmethod
    def _validate_stored_records(
        session: Session,
        dataset_key: str,
        records: tuple[TrainingDatasetRecord, ...],
    ) -> None:
        rows = session.scalars(
            select(TrainingDatasetRecordRow).where(
                TrainingDatasetRecordRow.dataset_key == dataset_key
            )
        ).all()
        stored = {
            row.record_id: _canonical(row.record_json)
            for row in rows
        }
        incoming = {
            item.record_id: _canonical(_dataset_record_payload(item))
            for item in records
        }
        if stored != incoming:
            raise WorkflowPersistenceError("Dataset replay changed immutable records")


def _candidate_row(candidate: HardNegativeCandidate) -> HardNegativeCandidateRow:
    review = candidate.review
    return HardNegativeCandidateRow(
        candidate_id=candidate.candidate_id,
        tenant_id=candidate.tenant_id,
        schema_version=candidate.schema_version,
        positive_example_id=candidate.positive_example_id,
        negative_example_id=candidate.negative_example_id,
        signal_types_json=[item.value for item in candidate.signal_types],
        proposal_source=candidate.proposal_source.value,
        rationale=candidate.rationale,
        fingerprint=candidate.fingerprint,
        status=candidate.status.value,
        source_judgment_id=candidate.source_judgment_id,
        review_id=review.review_id if review else None,
        reviewer_id=review.reviewer_id if review else None,
        review_reason=review.reason if review else None,
        reviewed_at=review.reviewed_at if review else None,
        is_valid=candidate.is_valid,
        candidate_json=_candidate_payload(candidate),
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def _apply_candidate(row: HardNegativeCandidateRow, candidate: HardNegativeCandidate) -> None:
    review = candidate.review
    row.signal_types_json = [item.value for item in candidate.signal_types]
    row.proposal_source = candidate.proposal_source.value
    row.rationale = candidate.rationale
    row.status = candidate.status.value
    row.source_judgment_id = candidate.source_judgment_id
    row.review_id = review.review_id if review else None
    row.reviewer_id = review.reviewer_id if review else None
    row.review_reason = review.reason if review else None
    row.reviewed_at = review.reviewed_at if review else None
    row.is_valid = candidate.is_valid
    row.candidate_json = _candidate_payload(candidate)
    row.updated_at = candidate.updated_at


def _judgment_payload(judgment: HumanRetrievalJudgment) -> dict[str, Any]:
    return {
        "judgment_id": judgment.judgment_id,
        "tenant_id": judgment.tenant_id,
        "query_example_id": judgment.query_example_id,
        "retrieved_example_id": judgment.retrieved_example_id,
        "schema_version": judgment.schema_version,
        "scores": _score_payload(judgment.scores),
        "rank": judgment.rank,
        "reviewer_id": judgment.reviewer_id,
        "rejection_reason": judgment.rejection_reason,
        "created_at": judgment.created_at.isoformat(),
        "is_relevant": False,
        "is_valid": judgment.is_valid,
    }


def _judgment_from_payload(payload: dict[str, Any]) -> HumanRetrievalJudgment:
    return HumanRetrievalJudgment(
        judgment_id=str(payload["judgment_id"]),
        tenant_id=str(payload["tenant_id"]),
        query_example_id=str(payload["query_example_id"]),
        retrieved_example_id=str(payload["retrieved_example_id"]),
        schema_version=str(payload["schema_version"]),
        scores=_score_from_payload(cast(dict[str, Any], payload["scores"])),
        rank=int(payload["rank"]),
        reviewer_id=str(payload["reviewer_id"]),
        rejection_reason=str(payload["rejection_reason"]),
        created_at=_datetime(payload["created_at"]),
        is_relevant=False,
        is_valid=_bool(payload["is_valid"]),
    )


def _candidate_payload(candidate: HardNegativeCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "tenant_id": candidate.tenant_id,
        "schema_version": candidate.schema_version,
        "positive_example_id": candidate.positive_example_id,
        "negative_example_id": candidate.negative_example_id,
        "signal_types": [item.value for item in candidate.signal_types],
        "proposal_source": candidate.proposal_source.value,
        "rationale": candidate.rationale,
        "fingerprint": candidate.fingerprint,
        "status": candidate.status.value,
        "source_judgment_id": candidate.source_judgment_id,
        "review": _review_payload(candidate.review),
        "is_valid": candidate.is_valid,
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


def _candidate_from_payload(payload: dict[str, Any]) -> HardNegativeCandidate:
    review_payload = payload.get("review")
    return HardNegativeCandidate(
        candidate_id=str(payload["candidate_id"]),
        tenant_id=str(payload["tenant_id"]),
        schema_version=str(payload["schema_version"]),
        positive_example_id=str(payload["positive_example_id"]),
        negative_example_id=str(payload["negative_example_id"]),
        signal_types=tuple(
            MiningSignalType(str(item))
            for item in cast(list[object], payload["signal_types"])
        ),
        proposal_source=CandidateProposalSource(str(payload["proposal_source"])),
        rationale=str(payload["rationale"]),
        fingerprint=str(payload["fingerprint"]),
        status=CandidateReviewStatus(str(payload["status"])),
        source_judgment_id=_optional_string(payload.get("source_judgment_id")),
        review=(
            _review_from_payload(cast(dict[str, Any], review_payload))
            if review_payload is not None
            else None
        ),
        is_valid=_bool(payload["is_valid"]),
        created_at=_datetime(payload["created_at"]),
        updated_at=_datetime(payload["updated_at"]),
    )


def _review_payload(review: HardNegativeReview | None) -> dict[str, Any] | None:
    if review is None:
        return None
    return {
        "review_id": review.review_id,
        "candidate_id": review.candidate_id,
        "tenant_id": review.tenant_id,
        "decision": review.decision.value,
        "reviewer_id": review.reviewer_id,
        "reason": review.reason,
        "reviewed_at": review.reviewed_at.isoformat(),
    }


def _review_from_payload(payload: dict[str, Any]) -> HardNegativeReview:
    decision = CandidateReviewStatus(str(payload["decision"]))
    if decision is CandidateReviewStatus.PENDING:
        raise ValueError("Persisted review cannot be pending")
    return HardNegativeReview(
        review_id=str(payload["review_id"]),
        candidate_id=str(payload["candidate_id"]),
        tenant_id=str(payload["tenant_id"]),
        decision=decision,
        reviewer_id=str(payload["reviewer_id"]),
        reason=str(payload["reason"]),
        reviewed_at=_datetime(payload["reviewed_at"]),
    )


def _dataset_payload(dataset: TrainingDatasetVersion) -> dict[str, Any]:
    return {
        "dataset_id": dataset.dataset_id,
        "version": dataset.version,
        "tenant_scope": dataset.tenant_scope,
        "source_tenant_ids": list(dataset.source_tenant_ids),
        "schema_version": dataset.schema_version,
        "generation_rule_version": dataset.generation_rule_version,
        "redaction_policy_version": dataset.redaction_policy_version,
        "split_rule_version": dataset.split_rule_version,
        "split_salt_version": dataset.split_salt_version,
        "created_by": dataset.created_by,
        "created_at": dataset.created_at.isoformat(),
        "cross_tenant": dataset.cross_tenant,
        "authorization_id": dataset.authorization_id,
        "status": dataset.status.value,
        "record_count": dataset.record_count,
        "fingerprint": dataset.fingerprint,
        "artifact_references": list(dataset.artifact_references),
        "completed_at": (
            dataset.completed_at.isoformat() if dataset.completed_at else None
        ),
        "failure_code": dataset.failure_code,
    }


def _dataset_from_payload(payload: dict[str, Any]) -> TrainingDatasetVersion:
    return TrainingDatasetVersion(
        dataset_id=str(payload["dataset_id"]),
        version=str(payload["version"]),
        tenant_scope=str(payload["tenant_scope"]),
        source_tenant_ids=tuple(
            str(item) for item in cast(list[object], payload["source_tenant_ids"])
        ),
        schema_version=str(payload["schema_version"]),
        generation_rule_version=str(payload["generation_rule_version"]),
        redaction_policy_version=str(payload["redaction_policy_version"]),
        split_rule_version=str(payload["split_rule_version"]),
        split_salt_version=str(payload["split_salt_version"]),
        created_by=str(payload["created_by"]),
        created_at=_datetime(payload["created_at"]),
        cross_tenant=_bool(payload["cross_tenant"]),
        authorization_id=_optional_string(payload.get("authorization_id")),
        status=TrainingDatasetStatus(str(payload["status"])),
        record_count=int(payload["record_count"]),
        fingerprint=str(payload["fingerprint"]),
        artifact_references=tuple(
            str(item) for item in cast(list[object], payload["artifact_references"])
        ),
        completed_at=(
            _datetime(payload["completed_at"])
            if payload.get("completed_at") is not None
            else None
        ),
        failure_code=_optional_string(payload.get("failure_code")),
    )


def _dataset_record_payload(record: TrainingDatasetRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "dataset_id": record.dataset_id,
        "dataset_version": record.dataset_version,
        "source_tenant_id": record.source_tenant_id,
        "source_document_ids": list(record.source_document_ids),
        "candidate_ids": list(record.candidate_ids),
        "group_fingerprint": record.group_fingerprint,
        "split": record.split.value,
        "record": {
            "query": record.record.query,
            "positive": record.record.positive,
            "hard_negatives": list(record.record.hard_negatives),
            "scope": {
                "tenant_scope": record.record.scope.tenant_scope,
                "document_type": record.record.scope.document_type,
                "field_path": record.record.scope.field_path,
                "schema_version": record.record.scope.schema_version,
                "vendor_fingerprint": record.record.scope.vendor_fingerprint,
                "template_cluster": record.record.scope.template_cluster,
            },
            "source_event_ids": list(record.record.source_event_ids),
        },
        "fingerprint": record.fingerprint,
    }


def _dataset_record_from_payload(payload: dict[str, Any]) -> TrainingDatasetRecord:
    record = cast(dict[str, Any], payload["record"])
    scope = cast(dict[str, Any], record["scope"])
    return TrainingDatasetRecord(
        record_id=str(payload["record_id"]),
        dataset_id=str(payload["dataset_id"]),
        dataset_version=str(payload["dataset_version"]),
        source_tenant_id=str(payload["source_tenant_id"]),
        source_document_ids=tuple(
            str(item) for item in cast(list[object], payload["source_document_ids"])
        ),
        candidate_ids=tuple(
            str(item) for item in cast(list[object], payload["candidate_ids"])
        ),
        group_fingerprint=str(payload["group_fingerprint"]),
        split=TrainingSplit(str(payload["split"])),
        record=TrainingRecord(
            query=str(record["query"]),
            positive=str(record["positive"]),
            hard_negatives=tuple(
                str(item) for item in cast(list[object], record["hard_negatives"])
            ),
            scope=TrainingRecordScope(
                tenant_scope=str(scope["tenant_scope"]),
                document_type=str(scope["document_type"]),
                field_path=str(scope["field_path"]),
                schema_version=str(scope["schema_version"]),
                vendor_fingerprint=_optional_string(scope.get("vendor_fingerprint")),
                template_cluster=_optional_string(scope.get("template_cluster")),
            ),
            source_event_ids=tuple(
                str(item) for item in cast(list[object], record["source_event_ids"])
            ),
        ),
        fingerprint=str(payload["fingerprint"]),
    )


def _score_payload(score: RetrievalScore) -> dict[str, float | None]:
    return {
        "dense": score.dense,
        "sparse": score.sparse,
        "fusion": score.fusion,
        "rerank": score.rerank,
    }


def _score_from_payload(payload: dict[str, Any]) -> RetrievalScore:
    return RetrievalScore(
        dense=_optional_float(payload.get("dense")),
        sparse=_optional_float(payload.get("sparse")),
        fusion=_optional_float(payload.get("fusion")),
        rerank=_optional_float(payload.get("rerank")),
    )


def _dataset_key(tenant_scope: str, dataset_id: str, version: str) -> str:
    return sha256(
        f"{tenant_scope}\0{dataset_id}\0{version}".encode("utf-8")
    ).hexdigest()


def _validate_page(
    tenant_id: str,
    schema_version: str,
    limit: int,
    cursor: str | None,
) -> None:
    _require_text("tenant_id", tenant_id)
    _require_text("schema_version", schema_version)
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    if cursor is not None:
        _require_text("cursor", cursor)


def _fingerprint(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Persisted training boolean has an invalid type")
    return value


def _optional_float(value: object) -> float | None:
    return float(cast(Any, value)) if value is not None else None


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
