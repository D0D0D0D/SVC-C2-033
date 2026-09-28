"""AgentCore Platform v1.0 — inner domain-workflow nodes (SVC-C2-033).

These FunctionNode subclasses form the inner TourPlanningWorkflow topology:

    internal_kb_load -> market_data_fetch -> plan_draft
      -> hitl_review (interrupt) -> supplier_match -> procurement_route
      -> document_generate

Sensitive-info invariant: nodes place only non-sensitive signals / reference IDs
into State. Sensitive supplier contact / terms are resolved later, inside the
outer post_process S-3 gate — never here (State is checkpointed).

Services (KB / Supplier / MarketData / PlanDraft LLM) are injected via
constructor seams so the graph wiring and tests control them explicitly.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from langgraph.types import interrupt

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


def _parse_planning_request(state: AgentState) -> dict:
    """Recover planning_request from state, or from the serialized user_input
    that GraphNode.extract_input() passed into the inner graph.invoke()."""
    request = state.get("planning_request")
    if isinstance(request, dict) and request:
        return request
    raw = state.get("user_input", "") or ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed.get("planning_request", parsed)
    except (json.JSONDecodeError, TypeError):
        pass
    return {"theme": raw}


class InternalKBLoadNode(FunctionNode):
    """Load NON-sensitive win-pattern signals from the internal KB."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, kb_service: Any) -> None:
        super().__init__()
        self._kb = kb_service

    def execute(self, state: AgentState) -> dict:
        request = _parse_planning_request(state)
        signals = self._kb.win_pattern_signals(request)
        emit_trace_event("kb_load", {"sample_count": signals.get("sample_count", 0)}, state)
        return {"planning_request": request, "kb_signals": signals}


class MarketDataFetchNode(FunctionNode):
    """Fetch public tourism data; degrade gracefully to {} on failure."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, market_service: Any) -> None:
        super().__init__()
        self._market = market_service

    def execute(self, state: AgentState) -> dict:
        request = state.get("planning_request", {})
        data, degraded = self._market.fetch(request)
        if degraded:
            emit_trace_event("market_fetch_degraded", {"reason": "fetch_failed"}, state)
        else:
            emit_trace_event("market_fetch", {"keys": sorted(data.keys())}, state)
        return {"market_data": data}


class PlanDraftNode(FunctionNode):
    """LLM drafts the tour plan grounded on KB signals + market data."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, plan_service: Any) -> None:
        super().__init__()
        self._planner = plan_service

    def execute(self, state: AgentState) -> dict:
        draft = self._planner.draft(
            state.get("planning_request", {}),
            state.get("kb_signals", {}),
            state.get("market_data", {}),
        )
        emit_trace_event("llm_plan_draft", {"has_concept": bool(draft.get("concept"))}, state)
        # hitl_draft = idempotency guard so resume does not re-run the LLM.
        return {"plan_draft": draft, "hitl_draft": draft}


class HitlReviewNode(FunctionNode):
    """HITL: present the plan draft; apply human approval / revision (D6).

    hitl_count contract (verified against agenticstar-agentcore==1.0.0):
      - `framework/schemas/agent_state.py` states hitl_count is "written only by
        backbone nodes — template authors must not write directly". So this node
        MUST NOT increment hitl_count (doing so would violate the contract and
        risk double-counting).
      - The `hitl_count >= max_hitl` circuit breaker lives ONLY in the Cat 3
        `ToolActNode` autonomous loop. This is a Cat 2 GraphNode + interrupt()
        (D6) path, which has no ToolActNode, so hitl_count is not the loop guard
        here. For Cat 2, `hitl.max_hitl` is validated at graph-config time
        (AgentBaseGraph) and the review is a single interrupt/resume per invoke;
        repeated revisions are new invoke/resume cycles bounded by the caller /
        AgentGateway, not by an in-node counter.
    """

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict:
        draft = state.get("hitl_draft") or state.get("plan_draft") or {}
        # S-4: domain event emitted immediately
        # before interrupt(). Fires on both the initial pass and the resume pass
        # (execute re-runs from the top on resume) — payload is a non-PII boolean
        # signal only, never draft content or sensitive values.
        emit_trace_event(
            "hitl_interrupt", {"plan_draft_present": bool(state.get("plan_draft"))}, state
        )
        # MANDATORY hitl_allowed guard: inside an
        # automated pipeline (parent GraphNode sets hitl_allowed=False) an
        # unconditional interrupt() would deadlock. Approve the draft as-is and
        # proceed without human review.
        if not state.get("hitl_allowed", True):
            emit_trace_event(
                "hitl_skipped_not_allowed", {"reason": "hitl_allowed=false"}, state
            )
            return {
                "plan_draft": draft,
                "hitl_draft": draft,
                "hitl_status": "approved",
            }
        # Suspends the graph; framework re-raises GraphInterrupt through __call__.
        feedback = interrupt(
            {
                "type": "plan_review",
                "draft": draft,
                "reason": "企画ドラフトの承認または修正指示をお願いします",
            }
        )
        approved = isinstance(feedback, dict) and feedback.get("approved") is True
        if approved:
            revised = draft
            hitl_status = "approved"
        else:
            revised = self._apply_feedback(draft, feedback)
            hitl_status = "corrected"
        return {
            "plan_draft": revised,
            "hitl_draft": draft,
            "hitl_feedback": feedback,
            "hitl_status": hitl_status,
        }

    @staticmethod
    def _apply_feedback(draft: dict, feedback: Any) -> dict:
        """Merge human revision into the draft (dict overlay or note append)."""
        revised = dict(draft)
        if isinstance(feedback, dict):
            for k in ("concept", "itinerary", "differentiation", "target"):
                if feedback.get(k) is not None:
                    revised[k] = feedback[k]
            if feedback.get("note"):
                revised["revision_note"] = feedback["note"]
        elif isinstance(feedback, str) and feedback:
            revised["revision_note"] = feedback
        return revised


class SupplierMatchNode(FunctionNode):
    """Enumerate required materials; match to suppliers (refs only, no sensitive)."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, supplier_service: Any) -> None:
        super().__init__()
        self._suppliers = supplier_service

    def execute(self, state: AgentState) -> dict:
        materials = self._required_materials(state.get("plan_draft", {}))
        refs = self._suppliers.match_refs(materials)
        emit_trace_event(
            "supplier_match",
            {"materials": len(materials), "matched": sum(1 for r in refs if r.get("matched"))},
            state,
        )
        return {"required_materials": materials, "supplier_match_refs": refs}

    @staticmethod
    def _required_materials(plan_draft: dict) -> list:
        """Derive required material categories from the itinerary (non-sensitive)."""
        materials: list[str] = []
        for leg in plan_draft.get("itinerary", []) or []:
            if isinstance(leg, dict):
                for cat in leg.get("categories", []) or []:
                    if cat not in materials:
                        materials.append(cat)
        # Baseline categories always needed for a tour product.
        for cat in ("宿泊", "交通"):
            if cat not in materials:
                materials.append(cat)
        return materials


class ProcurementRouteNode(FunctionNode):
    """Build the procurement action list as reference IDs only (no sensitive)."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict:
        refs = state.get("supplier_match_refs", [])
        actions = []
        for idx, ref in enumerate(refs):
            if not ref.get("matched"):
                continue
            if not ref.get("item"):  # NIT: skip actions with no item name
                continue
            actions.append(
                {
                    "action_id": f"A{idx + 1:03d}",
                    "item": ref.get("item"),
                    "supplier_id": ref.get("supplier_id"),
                    "action": "provisional_block",  # 仮押さえ
                }
            )
        emit_trace_event("procurement_route", {"action_count": len(actions)}, state)
        return {"procurement_refs": actions}


class DocumentGenerateNode(FunctionNode):
    """Assemble the deliverable skeleton (refs + placeholders — no sensitive values)."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict:
        result = {
            "plan_draft": state.get("plan_draft", {}),
            "procurement_refs": state.get("procurement_refs", []),
            "supplier_match_refs": state.get("supplier_match_refs", []),
            # Placeholder marks: sensitive values resolved only in post_process S-3 gate.
            "sensitive_resolution": "deferred_to_s3_gate",
        }
        emit_trace_event("document_generate", {"has_plan": bool(result["plan_draft"])}, state)
        return {"result": result, "status": AgentStatus.SUCCESS.value}
