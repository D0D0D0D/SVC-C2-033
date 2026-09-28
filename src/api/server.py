"""AgentCore Platform v1.0 — HTTP entry point (SVC-C2-033).

Adapter only — no business logic. AgentGateway may call agent.invoke() directly.

HITL (design H-01): the wheel does NOT auto-attach a checkpointer from config, so
this adapter compiles the graph with an EXPLICIT checkpointer. Without it,
interrupt() in the plan-review step raises "interrupt() called but no checkpointer".
The inner workflow receives its own checkpointer via checkpointer_factory in config.
"""

from __future__ import annotations

import os
import threading
import weakref
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import Graph
from src.services.azure_llm import AzureMarketplaceLLM
from src.services.kb_backend import LazyKBDatabase, validate_sample_sources
from src.services.kb_service import SupplierService
from src.services.output_resolver import resolve_sensitive_output

app = FastAPI(title="TourPlanningProcurementRoutingAgent")


def _load_config() -> dict:
    """Load runtime config + wire the inner-workflow checkpointer factory."""
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "config.yaml")
    with open(os.path.abspath(cfg_path)) as f:
        runtime_config = yaml.safe_load(f) or {}
    if not isinstance(runtime_config, dict):
        raise ValueError("config/config.yaml must contain a mapping")
    cfg = dict(runtime_config)
    # Inner workflow checkpointer factory (each subgraph instance gets its own saver).
    # Swap InMemorySaver for a durable saver in production.
    cfg["checkpointer_factory"] = InMemorySaver
    sample_data = cfg.get("sample_data") or {}
    validate_sample_sources(
        sample_data.get("past_tours_path"),
        sample_data.get("suppliers_path"),
    )
    if cfg.get("db") is None:
        cfg["db"] = LazyKBDatabase(
            tenant_id=cfg.get("tenant_id", "default"),
            past_tours_path=sample_data.get("past_tours_path"),
            suppliers_path=sample_data.get("suppliers_path"),
        )
    if cfg.get("llm") is None:
        cfg["llm"] = AzureMarketplaceLLM()
    return cfg


agent = Graph(config=_load_config())
agent.compile(checkpointer=InMemorySaver())  # ★ explicit checkpointer for outer HITL
agent.provision_secrets(
    secrets_factory(namespace="svc", agent_name="svc_c2_033")
)


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


class ResumeRequest(BaseModel):
    thread_id: str
    feedback: Any
    session_id: str = ""


def _supplier_resolver(cfg: dict):
    """Build the supplier resolver for adapter-side sensitive resolution."""
    if cfg.get("supplier_service") is not None:
        return cfg["supplier_service"]
    return SupplierService(cfg.get("db"), cfg.get("tenant_id", "default"))


def _authenticated_ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication.

    Entry authentication enforces the following trust contract:
    verified middleware trust is used as-is; INVOKE_AUTH_TOKEN Bearer auth
    promotes ANONYMOUS → VERIFIED_EXTERNAL (401 on mismatch). An unauthenticated
    caller (still ANONYMOUS, no token promotion) → 401 — replacing the old
    fallback that let ANONYMOUS callers reach the graph. This template has no
    tenant/org request-scope model (tenant is a deployment config value), so the
    entry-adapter scope layer is intentionally not wired (only the auth core).
    """
    trust, token_authenticated = _resolve_trust(request)
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


def _require_resume_ownership(thread_id: str, ctx: InvocationContext) -> None:
    """Fail-closed checkpoint ownership check for HITL resume.

    Any authenticated caller could replay another session's thread_id; verify
    the suspended checkpoint exists (404 if not) and its recorded session_id
    matches (403 on mismatch, and a checkpoint with no session_id is
    un-resumable). session_id is the primary owner key (token callers share
    caller_id=""); caller_id is defense-in-depth when the checkpoint recorded one.
    """
    compiled = getattr(agent, "_compiled", None)
    if compiled is None:
        raise HTTPException(status_code=409, detail="No suspended session for this thread.")
    snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
    values = getattr(snapshot, "values", None) or {}
    if not values:
        raise HTTPException(status_code=404, detail="No suspended session for this thread.")
    owner_session = values.get("session_id")
    if not owner_session or owner_session != ctx.session_id:
        raise HTTPException(status_code=403, detail="Session does not own this thread.")
    owner_caller = values.get("caller_id") or ""
    if owner_caller and owner_caller != (ctx.caller_id or ""):
        raise HTTPException(status_code=403, detail="Caller does not own this thread.")
    # Require this template's PENDING HITL wait: a completed thread
    # has an empty snapshot.next, and the outer graph's ONLY externally-returned
    # non-terminal checkpoint is the propagated inner HITL pause — which sets
    # subgraph_thread_id and leaves next == ("main",). Gating on BOTH predicates
    # (scheduled work remains AND a propagated HITL wait is pending) rejects a
    # resume replayed after completion, and also distinguishes this HITL wait from
    # any future static breakpoint / other pending workflow state that would leave
    # next non-empty without an interrupt to resume.
    #
    # The check + resume are serialized per thread by a keyed lock in /resume;
    # a durable multi-process deployment must add a per-thread lease/CAS in the
    # shared checkpoint store for full mutual exclusion.
    if not getattr(snapshot, "next", None) or not values.get("subgraph_thread_id"):
        raise HTTPException(status_code=409, detail="Thread is not awaiting human input.")


# Per-thread locks serialize the ownership-check + resume for one thread within
# this process (guarding the resume TOCTOU). A durable, multi-process deployment
# must use a per-thread lease/CAS instead.
_RESUME_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_RESUME_LOCKS_GUARD = threading.Lock()


def _resume_lock(thread_id: str) -> threading.Lock:
    with _RESUME_LOCKS_GUARD:
        lock = _RESUME_LOCKS.get(thread_id)
        if lock is None:
            lock = threading.Lock()
            _RESUME_LOCKS[thread_id] = lock
        return lock


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _authenticated_ctx(request, req.session_id)
    with bound_secrets(agent._secrets_provider):
        response = agent.invoke(req.input, ctx=ctx)
        # Resolve sensitive supplier values into the RESPONSE only (never persisted).
        return resolve_sensitive_output(response, _supplier_resolver(agent.config))


@app.post("/resume")
async def resume(req: ResumeRequest, request: Request):
    ctx = _authenticated_ctx(request, req.session_id)
    # Serialize check + resume per thread (guards the resume TOCTOU).
    with _resume_lock(req.thread_id):
        _require_resume_ownership(req.thread_id, ctx)
        with bound_secrets(agent._secrets_provider):
            response = agent.resume(thread_id=req.thread_id, feedback=req.feedback)
            # Same sensitive-value resolution as /invoke — resume also returns a plan.
            return resolve_sensitive_output(response, _supplier_resolver(agent.config))


@app.get("/health")
def health():
    return {"status": "ok", "agent": "TourPlanningProcurementRoutingAgent"}
