"""AgentCore Platform v1.0 — pre_process node (SVC-C2-033).

Validates / normalizes the planning request and applies the S-2 domain input
gate (_extra_security_gate_input). The framework default PII scan runs first;
this hook adds consent validation, input-size limits, and PII rejection on the
free-text fields.
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security import pii_detector
from shared.utils.audit_logger import emit_trace_event

_MAX_INPUT_CHARS = 4000
_REQUEST_KEYS = ("segment", "season", "theme", "area", "budget_band")


class PreProcessNode(FunctionNode):
    """Normalize input into planning_request; enforce S-2 domain checks."""

    # S-1: declared explicitly by design, not inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict:
        raw = state.get("user_input", "") or ""

        if not raw.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        request = self._parse_request(raw)
        emit_trace_event(
            "planning_request_normalized",
            {"keys_present": [k for k in _REQUEST_KEYS if request.get(k)]},
            state,
        )
        return {
            "validated_input": raw.strip(),
            "planning_request": request,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _parse_request(raw: str) -> dict:
        """Accept JSON or free text; return a structured planning_request."""
        request: dict = {k: "" for k in _REQUEST_KEYS}
        request["consent"] = False
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k in _REQUEST_KEYS:
                    if parsed.get(k) is not None:
                        request[k] = parsed[k]
                request["consent"] = bool(parsed.get("consent", False))
                return request
        except (json.JSONDecodeError, TypeError):
            pass
        # Free text: keep raw as the theme signal; planner tolerates sparse fields.
        request["theme"] = raw.strip()
        return request

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """S-2 domain input gate. Runs BEFORE execute() (per BaseNode.__call__),
        so it parses the raw input itself rather than relying on planning_request.
        Must NOT raise — reject via state.
        """
        raw = state.get("user_input", "") or ""

        # Input-size limit
        if len(raw) > _MAX_INPUT_CHARS:
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append(
                f"S-2: input exceeds {_MAX_INPUT_CHARS} chars"
            )
            return state

        # Consent flag validation for structured (KB-linkage) requests.
        request = self._parse_request(raw)
        if request.get("consent") is False:
            # Structured = a non-theme KB-linkage field is present, OR (L-02 guard)
            # the raw payload mentions KB-linkage keys even if the JSON is malformed
            # (prevents skipping the consent gate by deliberately breaking JSON).
            structured = any(request.get(k) for k in _REQUEST_KEYS if k != "theme")
            if not structured:
                lowered = raw.lower()
                structured = any(
                    f'"{k}"' in lowered or f"{k}:" in lowered
                    for k in ("area", "season", "segment", "budget_band")
                )
            if structured:
                state["status"] = AgentStatus.ERROR.value
                state.setdefault("error_log", []).append(
                    "S-2: consent flag required for internal-KB linkage"
                )
                return state

        # PII rejection on the free-text input
        if pii_detector.detect_pii(raw):
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append(
                "S-2: PII detected in planning input"
            )
            return state

        return state
