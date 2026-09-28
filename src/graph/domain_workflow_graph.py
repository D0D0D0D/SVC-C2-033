"""AgentCore Platform v1.0 — inner domain workflow (SVC-C2-033).

TourPlanningWorkflow is the inner BaseGraph wrapped by TourPlanningGraphNode
(the outer `main` slot). It implements the linear-with-HITL topology:

    START -> internal_kb_load -> market_data_fetch -> plan_draft
          -> hitl_review (interrupt) -> supplier_match -> procurement_route
          -> document_generate -> END

HITL / checkpointer (design H-01): this graph is compiled with an explicit
checkpointer by the outer GraphNode.get_subgraph() — the wheel does NOT auto-
attach one from config. Its own config carries memory_enabled / hitl so
invoke() attaches a thread_config on the HITL path.

Services are injected via config so graph.py / tests control them.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.inner_nodes import (
    DocumentGenerateNode,
    HitlReviewNode,
    InternalKBLoadNode,
    MarketDataFetchNode,
    PlanDraftNode,
    ProcurementRouteNode,
    SupplierMatchNode,
)
from src.schemas.state import State


class TourPlanningWorkflow(BaseGraph):
    """Inner multi-step tour-planning workflow with a HITL approval step."""

    @property
    def name(self) -> str:
        return "tour_planning_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # Services are required for a functional workflow; a missing service is a
        # construction error rather than a runtime surprise.
        for key in ("kb_service", "supplier_service", "market_service", "plan_service"):
            if self.config.get(key) is None:
                raise ValueError(f"TourPlanningWorkflow: missing required config '{key}'")

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract. No initialize/finalize
        # (those are outer backbone concerns).
        kb: Any = self.config["kb_service"]
        suppliers: Any = self.config["supplier_service"]
        market: Any = self.config["market_service"]
        planner: Any = self.config["plan_service"]

        self._nodes["internal_kb_load"] = InternalKBLoadNode(kb)
        self._nodes["market_data_fetch"] = MarketDataFetchNode(market)
        self._nodes["plan_draft"] = PlanDraftNode(planner)
        self._nodes["hitl_review"] = HitlReviewNode()
        self._nodes["supplier_match"] = SupplierMatchNode(suppliers)
        self._nodes["procurement_route"] = ProcurementRouteNode()
        self._nodes["document_generate"] = DocumentGenerateNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "internal_kb_load")
        self._sg.add_edge("internal_kb_load", "market_data_fetch")
        self._sg.add_edge("market_data_fetch", "plan_draft")
        self._sg.add_edge("plan_draft", "hitl_review")
        self._sg.add_edge("hitl_review", "supplier_match")
        self._sg.add_edge("supplier_match", "procurement_route")
        self._sg.add_edge("procurement_route", "document_generate")
        self._sg.add_edge("document_generate", END)

    def route(self, state) -> str:
        """Required by BaseGraph ABC. Linear topology → not wired to a
        conditional edge, but implemented for completeness."""
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "document_generate"

    def get_output(self, state) -> dict:
        """Shape sub_result for the outer TourPlanningGraphNode.merge_output().

        Includes HITL fields so the outer graph can persist them across
        checkpoint/resume (design C-3 / M fix).
        """
        return {
            "output": state.get("result"),
            "result": state.get("result"),
            "status": state.get("status"),
            "plan_draft": state.get("plan_draft"),
            "procurement_refs": state.get("procurement_refs", []),
            "supplier_match_refs": state.get("supplier_match_refs", []),
            "kb_signals": state.get("kb_signals", {}),
            "hitl_draft": state.get("hitl_draft"),
            "hitl_feedback": state.get("hitl_feedback"),
            "hitl_status": state.get("hitl_status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
        }
