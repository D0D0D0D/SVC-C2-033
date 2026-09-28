"""AgentCore Platform v1.0 — SVC-C2-033 State schema.

ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
object kinds). LangGraph checkpoints use msgpack serialization; non-primitive model objects
cause silent corruption. Extend AgentState with agent-specific fields only.
Do NOT add credentials, secrets, or such model objects.

Sensitive-info invariant (S-3): supplier contact / standard_terms and any
procurement sensitive value MUST NOT be stored in State (State is checkpointed
to the DB in plaintext). Only reference IDs / placeholders live in State;
sensitive values are resolved inside the post_process S-3 gate and only appear
in the gated final `output`.
"""

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Tour-planning + procurement-routing agent state (flat TypedDict).

    Shared fields (user_input, status, session_id, node_history, error_log,
    hitl_feedback, hitl_status, hitl_draft, hitl_count, hitl_allowed,
    subgraph_thread_id, etc.) are inherited from AgentState.
    """

    # ── input / normalization ────────────────────────────────────────────
    validated_input: str
    # planning_request keys: segment, season, theme, area, budget_band, consent
    planning_request: dict

    # ── inner workflow signals (non-sensitive only) ──────────────────────
    kb_signals: dict          # win-pattern features / performance aggregates
    market_data: dict         # Tokyo tourism open-data (public); {} on degrade
    plan_draft: dict          # concept / itinerary / differentiation / target
    required_materials: list  # item names only (non-sensitive)
    supplier_match_refs: list # [{supplier_id, matched: bool}] — no contact/terms
    procurement_refs: list    # [{action_id, supplier_id, item}] — refs only

    # ── output (S-3 gated) ───────────────────────────────────────────────
    result: dict              # skeleton: refs + placeholders (no sensitive values)
    formatted_output: dict    # post_process S-3-gated output (projected to `output`)
