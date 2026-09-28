"""API-adapter integration tests — entry auth + HITL /resume ownership.

Exercises src/api/server.py over HTTP (FastAPI TestClient). Guards the retrofit
invariants:
  - entry auth: unauthenticated caller → 401 (enum OR string ANONYMOUS);
  - /resume ownership: only the recording session may resume (fail-closed);
  - resume actually reaches success (this template already caches its inner
    subgraph, so resume works end-to-end).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.trust_level import TrustLevel
from src.graph.graph import Graph
from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService
from src.services.service import PlanDraftService

_TOKEN = "test-invoke-token-svc033"
_REQUEST = '{"area": "箱根", "theme": "温泉", "season": "2026-autumn", "consent": true}'


@pytest.fixture()
def client(monkeypatch, fake_db, fake_llm, fake_market_fetcher):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    import importlib

    import src.api.server as server

    importlib.reload(server)
    # Rebuild the module-level agent with injected fakes (server otherwise wires
    # real platform clients). Same HITL config as production.
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
    server.agent = agent
    return TestClient(server.app), server


def _auth(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(session_id="sess-owner"):
    return {"input": _REQUEST, "session_id": session_id}


def test_load_config_validates_configured_sample_sources(monkeypatch):
    import src.api.server as server

    recorded = []

    def recorder(past_tours_path, suppliers_path):
        recorded.append((past_tours_path, suppliers_path))
        return {"past_tours": 20, "suppliers": 10}

    monkeypatch.setattr(server, "validate_sample_sources", recorder)
    config = server._load_config()

    assert recorded == [
        (
            config["sample_data"]["past_tours_path"],
            config["sample_data"]["suppliers_path"],
        )
    ]


def test_load_config_propagates_sample_validation_failure(monkeypatch):
    import src.api.server as server

    def fail_validation(past_tours_path, suppliers_path):
        del past_tours_path, suppliers_path
        raise ValueError("configured sample corpus is invalid")

    monkeypatch.setattr(server, "validate_sample_sources", fail_validation)
    with pytest.raises(ValueError, match="configured sample corpus is invalid"):
        server._load_config()


def test_invoke_without_token_is_401(client):
    c, _ = client
    assert c.post("/invoke", json=_body()).status_code == 401


def test_invoke_wrong_token_is_401(client):
    c, _ = client
    assert c.post("/invoke", json=_body(), headers=_auth("nope")).status_code == 401


@pytest.mark.parametrize("stamp", [TrustLevel.ANONYMOUS, "ANONYMOUS"])
def test_invoke_explicit_anonymous_middleware_is_401(client, stamp):
    c, server = client

    @server.app.middleware("http")
    async def _stamp(request, call_next):
        request.state.trust_level = stamp
        return await call_next(request)

    fresh = TestClient(server.app)
    assert fresh.post("/invoke", json=_body()).status_code == 401


def test_invoke_good_token_suspends_at_hitl(client):
    c, _ = client
    r = c.post("/invoke", json=_body(), headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"
    assert r.json()["thread_id"]


def test_resume_without_token_is_401(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    r = c.post("/resume", json={"thread_id": th, "feedback": {"approved": True}, "session_id": "sess-owner"})
    assert r.status_code == 401


def test_resume_foreign_session_is_403(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    r = c.post("/resume", json={"thread_id": th, "feedback": {"approved": True}, "session_id": "attacker"},
               headers=_auth())
    assert r.status_code == 403


def test_resume_unknown_thread_is_404(client):
    c, _ = client
    c.post("/invoke", json=_body(), headers=_auth())
    r = c.post("/resume", json={"thread_id": "00000000-0000-0000-0000-000000000000",
                                "feedback": {"approved": True}, "session_id": "sess-owner"}, headers=_auth())
    assert r.status_code == 404


def test_resume_correct_owner_reaches_success(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    r = c.post("/resume", json={"thread_id": th, "feedback": {"approved": True}, "session_id": "sess-owner"},
               headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "success"


def test_resume_replay_after_completion_is_409(client):
    """A thread that already completed (empty snapshot.next) must not
    be resumable again — a replayed/duplicate resume is rejected fail-closed."""
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    first = c.post("/resume", json={"thread_id": th, "feedback": {"approved": True}, "session_id": "sess-owner"},
                   headers=_auth())
    assert first.status_code == 200 and first.json()["status"] == "success"
    replay = c.post("/resume", json={"thread_id": th, "feedback": {"approved": True}, "session_id": "sess-owner"},
                    headers=_auth())
    assert replay.status_code == 409
