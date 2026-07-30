from __future__ import annotations

from time import perf_counter

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.services.search_core.collection import assess_scoped_collection
from app.services.search_core.judgment import assess_evidence
from app.services.search_core.planner import extend_plan_for_retry, plan_queries
from app.services.search_core.retrieval import (
    build_evidence_packets,
    retrieve_candidates,
)
from app.services.search_core.state_engine import assess_current_state
from app.services.search_core.types import SearchState

settings = get_settings()


def _updated_timings(
    state: SearchState, step: str, started_at: float
) -> dict[str, float]:
    timings = dict(state.get("timings_ms", {}))
    elapsed_ms = (perf_counter() - started_at) * 1000
    timings[step] = round(timings.get(step, 0.0) + elapsed_ms, 2)
    return timings


def _plan_node(state: SearchState) -> dict:
    started_at = perf_counter()
    plan = plan_queries(state["question"], state["history"])
    return {
        "plan": plan,
        "timings_ms": _updated_timings(state, "plan", started_at),
    }


def _retrieve_node(state: SearchState) -> dict:
    retrieve_started_at = perf_counter()
    candidates = retrieve_candidates(state["db"], state["project_id"], state["plan"])
    timings = _updated_timings(state, "retrieve_candidates", retrieve_started_at)
    evidence_started_at = perf_counter()
    packets = build_evidence_packets(
        state["db"], state["project_id"], state["plan"], candidates
    )
    timing_state = dict(state)
    timing_state["timings_ms"] = timings
    return {
        "packets": packets,
        "candidate_count": len(candidates),
        "timings_ms": _updated_timings(
            timing_state, "build_evidence", evidence_started_at
        ),
    }


def _assess_node(state: SearchState) -> dict:
    started_at = perf_counter()
    collection_assessment = assess_scoped_collection(
        state["plan"], state["packets"]
    )
    assessment = collection_assessment or assess_evidence(
        state["plan"], state["packets"], state.get("current_state")
    )
    return {
        "assessment": assessment,
        "timings_ms": _updated_timings(state, "assess", started_at),
    }


def _state_node(state: SearchState) -> dict:
    started_at = perf_counter()
    current_state = assess_current_state(state["plan"], state["packets"])
    return {
        "current_state": current_state,
        "timings_ms": _updated_timings(state, "state", started_at),
    }


def _route_after_assessment(state: SearchState) -> str:
    assessment = state["assessment"]
    plan = state["plan"]
    if not assessment.retry_queries:
        return "finish"
    if plan.task_mode == "compose" and plan.scope_mode == "collection":
        return "finish"
    if state.get("retry_count", 0) >= settings.search_max_retries:
        return "finish"
    if assessment.status == "insufficient":
        return "retry"
    if assessment.status == "conflict":
        cited_ids = set(assessment.cited_document_ids)
        cited_sources = {
            packet.document.source_type
            for packet in state.get("packets", [])
            if packet.document.id in cited_ids
        }
        if len(cited_sources) < 2:
            return "retry"
    return "finish"


def _retry_node(state: SearchState) -> dict:
    plan = state["plan"]
    plan = extend_plan_for_retry(plan, state["assessment"].retry_queries)
    return {"plan": plan, "retry_count": state.get("retry_count", 0) + 1}


def build_search_graph():
    graph = StateGraph(SearchState)
    graph.add_node("plan", _plan_node)
    graph.add_node("retrieve", _retrieve_node)
    graph.add_node("state", _state_node)
    graph.add_node("assess", _assess_node)
    graph.add_node("retry", _retry_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "state")
    graph.add_edge("state", "assess")
    graph.add_conditional_edges(
        "assess",
        _route_after_assessment,
        {"retry": "retry", "finish": END},
    )
    graph.add_edge("retry", "retrieve")
    return graph.compile()


search_graph = build_search_graph()
