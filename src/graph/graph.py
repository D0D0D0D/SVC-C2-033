"""AgentCore Platform v1.0 — outer graph (SVC-C2-033).

Cat 2: outer AgentBaseGraph with the domain workflow encapsulated in a GraphNode
(`main` slot). The fixed 5-node backbone is provided by AgentBaseGraph; this class
only fills pre_process / main / post_process and does NOT override add_edges().

HITL (design H-01): for interrupt/resume the graph must be compiled with an
explicit checkpointer (server.py calls `agent.compile(checkpointer=...)`), and
config must carry memory_enabled / hitl so invoke() attaches a thread_config.

Services (DB / LLM / market source / tenant) are read from config and injected
into the inner workflow and the post_process S-3 resolver.
"""

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph
try:
    from framework.errors import ConfigError
except ImportError:  # compatibility with older AgentCore wheels
    ConfigError = ValueError  # type: ignore[misc,assignment]

from src.nodes.main_node import TourPlanningGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService
from src.services.service import PlanDraftService


class Graph(AgentBaseGraph):
    """Outer tour-planning + procurement-routing agent graph."""

    @property
    def name(self) -> str:
        return "TourPlanningProcurementRoutingAgent"

    @property
    def state_schema(self) -> type:
        return State

    # ── service construction (config-driven, injectable for tests) ───────────

    def _build_services(self) -> dict[str, Any]:
        """Instantiate domain services from config. Test config may inject
        pre-built fakes under the same keys, which take precedence."""
        cfg = self.config
        tenant_id = cfg.get("tenant_id", "default")
        db = cfg.get("db")            # shared.services.database.BaseDatabase instance
        llm = cfg.get("llm")          # shared.services.llm.BaseLLM instance
        kb_service = cfg.get("kb_service")
        if kb_service is None:
            if db is None:
                raise ConfigError(
                    "KB service requires a database backend (TOUR_KB_DB_PATH)"
                )
            kb_service = KBService(db, tenant_id)
        supplier_service = cfg.get("supplier_service")
        if supplier_service is None:
            if db is None:
                raise ConfigError(
                    "Supplier service requires a database backend (TOUR_KB_DB_PATH)"
                )
            supplier_service = SupplierService(db, tenant_id)
        plan_service = cfg.get("plan_service")
        if plan_service is None:
            if llm is None:
                raise ConfigError(
                    "Plan service requires an LLM backend (AZURE_OPENAI_* secrets)"
                )
            plan_service = PlanDraftService(llm)
        return {
            "kb_service": kb_service,
            "supplier_service": supplier_service,
            "market_service": cfg.get("market_service")
            or MarketDataService(cfg.get("market_source_url", "")),
            "plan_service": plan_service,
        }

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize slots

        services = self._build_services()
        inner_config = {
            **services,
            # HITL: inner graph must know it is HITL-enabled and get a checkpointer.
            "memory_enabled": self.config.get("memory_enabled", True),
            "hitl": self.config.get("hitl", {"enabled": True, "max_hitl": 8}),
            "checkpointer_factory": self.config.get("checkpointer_factory"),
        }

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = TourPlanningGraphNode(inner_config=inner_config)
        # post_process keeps refs only (no sensitive values in checkpointed output);
        # sensitive resolution happens in the API adapter after invoke().
        self._nodes["post_process"] = PostProcessNode()
