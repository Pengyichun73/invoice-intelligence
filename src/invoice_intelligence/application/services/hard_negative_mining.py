"""Controlled hard-negative mining with explicit human approval boundaries."""

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Callable

from invoice_intelligence.application.errors import TrainingDataError
from invoice_intelligence.application.ports.examples import ReviewedExampleRepository
from invoice_intelligence.application.ports.training import (
    FieldSimilarityProvider,
    HardNegativeProposalProvider,
    TrainingDataRepository,
)
from invoice_intelligence.domain.examples import ExampleLabelType, ReviewedExample
from invoice_intelligence.domain.training import (
    CandidateProposalSource,
    CandidateReviewStatus,
    HardNegativeCandidate,
    HardNegativeProposal,
    HardNegativeReview,
    HumanRetrievalJudgment,
    MiningSignalType,
    retrieval_score_at_least,
)

_POSITIVE_LABELS = {
    ExampleLabelType.CONFIRMED_CORRECT,
    ExampleLabelType.CORRECTED,
}


@dataclass(frozen=True, slots=True)
class HardNegativeMiningPolicy:
    """Versioned ranking thresholds and bounded mining controls."""

    rule_version: str
    high_recall_threshold: float
    high_rerank_threshold: float
    field_similarity_threshold: float
    page_size: int = 500
    max_examples: int = 10_000
    max_candidate_pairs: int = 20_000

    def __post_init__(self) -> None:
        if not self.rule_version.strip():
            raise ValueError("Mining rule_version must not be empty")
        for name, value in (
            ("high_recall_threshold", self.high_recall_threshold),
            ("high_rerank_threshold", self.high_rerank_threshold),
            ("field_similarity_threshold", self.field_similarity_threshold),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        for name, value in (
            ("page_size", self.page_size),
            ("max_examples", self.max_examples),
            ("max_candidate_pairs", self.max_candidate_pairs),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class HardNegativeMiningResult:
    """Observable output without changing model or retrieval configuration."""

    candidates: tuple[HardNegativeCandidate, ...]
    approved_from_human_review: int
    pending_human_review: int


@dataclass(slots=True)
class _CandidateSeed:
    positive: ReviewedExample
    negative: ReviewedExample
    signals: set[MiningSignalType]
    source: CandidateProposalSource
    rationales: set[str]
    judgment: HumanRetrievalJudgment | None = None


class HardNegativeMiningService:
    """Mine only reviewed facts; rule/model discoveries remain pending."""

    def __init__(
        self,
        *,
        examples: ReviewedExampleRepository,
        training_data: TrainingDataRepository,
        policy: HardNegativeMiningPolicy,
        field_similarity: FieldSimilarityProvider | None = None,
        proposal_provider: HardNegativeProposalProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._examples = examples
        self._training_data = training_data
        self._policy = policy
        self._field_similarity = field_similarity
        self._proposal_provider = proposal_provider
        self._clock = clock or (lambda: datetime.now(UTC))

    async def mine(
        self,
        *,
        tenant_id: str,
        schema_version: str,
    ) -> HardNegativeMiningResult:
        """Create replay-safe candidates without invoking training or approving proposals."""

        self._require_scope(tenant_id, schema_version)
        examples = await self._load_examples(tenant_id, schema_version)
        by_id = {item.example_id: item for item in examples}
        positives = tuple(item for item in examples if item.label_type in _POSITIVE_LABELS)
        seeds: dict[tuple[str, str], _CandidateSeed] = {}

        positives_by_scope: dict[tuple[str, str], list[ReviewedExample]] = defaultdict(list)
        for example in positives:
            positives_by_scope[(example.document_type, example.field_path)].append(example)

        for negative in examples:
            if negative.label_type is not ExampleLabelType.CONFIRMED_INCORRECT:
                continue
            anchors = positives_by_scope.get(
                (negative.document_type, negative.field_path),
                [],
            )
            if not anchors:
                continue
            positive = self._best_anchor(negative, anchors)
            self._merge_seed(
                seeds,
                positive=positive,
                negative=negative,
                signal=MiningSignalType.CONFIRMED_INCORRECT,
                source=CandidateProposalSource.REVIEWED_LABEL,
                rationale=negative.correction_reason or "human_confirmed_incorrect",
            )

        judgments = await self._load_judgments(tenant_id, schema_version)
        missing_ids = {
            item_id
            for judgment in judgments
            for item_id in (judgment.query_example_id, judgment.retrieved_example_id)
            if item_id not in by_id
        }
        if missing_ids:
            hydrated = await self._examples.get_eligible_by_ids(
                tenant_id,
                schema_version,
                tuple(sorted(missing_ids)),
            )
            by_id.update((item.example_id, item) for item in hydrated)
        for judgment in judgments:
            positive = by_id.get(judgment.query_example_id)
            negative = by_id.get(judgment.retrieved_example_id)
            if positive is None or negative is None or positive.label_type not in _POSITIVE_LABELS:
                continue
            signals: list[MiningSignalType] = []
            if retrieval_score_at_least(
                judgment.scores.fusion,
                self._policy.high_recall_threshold,
            ):
                signals.append(MiningSignalType.HIGH_RECALL_HUMAN_REJECTED)
            if retrieval_score_at_least(
                judgment.scores.rerank,
                self._policy.high_rerank_threshold,
            ):
                signals.append(MiningSignalType.HIGH_RERANK_HUMAN_REJECTED)
            for signal in signals:
                self._merge_seed(
                    seeds,
                    positive=positive,
                    negative=negative,
                    signal=signal,
                    source=CandidateProposalSource.HUMAN_RETRIEVAL_JUDGMENT,
                    rationale=judgment.rejection_reason,
                    judgment=judgment,
                )

        await self._add_vendor_template_proposals(seeds, positives)
        await self._add_same_document_proposals(seeds, positives)
        await self._add_external_proposals(seeds, examples, by_id)
        if len(seeds) > self._policy.max_candidate_pairs:
            raise TrainingDataError("Hard-negative candidate limit exceeded")

        now = self._clock()
        if now.tzinfo is None:
            raise TrainingDataError("Mining clock must return a timezone-aware datetime")
        persisted: list[HardNegativeCandidate] = []
        for key in sorted(seeds):
            candidate = self._candidate_from_seed(seeds[key], tenant_id, schema_version, now)
            persisted.append(await self._training_data.upsert_candidate(candidate))
        approved = sum(
            item.status is CandidateReviewStatus.APPROVED for item in persisted
        )
        return HardNegativeMiningResult(
            candidates=tuple(persisted),
            approved_from_human_review=approved,
            pending_human_review=len(persisted) - approved,
        )

    async def _load_examples(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> tuple[ReviewedExample, ...]:
        loaded: list[ReviewedExample] = []
        cursor: str | None = None
        while len(loaded) < self._policy.max_examples:
            page = await self._examples.list_eligible(
                tenant_id,
                schema_version,
                limit=min(
                    self._policy.page_size,
                    self._policy.max_examples - len(loaded),
                ),
                after_example_id=cursor,
            )
            if not page:
                break
            loaded.extend(page)
            cursor = page[-1].example_id
            if len(page) < self._policy.page_size:
                break
        if len(loaded) == self._policy.max_examples:
            overflow = await self._examples.list_eligible(
                tenant_id,
                schema_version,
                limit=1,
                after_example_id=loaded[-1].example_id,
            )
            if overflow:
                raise TrainingDataError("Reviewed-example mining limit exceeded")
        return tuple(loaded)

    async def _load_judgments(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> tuple[HumanRetrievalJudgment, ...]:
        loaded: list[HumanRetrievalJudgment] = []
        cursor: str | None = None
        while len(loaded) < self._policy.max_candidate_pairs:
            page = await self._training_data.list_retrieval_judgments(
                tenant_id,
                schema_version,
                limit=min(
                    self._policy.page_size,
                    self._policy.max_candidate_pairs - len(loaded),
                ),
                after_judgment_id=cursor,
            )
            if not page:
                break
            loaded.extend(page)
            cursor = page[-1].judgment_id
            if len(page) < self._policy.page_size:
                break
        return tuple(loaded)

    async def _add_vendor_template_proposals(
        self,
        seeds: dict[tuple[str, str], _CandidateSeed],
        positives: Sequence[ReviewedExample],
    ) -> None:
        groups: dict[tuple[str, str, str], list[ReviewedExample]] = defaultdict(list)
        for example in positives:
            if example.vendor_fingerprint and example.template_fingerprint:
                groups[
                    (
                        example.document_type,
                        example.vendor_fingerprint,
                        example.template_fingerprint,
                    )
                ].append(example)
        for group in groups.values():
            ordered = sorted(group, key=lambda item: item.example_id)
            for positive in ordered:
                for negative in ordered:
                    if positive.field_path == negative.field_path:
                        continue
                    self._merge_seed(
                        seeds,
                        positive=positive,
                        negative=negative,
                        signal=MiningSignalType.VENDOR_TEMPLATE_CONFUSION,
                        source=CandidateProposalSource.DETERMINISTIC_RULE,
                        rationale="same_vendor_template_different_field",
                    )
                    if len(seeds) > self._policy.max_candidate_pairs:
                        return

    async def _add_same_document_proposals(
        self,
        seeds: dict[tuple[str, str], _CandidateSeed],
        positives: Sequence[ReviewedExample],
    ) -> None:
        if self._field_similarity is None:
            return
        groups: dict[str, list[ReviewedExample]] = defaultdict(list)
        for example in positives:
            groups[example.document_id].append(example)
        for group in groups.values():
            ordered = sorted(group, key=lambda item: item.example_id)
            for positive in ordered:
                for negative in ordered:
                    if positive.field_path == negative.field_path:
                        continue
                    score = await self._field_similarity.similarity(
                        positive.field_path,
                        negative.field_path,
                    )
                    if not math.isfinite(score):
                        raise TrainingDataError("Field similarity returned a non-finite score")
                    if score < self._policy.field_similarity_threshold:
                        continue
                    self._merge_seed(
                        seeds,
                        positive=positive,
                        negative=negative,
                        signal=MiningSignalType.SAME_DOCUMENT_FIELD_CONFUSION,
                        source=CandidateProposalSource.DETERMINISTIC_RULE,
                        rationale=f"same_document_field_similarity:{score:.6g}",
                    )
                    if len(seeds) > self._policy.max_candidate_pairs:
                        return

    async def _add_external_proposals(
        self,
        seeds: dict[tuple[str, str], _CandidateSeed],
        examples: Sequence[ReviewedExample],
        by_id: dict[str, ReviewedExample],
    ) -> None:
        if self._proposal_provider is None:
            return
        proposals = await self._proposal_provider.propose(examples)
        for proposal in proposals:
            self._validate_proposal(proposal)
            positive = by_id.get(proposal.positive_example_id)
            negative = by_id.get(proposal.negative_example_id)
            if positive is None or negative is None or positive.label_type not in _POSITIVE_LABELS:
                continue
            self._merge_seed(
                seeds,
                positive=positive,
                negative=negative,
                signal=proposal.signal_type,
                source=proposal.proposal_source,
                rationale=proposal.rationale,
            )

    def _candidate_from_seed(
        self,
        seed: _CandidateSeed,
        tenant_id: str,
        schema_version: str,
        now: datetime,
    ) -> HardNegativeCandidate:
        fingerprint_payload = {
            "tenant_id": tenant_id,
            "schema_version": schema_version,
            "positive_example_id": seed.positive.example_id,
            "negative_example_id": seed.negative.example_id,
        }
        fingerprint = sha256(self._canonical(fingerprint_payload).encode("utf-8")).hexdigest()
        candidate_id = sha256(f"hard-negative\0{fingerprint}".encode("utf-8")).hexdigest()
        review: HardNegativeReview | None = None
        status = CandidateReviewStatus.PENDING
        source_judgment_id: str | None = None
        if seed.source is CandidateProposalSource.REVIEWED_LABEL:
            status = CandidateReviewStatus.APPROVED
            review = HardNegativeReview(
                review_id=sha256(
                    f"inherited-negative-review\0{candidate_id}".encode("utf-8")
                ).hexdigest(),
                candidate_id=candidate_id,
                tenant_id=tenant_id,
                decision=CandidateReviewStatus.APPROVED,
                reviewer_id=seed.negative.reviewer_id,
                reason=seed.negative.correction_reason or "human_confirmed_incorrect",
                reviewed_at=seed.negative.created_at,
            )
        elif seed.source is CandidateProposalSource.HUMAN_RETRIEVAL_JUDGMENT:
            if seed.judgment is None:
                raise TrainingDataError("Human retrieval seed is missing its judgment")
            source_judgment_id = seed.judgment.judgment_id
            status = CandidateReviewStatus.APPROVED
            review = HardNegativeReview(
                review_id=sha256(
                    f"retrieval-negative-review\0{candidate_id}\0{source_judgment_id}".encode(
                        "utf-8"
                    )
                ).hexdigest(),
                candidate_id=candidate_id,
                tenant_id=tenant_id,
                decision=CandidateReviewStatus.APPROVED,
                reviewer_id=seed.judgment.reviewer_id,
                reason=seed.judgment.rejection_reason,
                reviewed_at=seed.judgment.created_at,
            )
        return HardNegativeCandidate(
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            schema_version=schema_version,
            positive_example_id=seed.positive.example_id,
            negative_example_id=seed.negative.example_id,
            signal_types=tuple(sorted(seed.signals, key=lambda item: item.value)),
            proposal_source=seed.source,
            rationale=" | ".join(sorted(seed.rationales)),
            fingerprint=fingerprint,
            status=status,
            source_judgment_id=source_judgment_id,
            review=review,
            is_valid=True,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _merge_seed(
        seeds: dict[tuple[str, str], _CandidateSeed],
        *,
        positive: ReviewedExample,
        negative: ReviewedExample,
        signal: MiningSignalType,
        source: CandidateProposalSource,
        rationale: str,
        judgment: HumanRetrievalJudgment | None = None,
    ) -> None:
        if positive.tenant_id != negative.tenant_id:
            raise TrainingDataError("Hard-negative proposals cannot cross tenants")
        if positive.schema_version != negative.schema_version:
            raise TrainingDataError("Hard-negative proposals cannot cross Schema versions")
        key = (positive.example_id, negative.example_id)
        existing = seeds.get(key)
        if existing is None:
            seeds[key] = _CandidateSeed(
                positive=positive,
                negative=negative,
                signals={signal},
                source=source,
                rationales={rationale},
                judgment=judgment,
            )
            return
        existing.signals.add(signal)
        existing.rationales.add(rationale)
        priority = {
            CandidateProposalSource.REVIEWED_LABEL: 3,
            CandidateProposalSource.HUMAN_RETRIEVAL_JUDGMENT: 2,
            CandidateProposalSource.DETERMINISTIC_RULE: 1,
            CandidateProposalSource.LLM_PROPOSAL: 0,
        }
        if priority[source] > priority[existing.source]:
            existing.source = source
            existing.judgment = judgment

    @staticmethod
    def _best_anchor(
        negative: ReviewedExample,
        positives: Sequence[ReviewedExample],
    ) -> ReviewedExample:
        return sorted(
            positives,
            key=lambda item: (
                item.vendor_fingerprint != negative.vendor_fingerprint,
                item.template_fingerprint != negative.template_fingerprint,
                item.example_id,
            ),
        )[0]

    @staticmethod
    def _validate_proposal(proposal: HardNegativeProposal) -> None:
        if proposal.proposal_source is CandidateProposalSource.LLM_PROPOSAL:
            if proposal.signal_type is not MiningSignalType.LLM_PROPOSED:
                raise TrainingDataError("LLM proposal must retain LLM provenance")

    @staticmethod
    def _require_scope(tenant_id: str, schema_version: str) -> None:
        if not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise TrainingDataError("tenant_id must be non-empty and normalized")
        if not schema_version.strip():
            raise TrainingDataError("schema_version must not be empty")

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class HardNegativeReviewService:
    """The only application entry for human retrieval judgments and candidate decisions."""

    def __init__(
        self,
        *,
        examples: ReviewedExampleRepository,
        training_data: TrainingDataRepository,
    ) -> None:
        self._examples = examples
        self._training_data = training_data

    async def record_retrieval_judgment(
        self,
        judgment: HumanRetrievalJudgment,
    ) -> None:
        examples = await self._examples.get_eligible_by_ids(
            judgment.tenant_id,
            judgment.schema_version,
            (judgment.query_example_id, judgment.retrieved_example_id),
        )
        by_id = {item.example_id: item for item in examples}
        if set(by_id) != {judgment.query_example_id, judgment.retrieved_example_id}:
            raise TrainingDataError("Retrieval judgment references an ineligible example")
        if by_id[judgment.query_example_id].label_type not in _POSITIVE_LABELS:
            raise TrainingDataError("Retrieval judgment query must be a reviewed positive")
        await self._training_data.save_retrieval_judgment(judgment)

    async def review(self, review: HardNegativeReview) -> HardNegativeCandidate:
        candidate = await self._training_data.get_candidate(
            review.tenant_id,
            review.candidate_id,
        )
        if candidate is None:
            raise TrainingDataError("Hard-negative candidate does not exist")
        if not candidate.is_valid:
            raise TrainingDataError("Invalid hard-negative candidates cannot be reviewed")
        if candidate.status is not CandidateReviewStatus.PENDING:
            if candidate.review == review:
                return candidate
            raise TrainingDataError("Hard-negative candidate already has a final decision")
        return await self._training_data.review_candidate(review)
