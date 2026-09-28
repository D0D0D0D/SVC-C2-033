"""Shared test fixtures for SVC-C2-033.

Provides framework-compatible fakes (BaseDatabase, BaseLLM, market fetcher) so
tests run offline without real DB / LLM / HTTP. The repo root is added to
sys.path; the framework itself is provided by the installed
agenticstar-agentcore wheel (no sys.modules stubbing — wheel-era repo).
"""

from __future__ import annotations

import os
import sys

import pytest
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService


class FakeDB:
    """Minimal BaseDatabase-compatible fake backed by in-memory tables."""

    def __init__(self, past_tours=None, suppliers=None):
        self._past_tours = past_tours or []
        self._suppliers = suppliers or []
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return None

    def fetch_many(self, query, params=None):
        if "past_tours" in query:
            return list(self._past_tours)
        if "suppliers" in query:
            return list(self._suppliers)
        return []

    def fetch_one(self, query, params=None):
        params = params or {}
        if "suppliers" in query and "category" in query:
            for s in self._suppliers:
                if s.get("category") == params.get("category"):
                    return s
            return None
        if "suppliers" in query and "supplier_id" in query:
            for s in self._suppliers:
                if s.get("supplier_id") == params.get("sid"):
                    return s
            return None
        return None


class FakeLLM:
    """BaseLLM-compatible fake: returns a fixed JSON plan draft."""

    def __init__(self, content=None):
        self._content = content or (
            '{"concept": "秋の温泉文化ツアー", "itinerary": '
            '[{"day": 1, "categories": ["宿泊", "体験"]}], '
            '"differentiation": "自社実績上位テーマ準拠", "target": "50代夫婦"}'
        )
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return {"content": self._content, "tool_calls": [], "model": "fake", "usage": {}}


@pytest.fixture
def fake_db():
    return FakeDB(
        past_tours=[
            {"theme": "温泉", "area": "箱根", "price_band": "80k-120k",
             "units_sold": 40, "gross_margin": 0.3, "rating": 4.5},
            {"theme": "温泉", "area": "箱根", "price_band": "80k-120k",
             "units_sold": 22, "gross_margin": 0.25, "rating": 4.2},
        ],
        suppliers=[
            {"supplier_id": "S001", "supplier_name": "箱根旅館組合", "category": "宿泊",
             "area": "箱根", "contact": "yado@example.jp", "standard_terms": "前払30%",
             "lead_time_days": 14},
            {"supplier_id": "S002", "supplier_name": "小田急交通", "category": "交通",
             "area": "箱根", "contact": "bus@example.jp", "standard_terms": "月末締",
             "lead_time_days": 7},
        ],
    )


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def fake_market_fetcher():
    """Returns a fetcher callable that yields fixed market data."""
    def _fetch(url, params):
        return {"events": [{"name": "紅葉まつり", "area": params.get("area", "")}]}
    return _fetch


@pytest.fixture
def agent_config(fake_db, fake_market_fetcher):
    # tests/integration/test_graph_e2e.py:14 の happy path 構築に合わせる。
    return {
        "checkpointer_factory": InMemorySaver,
        "kb_service": KBService(fake_db, "default"),
        "supplier_service": SupplierService(fake_db, "default"),
        "market_service": MarketDataService("http://x", fetcher=fake_market_fetcher),
    }


@pytest.fixture
def case_payload():
    # tests/integration/test_graph_e2e.py:39 の happy path 入力に合わせる。
    return '{"area": "箱根", "theme": "温泉", "season": "2026-autumn", "consent": true}'


@pytest.fixture
def live_input_context():
    # tests/integration/test_graph_e2e.py:39 の happy path は input_context 追加値なし。
    return {}
