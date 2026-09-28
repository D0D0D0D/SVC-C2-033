"""AgentCore Platform v1.0 — adapter-side sensitive-value resolver (SVC-C2-033).

Resolves sensitive supplier values (contact / standard_terms) into the final
deliverable AFTER invoke() returns — i.e. outside the checkpointed graph — so
these values never persist in the checkpoint DB (sensitive-info invariant).

Called by the API adapter (src/api/server.py) on the invoke() response.
"""

from __future__ import annotations

from typing import Any


def resolve_sensitive_output(response: dict, supplier_resolver: Any) -> dict:
    """Return a copy of `response` with sensitive supplier values resolved into
    the procurement actions. Does not mutate the input / any persisted state.

    `supplier_resolver.resolve_sensitive(supplier_id) -> dict` supplies the
    sensitive fields. If there is no output payload (e.g. HITL suspend), the
    response is returned unchanged.
    """
    if not isinstance(response, dict):
        return response
    output = response.get("output")
    if not isinstance(output, dict):
        return response

    actions = output.get("procurement_actions")
    if not isinstance(actions, list):
        return response

    resolved_actions = []
    for action in actions:
        enriched = dict(action) if isinstance(action, dict) else {"raw": action}
        sid = enriched.get("supplier_id")
        if sid and supplier_resolver is not None:
            sensitive = supplier_resolver.resolve_sensitive(sid) or {}
            enriched["supplier_name"] = sensitive.get("supplier_name")
            enriched["contact"] = sensitive.get("contact")
            enriched["terms"] = sensitive.get("standard_terms")
            enriched["lead_time_days"] = sensitive.get("lead_time_days")
        resolved_actions.append(enriched)

    new_output = dict(output)
    new_output["procurement_actions"] = resolved_actions
    new_output["sensitive_resolution"] = "resolved"
    new_response = dict(response)
    new_response["output"] = new_output
    return new_response
