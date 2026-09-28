# PB-7: HITL interrupt propagation
# interrupt() inside hitl_review must suspend the graph (GraphInterrupt bubbles
# through BaseNode.__call__ and is NOT caught by the error boundary). Status must
# be AWAITING_HUMAN, never error.
#
# PB-5 reinforcement: the checkpointed State must not contain sensitive supplier
# values (contact / standard_terms) at the suspend point.

from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import Graph
from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService
from src.services.service import PlanDraftService


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


class TestHitlInterruptPropagation:
    def test_interrupt_suspends_not_errors(self, fake_db, fake_llm, fake_market_fetcher):
        """PB-7: HITL interrupt yields AWAITING_HUMAN, not error."""
        agent = _agent(fake_db, fake_llm, fake_market_fetcher)
        out = agent.invoke(
            '{"area": "箱根", "theme": "温泉", "season": "2026-autumn", "consent": true}',
            ctx=InvocationContext(session_id="pb7", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert out.get("status") == AgentStatus.AWAITING_HUMAN.value
        assert out.get("status") != AgentStatus.ERROR.value

    def test_no_sensitive_values_in_output_before_gate(self, fake_db, fake_llm, fake_market_fetcher):
        """PB-5: at suspend, no sensitive supplier contact/terms surfaced in output."""
        agent = _agent(fake_db, fake_llm, fake_market_fetcher)
        out = agent.invoke(
            '{"area": "箱根", "theme": "温泉", "season": "2026-autumn", "consent": true}',
            ctx=InvocationContext(session_id="pb5", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        blob = str(out)
        assert "yado@example.jp" not in blob
        assert "前払30%" not in blob

    def test_no_sensitive_values_in_checkpoint_after_full_run(
        self, fake_db, fake_llm, fake_market_fetcher
    ):
        """PB-5 (strong): after invoke → resume(approve) → completion, the PERSISTED
        checkpoints must contain no sensitive supplier contact/terms. This is the
        invariant that sensitive resolution must never happen inside the graph.
        """
        saver = InMemorySaver()
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
        agent.compile(checkpointer=saver)

        ctx = InvocationContext(session_id="pbfull", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        out = agent.invoke(
            '{"area": "箱根", "theme": "温泉", "season": "2026-autumn", "consent": true}',
            ctx=ctx,
        )
        assert out.get("status") == AgentStatus.AWAITING_HUMAN.value

        # Resume through the HITL approval so post_process / document_generate run
        # to completion — the phase where a regression could reintroduce a leak.
        resumed = agent.resume(thread_id=ctx.thread_id, feedback={"approved": True})

        # (a) resume must actually COMPLETE (guards the get_subgraph caching bug:
        #     a fresh inner saver per call loses the checkpoint → loops on
        #     awaiting_human and never finishes).
        assert resumed.get("status") == AgentStatus.SUCCESS.value, (
            f"HITL resume did not complete: {resumed.get('status')}"
        )

        # (b) completion output (returned to caller, pre-adapter) carries no sensitive value
        assert "yado@example.jp" not in str(resumed)
        assert "前払30%" not in str(resumed)

        # (b) every persisted checkpoint payload — across suspend AND completion — is clean
        dumped = str(list(saver.list(None)))
        assert "yado@example.jp" not in dumped, "sensitive contact leaked into checkpoint"
        assert "前払30%" not in dumped, "sensitive terms leaked into checkpoint"
