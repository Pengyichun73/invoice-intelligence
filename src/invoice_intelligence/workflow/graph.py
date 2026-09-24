"""Deterministic StateGraph definition for invoice extraction and review."""

from collections.abc import Callable
from inspect import isawaitable
from typing import Any, Literal, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.domain.workflow import WorkflowStatus
from invoice_intelligence.workflow.nodes import InvoiceWorkflowNodes, WorkflowDependencies
from invoice_intelligence.workflow.state import GraphState, trace_id_from_state

InvoiceT = TypeVar("InvoiceT")
NodeName = Literal[
    "prepare_document",
    "retrieve_correction_context",
    "extract_invoice",
    "validate_extraction",
    "request_human_review",
    "apply_human_correction",
    "persist_result",
    "save_correction_memory",
]


def _route_next(state: GraphState, next_node: NodeName) -> NodeName | Literal["__end__"]:
    return END if state["status"] == WorkflowStatus.FAILED.value else next_node


def _route_after_validation(
    state: GraphState,
) -> Literal["request_human_review", "persist_result", "__end__"]:
    if state["status"] == WorkflowStatus.FAILED.value:
        return END
    if state["status"] == WorkflowStatus.PENDING_REVIEW.value:
        return "request_human_review"
    return "persist_result"


def _route_after_extraction(
    state: GraphState,
) -> Literal["retrieve_correction_context", "validate_extraction", "__end__"]:
    if state["status"] == WorkflowStatus.FAILED.value:
        return END
    if state["correction_context_retrieved"]:
        return "validate_extraction"
    return "retrieve_correction_context"


def _route_after_context(
    state: GraphState,
) -> Literal["extract_invoice", "validate_extraction", "__end__"]:
    if state["status"] == WorkflowStatus.FAILED.value:
        return END
    reviewed_context = state["reviewed_example_context"]
    has_reviewed_examples = bool(
        reviewed_context
        and any(
            reviewed_context.get(key)
            for key in (
                "verified_correct_examples",
                "reviewed_correction_examples",
                "reviewed_negative_examples",
            )
        )
    )
    if state["correction_context"] or has_reviewed_examples:
        return "extract_invoice"
    return "validate_extraction"


def _route_after_review(
    state: GraphState,
) -> Literal["request_human_review", "apply_human_correction", "__end__"]:
    if state["status"] == WorkflowStatus.FAILED.value:
        return END
    if state["human_correction"] is None:
        return "request_human_review"
    return "apply_human_correction"


def _route_after_correction(
    state: GraphState,
) -> Literal["request_human_review", "validate_extraction", "__end__"]:
    if state["status"] == WorkflowStatus.FAILED.value:
        return END
    if state["status"] == WorkflowStatus.PENDING_REVIEW.value:
        return "request_human_review"
    return "validate_extraction"


def build_invoice_workflow(
    dependencies: WorkflowDependencies[InvoiceT],
    checkpointer: BaseCheckpointSaver[str],
) -> CompiledStateGraph:
    """Compile one sequential graph with Python-only conditional routing."""

    nodes = InvoiceWorkflowNodes(dependencies)
    builder = StateGraph(GraphState)
    telemetry = dependencies.privacy_telemetry
    builder.add_node("prepare_document", nodes.prepare_document)
    builder.add_node(
        "retrieve_correction_context",
        _traced_node(
            nodes.retrieve_correction_context,
            TraceStage.RETRIEVAL,
            "retrieve_correction_context",
            telemetry,
        ),
    )
    builder.add_node("extract_invoice", nodes.extract_invoice)
    builder.add_node(
        "validate_extraction",
        _traced_node(
            nodes.validate_extraction,
            TraceStage.VALIDATION,
            "validate_extraction",
            telemetry,
        ),
    )
    builder.add_node("request_human_review", nodes.request_human_review)
    builder.add_node(
        "apply_human_correction",
        _traced_node(
            nodes.apply_human_correction,
            TraceStage.HUMAN_REVIEW,
            "apply_human_correction",
            telemetry,
        ),
    )
    builder.add_node("persist_result", nodes.persist_result)
    builder.add_node(
        "save_correction_memory",
        _traced_node(
            nodes.save_correction_memory,
            TraceStage.MEMORY_ADMISSION,
            "save_correction_memory",
            telemetry,
        ),
    )

    builder.add_edge(START, "prepare_document")
    builder.add_conditional_edges(
        "prepare_document",
        lambda state: _route_next(state, "extract_invoice"),
    )
    builder.add_conditional_edges("extract_invoice", _route_after_extraction)
    builder.add_conditional_edges("retrieve_correction_context", _route_after_context)
    builder.add_conditional_edges("validate_extraction", _route_after_validation)
    builder.add_conditional_edges("request_human_review", _route_after_review)
    builder.add_conditional_edges("apply_human_correction", _route_after_correction)
    builder.add_conditional_edges(
        "persist_result",
        lambda state: _route_next(state, "save_correction_memory"),
    )
    builder.add_edge("save_correction_memory", END)
    return builder.compile(checkpointer=checkpointer)


def _traced_node(
    node: Callable[[GraphState], Any],
    stage: TraceStage,
    operation: str,
    telemetry: PrivacyTelemetry | None,
) -> Any:
    if telemetry is None:
        return node

    async def invoke(state: GraphState, config: RunnableConfig) -> Any:
        del config
        with telemetry.span(
            trace_id=trace_id_from_state(state),
            tenant_id=state["tenant_id"],
            stage=stage,
            operation=operation,
            attributes={
                "run_id": state["run_id"],
                "document_id": state["document_id"],
            },
        ):
            result = node(state)
            return await result if isawaitable(result) else result

    return invoke
