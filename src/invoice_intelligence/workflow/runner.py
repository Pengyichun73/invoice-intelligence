"""Start and resume helpers that preserve LangGraph identity semantics."""

from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from invoice_intelligence.application.errors import WorkflowIdentityError
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.workflow import (
    HumanCorrection,
    WorkflowIdentity,
    WorkflowStatus,
)
from invoice_intelligence.workflow.state import (
    GraphState,
    human_correction_to_state,
    initial_graph_state,
)


class InvoiceWorkflowRunner:
    """Invoke a compiled graph with thread_id only in checkpoint configuration."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def start(
        self,
        identity: WorkflowIdentity,
        document: DocumentReference,
        tenant_id: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a new run under its dedicated checkpoint thread."""

        config = self._config(identity.thread_id)
        snapshot = await self._graph.aget_state(config)
        if snapshot.values:
            raise WorkflowIdentityError("thread_id already contains checkpoint state")
        return cast(
            dict[str, Any],
            await self._graph.ainvoke(
                initial_graph_state(identity, document, tenant_id, trace_id=trace_id),
                config=config,
            ),
        )

    async def resume(
        self,
        identity: WorkflowIdentity,
        correction: HumanCorrection,
    ) -> dict[str, Any]:
        """Resume exactly one interrupted run with Command(resume=HumanCorrection state)."""

        config = self._config(identity.thread_id)
        snapshot = await self._graph.aget_state(config)
        values = cast(GraphState, snapshot.values)
        self._ensure_identity(values, identity)
        if values.get("status") != WorkflowStatus.PENDING_REVIEW.value or not snapshot.interrupts:
            raise WorkflowIdentityError("Workflow is not waiting for human review")
        return cast(
            dict[str, Any],
            await self._graph.ainvoke(
                Command(resume=human_correction_to_state(correction)),
                config=config,
            ),
        )

    async def start_or_continue(
        self,
        identity: WorkflowIdentity,
        document: DocumentReference,
        tenant_id: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        config = self._config(identity.thread_id)
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            return await self.start(identity, document, tenant_id, trace_id)
        values = cast(GraphState, snapshot.values)
        self._ensure_identity(values, identity)
        if values.get("status") in {
            WorkflowStatus.PENDING_REVIEW.value,
            WorkflowStatus.COMPLETED.value,
            WorkflowStatus.FAILED.value,
        }:
            return cast(dict[str, Any], values)
        return cast(dict[str, Any], await self._graph.ainvoke(None, config=config))

    async def resume_or_continue(
        self,
        identity: WorkflowIdentity,
        correction: HumanCorrection,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        config = self._config(identity.thread_id)
        snapshot = await self._graph.aget_state(config)
        values = cast(GraphState, snapshot.values)
        self._ensure_identity(values, identity)
        if values.get("status") in {WorkflowStatus.COMPLETED.value, WorkflowStatus.FAILED.value}:
            return cast(dict[str, Any], values)
        if snapshot.interrupts:
            if self._checkpoint_id(snapshot.config) != checkpoint_id:
                return cast(dict[str, Any], values)
            return await self.resume(identity, correction)
        return cast(dict[str, Any], await self._graph.ainvoke(None, config=config))

    async def get_pending_checkpoint_id(self, identity: WorkflowIdentity) -> str:
        snapshot = await self._graph.aget_state(self._config(identity.thread_id))
        values = cast(GraphState, snapshot.values)
        self._ensure_identity(values, identity)
        if not snapshot.interrupts:
            raise WorkflowIdentityError("Workflow is not waiting for human review")
        checkpoint_id = self._checkpoint_id(snapshot.config)
        if checkpoint_id is None:
            raise WorkflowIdentityError("Pending review checkpoint has no identity")
        return checkpoint_id

    @staticmethod
    def _checkpoint_id(config: RunnableConfig) -> str | None:
        value = config.get("configurable", {}).get("checkpoint_id")
        return value if isinstance(value, str) and value else None

    async def get_state(self, identity: WorkflowIdentity) -> dict[str, Any]:
        """Read one checkpoint state after validating all three identifiers."""

        snapshot = await self._graph.aget_state(self._config(identity.thread_id))
        values = cast(GraphState, snapshot.values)
        self._ensure_identity(values, identity)
        return cast(dict[str, Any], values)

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _ensure_identity(state: GraphState, identity: WorkflowIdentity) -> None:
        if (
            state.get("thread_id") != identity.thread_id
            or state.get("run_id") != identity.run_id
            or state.get("document_id") != identity.document_id
        ):
            raise WorkflowIdentityError("Workflow identity does not match checkpoint state")
