"""AgentCore Platform v1.0 — post_process node (SVC-C2-033).

Builds the formatted deliverable from the ref-only `result` skeleton and applies
the S-3 domain output gate (_extra_security_gate_output).

Sensitive-info invariant (S-3): this node runs INSIDE the graph, so anything it
returns is checkpointed in plaintext. Therefore it MUST NOT resolve sensitive
supplier values (contact / standard_terms) into its output — it keeps reference
IDs only. The S-3 gate here performs the SAFETY function (PII / credential
re-scan on the LLM-authored narrative) and validates that the skeleton carries
no sensitive values.

The actual resolution of sensitive supplier values into the final deliverable
happens in the API adapter (src/api/server.py) AFTER invoke() returns — i.e.
outside the checkpointed graph — so contacts/terms never persist in the
checkpoint DB. See docs/02_design.md §S-3.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security import pii_detector
from shared.utils.audit_logger import emit_trace_event

# Sensitive keys that must never appear in checkpointed output.
_SENSITIVE_KEYS = ("contact", "standard_terms", "terms")


class PostProcessNode(FunctionNode):
    """Format tour plan + procurement list (refs only); S-3 safety gate."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict:
        # Deliverable skeleton — reference IDs + placeholders only, NO sensitive values.
        formatted = {
            "plan_draft": state.get("plan_draft", {}),
            "procurement_actions": state.get("procurement_refs", []),
            "supplier_matches": state.get("supplier_match_refs", []),
            "kb_signals": state.get("kb_signals", {}),
            # Marker: sensitive supplier values are resolved by the API adapter
            # after invoke() returns (outside the checkpointed graph).
            "sensitive_resolution": "resolve_in_adapter",
        }
        emit_trace_event(
            "output_formatted",
            {"action_count": len(formatted["procurement_actions"])},
            state,
        )
        return {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain output gate (SAFETY, not resolution).

        (1) Assert the output carries no resolved sensitive values (defense in
            depth for the checkpoint invariant) — raise if any leak is detected.
        (2) Re-scan the LLM-authored narrative for PII; raise to block if unsafe.
        """
        formatted = result.get("formatted_output")
        if not isinstance(formatted, dict):
            return result

        # (1) Invariant guard: no sensitive keys in any procurement action.
        for action in formatted.get("procurement_actions", []):
            if isinstance(action, dict):
                for k in _SENSITIVE_KEYS:
                    if k in action:
                        raise ValueError(
                            f"S-3: sensitive key '{k}' must not be in checkpointed output; "
                            "resolve it in the API adapter after invoke()"
                        )

        # (2) PII safety scan on the LLM-authored plan narrative.
        plan = formatted.get("plan_draft", {})
        narrative = " ".join(
            str(plan.get(k, "")) for k in ("concept", "differentiation", "target")
        )
        if pii_detector.detect_pii(narrative):
            raise ValueError("S-3: PII detected in generated plan narrative")

        return result
