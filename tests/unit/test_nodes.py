# SVC-C2-033 — Unit Tests: pre/post nodes, inner nodes, services
import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.inner_nodes import (
    InternalKBLoadNode,
    MarketDataFetchNode,
    PlanDraftNode,
    SupplierMatchNode,
    ProcurementRouteNode,
    DocumentGenerateNode,
)
from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService
from src.services.service import PlanDraftService


# ── pre_process ──────────────────────────────────────────────────────────────

class TestPreProcess:
    def test_json_request_parsed(self):
        node = PreProcessNode()
        out = node.execute({"user_input": '{"area": "箱根", "theme": "温泉", "consent": true}'})
        assert out["planning_request"]["area"] == "箱根"
        assert out["status"] == AgentStatus.SUCCESS.value

    def test_empty_input_errors(self):
        node = PreProcessNode()
        out = node.execute({"user_input": "  "})
        assert out["status"] == AgentStatus.ERROR.value

    def test_s2_gate_rejects_oversize(self):
        node = PreProcessNode()
        state = node._extra_security_gate_input({"user_input": "x" * 5000, "error_log": []})
        assert state["status"] == AgentStatus.ERROR.value

    def test_s2_gate_requires_consent_for_structured(self):
        node = PreProcessNode()
        state = node._extra_security_gate_input(
            {"user_input": '{"area": "箱根", "season": "2026-autumn", "consent": false}',
             "error_log": []}
        )
        assert state["status"] == AgentStatus.ERROR.value
        assert any("consent" in e for e in state["error_log"])

    def test_s2_gate_allows_freetext(self):
        node = PreProcessNode()
        state = node._extra_security_gate_input({"user_input": "温泉ツアーを企画したい", "error_log": []})
        assert state.get("status") != AgentStatus.ERROR.value

    def test_s2_gate_consent_not_bypassed_by_broken_json(self):
        """L-02: deliberately malformed JSON with KB-linkage keys still requires consent."""
        node = PreProcessNode()
        state = node._extra_security_gate_input(
            {"user_input": '{"area": "箱根", "season": "2026-autumn"', "error_log": []}  # missing }
        )
        assert state["status"] == AgentStatus.ERROR.value
        assert any("consent" in e for e in state["error_log"])


# ── inner nodes with services ────────────────────────────────────────────────

class TestInnerNodes:
    def test_kb_load_signals(self, fake_db):
        node = InternalKBLoadNode(KBService(fake_db, "default"))
        out = node.execute({"planning_request": {"theme": "温泉", "area": "箱根"}})
        assert out["kb_signals"]["sample_count"] == 2
        assert "温泉" in out["kb_signals"]["top_themes"]

    def test_market_fetch_success(self, fake_market_fetcher):
        svc = MarketDataService("http://x", fetcher=fake_market_fetcher)
        node = MarketDataFetchNode(svc)
        out = node.execute({"planning_request": {"area": "箱根"}})
        assert "events" in out["market_data"]

    def test_market_fetch_degrades(self):
        def boom(url, params):
            raise RuntimeError("api down")
        svc = MarketDataService("http://x", fetcher=boom)
        node = MarketDataFetchNode(svc)
        out = node.execute({"planning_request": {"area": "箱根"}})
        assert out["market_data"] == {}

    def test_plan_draft(self, fake_llm):
        node = PlanDraftNode(PlanDraftService(fake_llm))
        out = node.execute({"planning_request": {}, "kb_signals": {}, "market_data": {}})
        assert out["plan_draft"]["concept"]
        assert out["hitl_draft"] == out["plan_draft"]

    def test_supplier_match_refs_have_no_sensitive(self, fake_db):
        node = SupplierMatchNode(SupplierService(fake_db, "default"))
        out = node.execute({"plan_draft": {"itinerary": [{"categories": ["宿泊"]}]}})
        for ref in out["supplier_match_refs"]:
            assert "contact" not in ref
            assert "standard_terms" not in ref

    def test_procurement_refs_only_ids(self, fake_db):
        refs = [{"item": "宿泊", "supplier_id": "S001", "matched": True}]
        out = ProcurementRouteNode().execute({"supplier_match_refs": refs})
        assert out["procurement_refs"][0]["supplier_id"] == "S001"
        assert "contact" not in out["procurement_refs"][0]

    def test_procurement_skips_item_none(self):
        """NIT: an action with no item name must not enter the deliverable."""
        refs = [
            {"item": None, "supplier_id": "S001", "matched": True},
            {"item": "宿泊", "supplier_id": "S002", "matched": True},
        ]
        out = ProcurementRouteNode().execute({"supplier_match_refs": refs})
        assert len(out["procurement_refs"]) == 1
        assert out["procurement_refs"][0]["item"] == "宿泊"

    def test_document_generate_defers_sensitive(self):
        out = DocumentGenerateNode().execute(
            {"plan_draft": {"concept": "c"}, "procurement_refs": [], "supplier_match_refs": []}
        )
        assert out["result"]["sensitive_resolution"] == "deferred_to_s3_gate"
        assert out["status"] == AgentStatus.SUCCESS.value


# ── post_process S-3 SAFETY gate (no resolution in graph) ────────────────────

class TestPostProcessS3:
    def test_execute_output_has_no_sensitive(self):
        node = PostProcessNode()
        state = {
            "plan_draft": {"concept": "秋ツアー"},
            "procurement_refs": [{"action_id": "A001", "item": "宿泊", "supplier_id": "S001"}],
            "supplier_match_refs": [],
        }
        result = node.execute(state)
        actions = result["formatted_output"]["procurement_actions"]
        assert "contact" not in actions[0]
        assert "terms" not in actions[0]
        assert result["formatted_output"]["sensitive_resolution"] == "resolve_in_adapter"

    def test_s3_gate_passes_ref_only_output(self):
        """S-3 SAFETY gate accepts a refs-only skeleton (no sensitive keys)."""
        node = PostProcessNode()
        result = node.execute(
            {"plan_draft": {"concept": "c"},
             "procurement_refs": [{"action_id": "A001", "supplier_id": "S001"}],
             "supplier_match_refs": []}
        )
        gated = node._extra_security_gate_output(result)
        assert "contact" not in gated["formatted_output"]["procurement_actions"][0]

    def test_s3_gate_rejects_leaked_sensitive(self):
        """Defense-in-depth: S-3 raises if a sensitive key leaked into the output."""
        node = PostProcessNode()
        bad = {"formatted_output": {
            "plan_draft": {},
            "procurement_actions": [{"action_id": "A001", "contact": "leak@example.jp"}],
        }}
        with pytest.raises(ValueError):
            node._extra_security_gate_output(bad)

    def test_s3_gate_blocks_pii_in_narrative(self):
        """S-3 must block a plan narrative containing PII (JP phone number)."""
        node = PostProcessNode()
        bad = {"formatted_output": {
            "plan_draft": {"concept": "連絡先 090-1234-5678 の担当まで"},
            "procurement_actions": [],
        }}
        with pytest.raises(ValueError):
            node._extra_security_gate_output(bad)


# ── adapter-side sensitive resolution (outside checkpointed graph) ────────────

class TestOutputResolver:
    def test_resolver_injects_sensitive_into_response_only(self, fake_db):
        from src.services.output_resolver import resolve_sensitive_output
        resolver = SupplierService(fake_db, "default")
        response = {
            "status": "success",
            "output": {
                "plan_draft": {"concept": "秋ツアー"},
                "procurement_actions": [{"action_id": "A001", "item": "宿泊", "supplier_id": "S001"}],
            },
        }
        resolved = resolve_sensitive_output(response, resolver)
        act = resolved["output"]["procurement_actions"][0]
        assert act["contact"] == "yado@example.jp"
        assert act["terms"] == "前払30%"
        # original response untouched (no mutation → no accidental persistence)
        assert "contact" not in response["output"]["procurement_actions"][0]

    def test_resolver_noop_on_hitl_suspend(self, fake_db):
        from src.services.output_resolver import resolve_sensitive_output
        resolver = SupplierService(fake_db, "default")
        suspend = {"status": "awaiting_human"}
        assert resolve_sensitive_output(suspend, resolver) == suspend
