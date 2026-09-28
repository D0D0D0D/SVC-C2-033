# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# PB-7: HITL Interrupt Propagation — exact-name file required by the
# scaffold-integrity check for HITL-enabled templates.
#
# This template enables HITL (the inner hitl_review node calls interrupt()), so
# the assertions below run for real. The full substantive coverage — suspend →
# awaiting_human, resume → success, and no sensitive values in checkpoints —
# lives in test_hitl_interrupt.py; this exact-name file re-exercises the core
# suspend + resume boundary and adds the hitl_allowed=False guard case.

from __future__ import annotations

import pathlib
import warnings

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import Graph
from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService
from src.services.service import PlanDraftService

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)

_REQUEST = '{"area": "箱根", "theme": "温泉", "season": "2026-autumn", "consent": true}'


def _agent(fake_db, fake_llm, fake_market_fetcher):
    cfg = {
        "tenant_id": "default",
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 8},
        "checkpointer_factory": InMemorySaver,
        "kb_service": KBService(fake_db, "default"),
        "supplier_service": SupplierService(fake_db, "default"),
        "market_service": MarketDataService("http://x", fetcher=fake_market_fetcher),
        "plan_service": PlanDraftService(fake_llm),
    }
    agent = Graph(config=cfg)
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _ctx(session):
    return InvocationContext(session_id=session, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def test_pb7_interrupt_surfaces_as_awaiting_human_not_error(fake_db, fake_llm, fake_market_fetcher):
    agent = _agent(fake_db, fake_llm, fake_market_fetcher)
    out = agent.invoke(_REQUEST, ctx=_ctx("pb7-suspend"))
    assert out.get("status") == AgentStatus.AWAITING_HUMAN.value
    assert not out.get("error_log")


def test_pb7_resume_reaches_success(fake_db, fake_llm, fake_market_fetcher):
    """resume() must reach SUCCESS — guards the get_subgraph caching bug where a
    fresh inner graph+saver per call loses the suspend checkpoint."""
    agent = _agent(fake_db, fake_llm, fake_market_fetcher)
    ctx = _ctx("pb7-resume")
    out = agent.invoke(_REQUEST, ctx=ctx)
    assert out.get("status") == AgentStatus.AWAITING_HUMAN.value
    resumed = agent.resume(thread_id=ctx.thread_id, feedback={"approved": True})
    assert resumed.get("status") == AgentStatus.SUCCESS.value, (
        f"HITL resume did not complete: {resumed.get('status')}"
    )


def test_pb7_hitl_allowed_false_skips_interrupt(fake_db, fake_llm, fake_market_fetcher):
    """hitl_allowed=False must skip interrupt() and approve the draft — no deadlock."""
    from src.nodes.inner_nodes import HitlReviewNode

    node = HitlReviewNode()
    out = node.execute({"plan_draft": {"concept": "x"}, "hitl_allowed": False})
    assert out.get("hitl_status") == "approved"
    assert "plan_draft" in out
