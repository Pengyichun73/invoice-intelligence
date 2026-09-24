"""Deterministic orchestration for the fixed Harness stages."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

from ..application.ports.code_index import (
    CodeContext,
    CodeIndex,
    CodeIndexScope,
    CodeQuery,
)
from ..application.ports.code_model import CodeGenerationRequest, CodeModel
from ..application.ports.parser import CodeParser, ParsedArtifact
from ..application.ports.sandbox import SandboxExecutor, SandboxPolicy
from ..domain.errors import HarnessError, HarnessErrorCode
from ..domain.execution import HarnessTaskStatus
from ..domain.patch_model import PatchProposal
from ..domain.repository import RepositorySnapshot, RepositorySource
from ..domain.watchdog import WatchdogObservation, WatchdogOutcome
from ..application.services.repository_inspection import RepositoryInspectionService
from ..application.ports.facts import HarnessFactRepository
from ..application.ports.harness_observability import HarnessObservability
from ..infrastructure.patching.structured_patch import validate_patch
from .policy import route_after_watchdog
from .runner import HarnessRunner
from .state import HarnessStage, HarnessState


@dataclass(slots=True)
class HarnessWorkflowDependencies:
    """Composition-root dependencies; absent external providers fail closed."""

    repository_inspection: RepositoryInspectionService
    sources: Mapping[tuple[str, str], RepositorySource]
    parser: CodeParser | None = None
    code_index: CodeIndex | None = None
    local_code_index_factory: Callable[
        [RepositorySnapshot, tuple[ParsedArtifact, ...], Mapping[str, bytes]],
        CodeIndex,
    ] | None = None
    code_model: CodeModel | None = None
    sandbox: SandboxExecutor | None = None
    sandbox_policy: SandboxPolicy | None = None
    observability: HarnessObservability | None = None
    facts: HarnessFactRepository | None = None
    # 仅用于单进程编排期间传递不可变快照；事实仍必须由持久化层保存。
    _snapshots: dict[
        str,
        tuple[RepositorySnapshot, Mapping[str, bytes], tuple[ParsedArtifact, ...]],
    ] = field(
        default_factory=dict, init=False, repr=False
    )
    _snapshot_indexes: dict[str, CodeIndex] = field(default_factory=dict, init=False, repr=False)
    _patches: dict[str, PatchProposal] = field(default_factory=dict, init=False, repr=False)
    _context_payloads: dict[str, tuple[CodeContext, ...]] = field(
        default_factory=dict, init=False, repr=False
    )


class HarnessWorkflowService:
    """Run one bounded task; no handler can create an unlisted stage."""

    def __init__(self, dependencies: HarnessWorkflowDependencies) -> None:
        self._dependencies = dependencies
        self._runner = HarnessRunner(
            {
                HarnessStage.PREPARE_TASK: self._prepare_task,
                HarnessStage.INSPECT_REPOSITORY: self._inspect_repository,
                HarnessStage.RETRIEVE_CODE_CONTEXT: self._retrieve_code_context,
                HarnessStage.GENERATE_PATCH: self._generate_patch,
                HarnessStage.VALIDATE_PATCH: self._validate_patch,
                HarnessStage.EXECUTE_IN_SANDBOX: self._execute_in_sandbox,
                HarnessStage.EVALUATE_RESULT: self._evaluate_result,
                HarnessStage.REPAIR_OR_FINISH: self._repair_or_finish,
            }
        )

    async def run(self, state: HarnessState) -> HarnessState:
        if self._dependencies.observability is None:
            return await self._runner.run(state)
        with self._dependencies.observability.span(
            trace_id=state.trace_id,
            tenant_id=state.tenant_id,
            stage=f"code_harness.{state.stage.value}",
            operation="workflow",
            attributes=_version_attributes(state),
        ):
            return await self._runner.run(state)

    async def _prepare_task(self, state: HarnessState) -> HarnessState:
        if state.attempt_count > state.budget.max_attempts:
            return state.next(
                HarnessStage.REPAIR_OR_FINISH,
                status=HarnessTaskStatus.QUARANTINED,
                error_code=HarnessErrorCode.BUDGET_EXHAUSTED,
            )
        return state.next(HarnessStage.INSPECT_REPOSITORY)

    async def _inspect_repository(self, state: HarnessState) -> HarnessState:
        source = self._dependencies.sources.get((state.tenant_id, state.repository_id))
        if source is None:
            return self._fail(state, HarnessErrorCode.SNAPSHOT_NOT_FOUND)
        try:
            with self._span(state, "inspect_repository", "snapshot_capture"):
                snapshot, contents = (
                    self._dependencies.repository_inspection.capture_snapshot_with_contents(
                        source=source,
                        snapshot_id=state.snapshot_id or state.task_id,
                        revision=state.snapshot_revision or 1,
                        versions=state.versions,
                    )
                )
            if (
                snapshot.tenant_id != state.tenant_id
                or snapshot.repository_id != state.repository_id
            ):
                return self._fail(state, HarnessErrorCode.TENANT_SCOPE_MISMATCH)
            if self._dependencies.facts is not None:
                await self._dependencies.facts.save_snapshot(
                    snapshot,
                    created_at=datetime.now(UTC),
                )
            if self._dependencies.parser is None:
                return self._fail(state, HarnessErrorCode.PARSER_NOT_CONFIGURED)
            with self._span(state, "inspect_repository", "parser"):
                parsed = await self._dependencies.parser.parse(
                    snapshot_id=snapshot.snapshot_id,
                    files=snapshot.files,
                    contents=contents,
                    versions=state.versions,
                )
            self._dependencies._snapshots[state.task_id] = (snapshot, contents, parsed)
            if (
                self._dependencies.code_index is None
                and self._dependencies.local_code_index_factory is not None
            ):
                self._dependencies._snapshot_indexes[state.task_id] = (
                    self._dependencies.local_code_index_factory(snapshot, parsed, contents)
                )
            return state.next(
                HarnessStage.RETRIEVE_CODE_CONTEXT,
                snapshot_id=snapshot.snapshot_id,
                snapshot_revision=snapshot.revision,
                source_revision=snapshot.source_revision,
                affected_language=_parsed_language(parsed),
                affected_symbol_kind=_parsed_symbol_kind(parsed),
            )
        except HarnessError as exc:
            return self._fail(state, exc.code)
        except (OSError, ValueError):
            return self._fail(state, HarnessErrorCode.SNAPSHOT_NOT_FOUND)

    async def _retrieve_code_context(self, state: HarnessState) -> HarnessState:
        record = self._dependencies._snapshots.get(state.task_id)
        if record is None:
            return self._fail(state, HarnessErrorCode.SNAPSHOT_NOT_FOUND)
        code_index = self._dependencies.code_index or self._dependencies._snapshot_indexes.get(
            state.task_id
        )
        if code_index is None:
            return self._fail(state, HarnessErrorCode.INDEX_PROVIDER_NOT_CONFIGURED)
        snapshot = record[0]
        scope = CodeIndexScope(
            tenant_id=state.tenant_id,
            repository_id=state.repository_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_revision=snapshot.revision,
            source_revision=snapshot.source_revision,
            schema_version=state.versions.schema_version,
            parser_version=state.versions.parser_version,
            grammar_version=state.versions.grammar_version,
            redaction_version=state.versions.redaction_version,
            index_version=state.versions.index_version,
        )
        try:
            with self._span(state, "retrieve_code_context", "code_index"):
                query = CodeQuery(
                    keyword=state.error_code.value
                    if state.error_code
                    else state.repository_id,
                )
                hits = await code_index.search_structured(
                    scope=scope,
                    query=query,
                    limit=8,
                )
        except (HarnessError, OSError, TimeoutError, ValueError):
            return self._fail(state, HarnessErrorCode.CODE_INDEX_UNAVAILABLE)
        try:
            with self._span(state, "retrieve_code_context", "code_context"):
                payloads_list: list[CodeContext] = []
                for hit in hits[:8]:
                    payloads_list.append(
                        await code_index.read_context(
                            scope=scope,
                            hit=hit,
                            max_bytes=16_000,
                            context_lines=8,
                        )
                    )
                payloads = tuple(payloads_list)
        except (HarnessError, OSError, TimeoutError, ValueError, AttributeError):
            return self._fail(state, HarnessErrorCode.CODE_INDEX_UNAVAILABLE)
        self._dependencies._context_payloads[state.task_id] = payloads
        return state.next(
            HarnessStage.GENERATE_PATCH,
            context_refs=tuple(hit.chunk_id for hit in hits[:8]),
        )

    async def _generate_patch(self, state: HarnessState) -> HarnessState:
        if self._dependencies.code_model is None:
            return self._fail(state, HarnessErrorCode.MODEL_PROVIDER_NOT_CONFIGURED)
        record = self._dependencies._snapshots.get(state.task_id)
        if record is None:
            return self._fail(state, HarnessErrorCode.SNAPSHOT_NOT_FOUND)
        try:
            with self._span(state, "generate_patch", "code_model"):
                proposal = await self._dependencies.code_model.generate_patch(
                    CodeGenerationRequest(
                        task_id=state.task_id,
                        snapshot=record[0],
                        context_refs=state.context_refs,
                        failure_signature=state.error_signature,
                        max_model_tokens=state.budget.max_model_tokens,
                        context_payloads=self._dependencies._context_payloads.get(
                            state.task_id,
                            (),
                        ),
                    )
                )
        except (HarnessError, OSError, TimeoutError, ValueError):
            return self._fail(state, HarnessErrorCode.MODEL_PROVIDER_NOT_CONFIGURED)
        if proposal.tenant_id != state.tenant_id or proposal.repository_id != state.repository_id:
            return self._fail(state, HarnessErrorCode.TENANT_SCOPE_MISMATCH)
        if self._dependencies.facts is not None:
            await self._dependencies.facts.save_patch(
                proposal,
                created_at=datetime.now(UTC),
            )
        self._dependencies._patches[state.task_id] = proposal
        return state.next(
            HarnessStage.VALIDATE_PATCH,
            patch_id=proposal.patch_id,
            patch_shape=_patch_shape(proposal),
            prior_patch_fingerprints=(
                *state.prior_patch_fingerprints,
                proposal.fingerprint,
            )[-state.budget.max_attempts :],
        )

    async def _validate_patch(self, state: HarnessState) -> HarnessState:
        record = self._dependencies._snapshots.get(state.task_id)
        proposal = self._dependencies._patches.get(state.task_id)
        if record is None or proposal is None:
            return self._fail(state, HarnessErrorCode.PATCH_INVALID)
        with self._span(state, "validate_patch", "patch_validator"):
            result = validate_patch(
                snapshot=record[0],
                proposal=proposal,
                contents=record[1],
                max_patch_bytes=state.budget.max_patch_bytes,
                max_changed_files=state.budget.max_changed_files,
                max_changed_lines=state.budget.max_changed_lines,
                max_patch_operations=state.budget.max_patch_operations,
            )
        if not result.valid:
            return self._fail(
                state,
                HarnessErrorCode(result.error_code or HarnessErrorCode.PATCH_INVALID.value),
            )
        return state.next(
            HarnessStage.EXECUTE_IN_SANDBOX,
            changed_ast_fingerprint=bool(result.ast_changed_files),
            changed_symbols=bool(result.changed_files),
        )

    async def _execute_in_sandbox(self, state: HarnessState) -> HarnessState:
        if self._dependencies.sandbox is None or self._dependencies.sandbox_policy is None:
            return self._fail(state, HarnessErrorCode.SANDBOX_NOT_CONFIGURED)
        record = self._dependencies._snapshots.get(state.task_id)
        proposal = self._dependencies._patches.get(state.task_id)
        if record is None or proposal is None:
            return self._fail(state, HarnessErrorCode.PATCH_INVALID)
        try:
            with self._span(state, "execute_in_sandbox", "sandbox"):
                result = await self._dependencies.sandbox.execute(
                    snapshot=record[0],
                    patch=proposal,
                    policy=self._dependencies.sandbox_policy,
                )
        except (HarnessError, OSError, TimeoutError, ValueError):
            return self._fail(state, HarnessErrorCode.SANDBOX_UNAVAILABLE)
        if self._dependencies.facts is not None:
            await self._dependencies.facts.save_execution(
                execution_id=_execution_id(state.task_id, state.attempt_count),
                task_id=state.task_id,
                attempt_id=f"{state.task_id}:{state.attempt_count}",
                status="succeeded" if result.succeeded else "failed",
                result_summary=(
                    "sandbox execution succeeded"
                    if result.succeeded
                    else "sandbox execution failed"
                ),
                error_signature=(
                    None
                    if result.succeeded
                    else _stable_signature(result.error_code or "hard_failure", result.diagnostics)
                ),
                created_at=datetime.now(UTC),
            )
        if result.succeeded:
            return state.next(
                HarnessStage.EVALUATE_RESULT,
                error_code=None,
                changed_diagnostics=True,
            )
        code = _error_code(result.error_code)
        signature = _stable_signature(code.value, result.diagnostics)
        previous_signature = state.error_signatures[-1] if state.error_signatures else None
        return state.next(
            HarnessStage.EVALUATE_RESULT,
            error_code=code,
            error_signature=signature,
            changed_diagnostics=signature != previous_signature,
            error_signatures=(*state.error_signatures, signature)[-state.budget.max_attempts :],
        )

    async def _evaluate_result(self, state: HarnessState) -> HarnessState:
        if state.error_code is None:
            if self._dependencies.facts is not None:
                await self._dependencies.facts.save_watchdog(
                    observation_id=f"{state.task_id}:{state.attempt_count}:watchdog",
                    task_id=state.task_id,
                    observation=WatchdogObservation(
                        attempt_id=f"{state.task_id}:{state.attempt_count}",
                        outcome=WatchdogOutcome.SUCCESS,
                        error_signature=None,
                        changed_ast_fingerprint=state.changed_ast_fingerprint,
                        changed_symbols=state.changed_symbols,
                        changed_diagnostics=state.changed_diagnostics,
                    ),
                    created_at=datetime.now(UTC),
                )
            return state.next(
                HarnessStage.REPAIR_OR_FINISH,
                status=HarnessTaskStatus.SUCCEEDED,
            )
        repeated = (
            len(state.error_signatures) >= 2
            and state.error_signatures[-1] == state.error_signatures[-2]
        )
        same_patch = (
            len(state.prior_patch_fingerprints) >= 2
            and state.prior_patch_fingerprints[-1]
            == state.prior_patch_fingerprints[-2]
        )
        same_error = (
            len(state.error_signatures) >= 2
            and state.error_signatures[-1] == state.error_signatures[-2]
        )
        materially_changed = (
            not same_error
            and (
                state.changed_ast_fingerprint
                or state.changed_symbols
                or state.changed_diagnostics
            )
        )
        if state.error_code in {
            HarnessErrorCode.SANDBOX_NOT_CONFIGURED,
            HarnessErrorCode.SANDBOX_UNAVAILABLE,
            HarnessErrorCode.PATH_OUTSIDE_REPOSITORY,
            HarnessErrorCode.RESOURCE_EXHAUSTED,
            HarnessErrorCode.TENANT_SCOPE_MISMATCH,
        }:
            outcome = WatchdogOutcome.HARD_FAILURE
        elif same_patch and same_error:
            outcome = WatchdogOutcome.EXACT_REPEAT
        elif materially_changed:
            outcome = WatchdogOutcome.JITTER
        else:
            outcome = WatchdogOutcome.STALL
        observation = WatchdogObservation(
            attempt_id=f"{state.task_id}:{state.attempt_count}",
            outcome=outcome,
            error_signature=state.error_signature,
            changed_ast_fingerprint=state.changed_ast_fingerprint,
            changed_symbols=state.changed_symbols,
            changed_diagnostics=state.changed_diagnostics,
            failure_code=state.error_code,
        )
        if self._dependencies.facts is not None:
            await self._dependencies.facts.save_watchdog(
                observation_id=f"{state.task_id}:{state.attempt_count}:watchdog",
                task_id=state.task_id,
                observation=observation,
                created_at=datetime.now(UTC),
            )
        with self._span(state, "evaluate_result", "watchdog"):
            route = route_after_watchdog(
                observation=observation,
                attempt_count=state.attempt_count + 1,
                budget=state.budget,
            )
        return state.next(
            HarnessStage.REPAIR_OR_FINISH,
            status=route.status,
            error_code=route.failure_code or state.error_code,
        )

    async def _repair_or_finish(self, state: HarnessState) -> HarnessState:
        if state.status in {
            HarnessTaskStatus.SUCCEEDED,
            HarnessTaskStatus.QUARANTINED,
            HarnessTaskStatus.FAILED,
        }:
            self._dependencies._snapshots.pop(state.task_id, None)
            self._dependencies._snapshot_indexes.pop(state.task_id, None)
            self._dependencies._patches.pop(state.task_id, None)
            self._dependencies._context_payloads.pop(state.task_id, None)
            return state
        return state.next(
            HarnessStage.RETRIEVE_CODE_CONTEXT,
            attempt_count=state.attempt_count + 1,
            status=HarnessTaskStatus.REPAIR_PENDING,
        )

    @staticmethod
    def _fail(state: HarnessState, code: HarnessErrorCode) -> HarnessState:
        signature = _stable_signature(code.value, ())
        return state.next(
            HarnessStage.REPAIR_OR_FINISH,
            status=HarnessTaskStatus.QUARANTINED
            if code
            in {
                HarnessErrorCode.SANDBOX_NOT_CONFIGURED,
                HarnessErrorCode.SANDBOX_UNAVAILABLE,
                HarnessErrorCode.PATH_OUTSIDE_REPOSITORY,
                HarnessErrorCode.RESOURCE_EXHAUSTED,
                HarnessErrorCode.TENANT_SCOPE_MISMATCH,
                HarnessErrorCode.PARSER_NOT_CONFIGURED,
                HarnessErrorCode.INDEX_PROVIDER_NOT_CONFIGURED,
                HarnessErrorCode.MODEL_PROVIDER_NOT_CONFIGURED,
            }
            else HarnessTaskStatus.REPAIR_PENDING,
            error_code=code,
            error_signature=signature,
            error_signatures=(*state.error_signatures, signature)[-state.budget.max_attempts :],
        )

    def _span(
        self,
        state: HarnessState,
        stage: str,
        operation: str,
    ) -> AbstractContextManager[None]:
        observability = self._dependencies.observability
        if observability is None:
            return nullcontext()
        return observability.span(
            trace_id=state.trace_id,
            tenant_id=state.tenant_id,
            stage=f"code_harness.{stage}",
            operation=operation,
            attributes=_version_attributes(state),
        )


def _stable_signature(code: str, diagnostics: tuple[str, ...]) -> str:
    normalized = "|".join(item.strip()[:256] for item in diagnostics)
    return sha256(f"{code}|{normalized}".encode("utf-8")).hexdigest()


def _execution_id(task_id: str, attempt_count: int) -> str:
    return sha256(f"{task_id}|{attempt_count}".encode("utf-8")).hexdigest()


def _error_code(value: str | None) -> HarnessErrorCode:
    if value is None:
        return HarnessErrorCode.HARD_FAILURE
    try:
        return HarnessErrorCode(value)
    except ValueError:
        return HarnessErrorCode.HARD_FAILURE


def _version_attributes(state: HarnessState) -> dict[str, str | int]:
    return {
        "attempt_count": state.attempt_count,
        "schema_version": state.versions.schema_version,
        "parser_version": state.versions.parser_version,
        "grammar_version": state.versions.grammar_version,
        "index_version": state.versions.index_version,
        "model_version": state.versions.model_version,
        "prompt_version": state.versions.prompt_version,
        "patch_policy_version": state.versions.patch_policy_version,
        "sandbox_policy_version": state.versions.sandbox_policy_version,
        "watchdog_version": state.versions.watchdog_version,
    }


def _parsed_language(parsed: tuple[ParsedArtifact, ...]) -> str | None:
    languages = {
        symbol.language
        for artifact in parsed
        for symbol in artifact.symbols
        if symbol.symbol_kind != "module"
    }
    if not languages:
        languages = {symbol.language for artifact in parsed for symbol in artifact.symbols}
    return next(iter(languages)) if len(languages) == 1 else None


def _parsed_symbol_kind(parsed: tuple[ParsedArtifact, ...]) -> str | None:
    kinds = {
        symbol.symbol_kind
        for artifact in parsed
        for symbol in artifact.symbols
        if symbol.symbol_kind != "module"
    }
    return next(iter(kinds)) if len(kinds) == 1 else None


def _patch_shape(proposal: PatchProposal) -> str:
    return "+".join(sorted({operation.kind.value for operation in proposal.operations}))
