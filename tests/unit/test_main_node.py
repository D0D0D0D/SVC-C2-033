# SVC-C2-033 — Unit Tests: main GraphNode + node contracts

import inspect

from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import TourPlanningGraphNode


class TestMainNodeContract:
    """Node contract checks for the outer main GraphNode."""

    def test_execute_method_signature(self):
        """node contract: node implements execute(self, state), not _invoke_impl."""
        assert hasattr(TourPlanningGraphNode, "execute")
        sig = inspect.signature(TourPlanningGraphNode.execute)
        params = list(sig.parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in TourPlanningGraphNode.__dict__

    def test_hitl_and_error_strategy_flags(self):
        """GraphNode must propagate HITL and fail-fast on inner errors."""
        assert TourPlanningGraphNode.propagate_hitl is True
        assert TourPlanningGraphNode.error_strategy == "propagate"

    def test_upstream_error_passthrough(self):
        """pre->main is unconditional; an upstream error must pass straight through."""
        node = TourPlanningGraphNode(inner_config={})
        out = node.execute({"status": AgentStatus.ERROR.value})
        assert out == {}

    def test_extract_input_serializes_planning_request(self):
        """Structured planning context must be serialized into the inner string input."""
        node = TourPlanningGraphNode(inner_config={})
        s = node.extract_input({"planning_request": {"area": "箱根"}, "validated_input": "x"})
        assert "箱根" in s
        assert isinstance(s, str)

    def test_get_subgraph_fallback_checkpointer_when_hitl(self, fake_db, fake_llm, fake_market_fetcher):
        """L-01: HITL enabled but checkpointer_factory missing → fallback saver attached."""
        from src.services.kb_service import KBService, SupplierService
        from src.services.market_data_service import MarketDataService
        from src.services.service import PlanDraftService
        inner_config = {
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 8},
            # checkpointer_factory intentionally omitted
            "kb_service": KBService(fake_db, "default"),
            "supplier_service": SupplierService(fake_db, "default"),
            "market_service": MarketDataService("http://x", fetcher=fake_market_fetcher),
            "plan_service": PlanDraftService(fake_llm),
        }
        node = TourPlanningGraphNode(inner_config=inner_config)
        inner = node.get_subgraph()
        # compiled with a checkpointer (no RuntimeError on interrupt path)
        assert inner._compiled is not None

    def test_merge_output_includes_hitl_fields(self):
        """merge_output must carry HITL fields so resume keeps draft/feedback."""
        node = TourPlanningGraphNode(inner_config={})
        sub = {
            "result": {"plan_draft": {}},
            "status": AgentStatus.SUCCESS.value,
            "hitl_draft": {"concept": "c"},
            "hitl_feedback": {"approved": True},
            "hitl_status": "approved",
        }
        delta = node.merge_output({}, sub)
        assert delta["hitl_draft"] == {"concept": "c"}
        assert delta["hitl_feedback"] == {"approved": True}
        assert delta["hitl_status"] == "approved"
        assert delta["status"] == AgentStatus.SUCCESS.value
