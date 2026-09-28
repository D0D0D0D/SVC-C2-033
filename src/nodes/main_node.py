"""AgentCore Platform v1.0 — main GraphNode (SVC-C2-033).

TourPlanningGraphNode occupies the outer `main` slot. It wraps the inner
TourPlanningWorkflow and:
  - serializes the outer planning context into the inner graph's string input
    (extract_input) — the inner graph receives only user_input + ctx,
  - builds the inner graph WITH AN EXPLICIT CHECKPOINTER (get_subgraph) so HITL
    interrupt/resume works (design H-01: the wheel does not auto-attach one),
  - merges inner results back, INCLUDING HITL fields, so resume keeps the draft
    and human feedback (design C-3),
  - propagates inner HITL to the outer caller (propagate_hitl=True),
  - fails fast on inner errors (error_strategy="propagate") and passes an
    upstream error straight through (pre->main is an unconditional edge).

The inner graph's services + checkpointer factory are injected via the outer
graph config so tests can inject fakes.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.domain_workflow_graph import TourPlanningWorkflow


class TourPlanningGraphNode(GraphNode):
    """Outer `main` node: wraps the inner tour-planning workflow."""

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = True

    def __init__(self, inner_config: dict | None = None) -> None:
        super().__init__()
        # inner_config carries the injected services + a checkpointer_factory.
        self._inner_config = inner_config or {}
        # Cached compiled inner graph. MUST be stable across get_subgraph() calls:
        # HITL resume relies on the SAME checkpointer instance that held the
        # suspended checkpoint. Rebuilding a fresh graph+saver per call would make
        # subgraph.resume() find no checkpoint → re-run from scratch → never complete.
        self._subgraph = None

    def execute(self, state: AgentState) -> dict:
        # pre_process -> main is an unconditional edge; pass an upstream error
        # straight through instead of running the subgraph on a failed state.
        if state.get("status") == AgentStatus.ERROR.value and not state.get("subgraph_thread_id"):
            return {}
        return super().execute(state)

    def get_subgraph(self):
        """Return the (cached) inner workflow compiled WITH an explicit checkpointer.

        The wheel's BaseGraph.compile()/_ensure_compiled() do NOT auto-attach a
        checkpointer from config, so HITL requires this explicit attach.

        The instance is CACHED: GraphNode.execute() calls get_subgraph() on both
        the initial invoke and the resume, and subgraph.resume() must reach the
        SAME checkpointer that stored the suspended checkpoint. A fresh graph+saver
        per call would lose the checkpoint and loop on awaiting_human forever.
        """
        if self._subgraph is not None:
            return self._subgraph
        inner = TourPlanningWorkflow(config=self._inner_config)
        checkpointer_factory = self._inner_config.get("checkpointer_factory")
        hitl_enabled = self._inner_config.get("hitl", {}).get("enabled", False) \
            or self._inner_config.get("memory_enabled", False)
        if checkpointer_factory is None and hitl_enabled:
            # L-01 fallback: HITL requires a checkpointer. If the factory was not
            # wired (config oversight), default to InMemorySaver so inner interrupt()
            # does not RuntimeError. Production should inject a durable saver.
            from langgraph.checkpoint.memory import InMemorySaver
            checkpointer_factory = InMemorySaver
        if checkpointer_factory is not None:
            inner.compile(checkpointer=checkpointer_factory())
        else:
            inner.compile()
        self._subgraph = inner
        return inner

    def extract_input(self, state: AgentState) -> str:
        """Serialize outer context into the inner graph's string input.

        GraphNode.execute passes only user_input + ctx to the subgraph; the
        structured planning_request would otherwise be lost.
        """
        payload = {
            "planning_request": state.get("planning_request", {}),
            "validated_input": state.get("validated_input", ""),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map inner sub_result back to outer state — changed keys only.

        HITL fields are included so the outer checkpointer persists them across
        resume (otherwise the draft / human feedback are dropped, breaking the
        D6 'no LLM re-run' guarantee).
        """
        delta: dict[str, Any] = {
            "result": sub_result.get("result") or sub_result.get("output"),
            "status": sub_result.get("status"),
            "plan_draft": sub_result.get("plan_draft"),
            "procurement_refs": sub_result.get("procurement_refs", []),
            "supplier_match_refs": sub_result.get("supplier_match_refs", []),
            "kb_signals": sub_result.get("kb_signals", {}),
        }
        for k in ("hitl_draft", "hitl_feedback", "hitl_status"):
            if sub_result.get(k) is not None:
                delta[k] = sub_result[k]
        return {k: v for k, v in delta.items() if v is not None}
