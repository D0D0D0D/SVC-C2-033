from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from src.graph.graph import Graph
from src.services.azure_llm import AzureMarketplaceLLM
from src.services.market_data_service import MarketDataService

_REQUIRED_ENV = {
    "AZURE_OPENAI_API_KEY": "sample-key-for-test",
    "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
    "AZURE_OPENAI_DEPLOYMENT": "sample-deployment",
}


class _NullProvider(SecretProvider):
    def get(self, key: str, default: str | None = None) -> str | None:
        del key
        return default


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return {
            "content": json.dumps(
                {
                    "concept": "サンプルKBに基づく箱根温泉企画",
                    "itinerary": [{"day": 1, "categories": ["宿泊", "交通", "体験"]}],
                    "differentiation": "温泉テーマの評価集計を利用",
                    "target": "少人数旅行者",
                },
                ensure_ascii=False,
            ),
            "tool_calls": [],
            "model": "counting-fake",
            "usage": {},
        }


def _runtime_config() -> dict:
    root = Path(__file__).resolve().parents[2]
    return load_config(str(root / "config" / "config.yaml"))


def _set_required_env(monkeypatch, db_path: Path) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TOUR_KB_DB_PATH", str(db_path))


def _ctx() -> InvocationContext:
    return InvocationContext(
        session_id="marketplace-session",
        caller_id="marketplace-user",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def test_runner_order_invokes_real_sqlite_samples_without_history_leak(monkeypatch, tmp_path):
    _set_required_env(monkeypatch, tmp_path / "marketplace.sqlite3")
    llm = _CountingLLM()
    config = _runtime_config()
    config["llm"] = llm
    config["market_service"] = MarketDataService("", fetcher=lambda url, params: {})

    agent = cli.MarketplaceGraph(config=config)
    agent.provision_secrets(_NullProvider())
    agent.compile()
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(
            '{"area":"箱根","theme":"温泉","season":"2026-autumn","consent":true}',
            ctx=_ctx(),
            input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
        )

    serialized = json.dumps(result, ensure_ascii=False, default=str)
    output = result.get("output") or {}
    assert result["status"] == "success"
    assert output
    assert "SAMPLE-S" in serialized
    assert output["kb_signals"]["sample_count"] >= 1
    assert "CANARY" not in serialized
    assert result["status"] != "awaiting_human"
    assert llm.calls >= 1


def test_marketplace_constructor_injects_defaults_and_preserves_callers():
    config = _runtime_config()
    config["db"] = None
    config["llm"] = None
    defaulted = cli.MarketplaceGraph(config=config)
    assert defaulted.config["db"].__class__.__name__ == "LazyKBDatabase"
    assert isinstance(defaulted.config["llm"], AzureMarketplaceLLM)
    assert vars(defaulted.config["llm"]) == {}

    caller_db = object()
    caller_llm = _CountingLLM()
    preserved = cli.MarketplaceGraph(config={**config, "db": caller_db, "llm": caller_llm})
    assert preserved.config["db"] is caller_db
    assert preserved.config["llm"] is caller_llm


def test_marketplace_llm_bind_tools_returns_new_wrapper():
    llm = AzureMarketplaceLLM()
    bound = llm.bind_tools([{"type": "function"}])
    assert bound is not llm
    assert vars(llm) == {}


def test_invoke_rebuilds_context_disables_hitl_and_decodes_json(monkeypatch):
    captured = {}

    def record_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, session_id
        captured.update(user_input=user_input, ctx=ctx, input_context=input_context)
        return {"status": "success", "output": {"ok": True}}

    monkeypatch.setattr(Graph, "invoke", record_invoke)
    agent = cli.MarketplaceGraph(config={"db": object(), "llm": _CountingLLM()})
    result = agent.invoke(
        r'{"theme":"\u6e29\u6cc9","consent":true}',
        ctx=_ctx(),
        input_context={"conversation_history": [{"content": "CANARY"}]},
    )
    assert result["status"] == "success"
    assert captured["input_context"] == {}
    assert "温泉" in captured["user_input"]
    assert r"\u6e29" not in captured["user_input"]
    assert captured["ctx"].hitl_allowed is False


@pytest.mark.parametrize("missing", list(cli._REQUIRED_SECRET_KEYS))
def test_provision_secrets_fails_closed_when_required_env_missing(monkeypatch, tmp_path, missing):
    _set_required_env(monkeypatch, tmp_path / "missing.sqlite3")
    monkeypatch.delenv(missing)
    agent = cli.MarketplaceGraph(config=_runtime_config())
    with pytest.raises(Exception):
        agent.provision_secrets(_NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, tmp_path, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_env(monkeypatch, tmp_path / "missing.sqlite3")
    agent = cli.MarketplaceGraph(config=_runtime_config())
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(_NullProvider())


@pytest.mark.parametrize("dirty", list(cli._REQUIRED_SECRET_KEYS))
def test_provision_secrets_rejects_whitespace(monkeypatch, tmp_path, dirty):
    _set_required_env(monkeypatch, tmp_path / "dirty.sqlite3")
    monkeypatch.setenv(dirty, " dirty-value")
    agent = cli.MarketplaceGraph(config=_runtime_config())
    with pytest.raises(ValueError, match="clean non-empty"):
        agent.provision_secrets(_NullProvider())


def test_provision_secrets_fails_when_configured_sample_file_is_missing(monkeypatch, tmp_path):
    _set_required_env(monkeypatch, tmp_path / "sample-source.sqlite3")
    missing = tmp_path / "missing.sample.json"
    config = _runtime_config()
    config["sample_data"] = {
        **config["sample_data"],
        "past_tours_path": str(missing),
    }
    agent = cli.MarketplaceGraph(config=config)
    with pytest.raises(FileNotFoundError) as exc_info:
        agent.provision_secrets(_NullProvider())
    assert str(missing) in str(exc_info.value)


def test_main_passes_agent_identity_and_loaded_config(monkeypatch):
    recorded = {}

    def recorder(agent_cls, *, agent_name, namespace, config):
        recorded.update(
            agent_cls=agent_cls,
            agent_name=agent_name,
            namespace=namespace,
            config=config,
        )

    monkeypatch.setattr(cli, "run_agent_marketplace", recorder)
    cli.main()
    assert recorded["agent_cls"] is cli.MarketplaceGraph
    assert recorded["agent_name"] == "SVC-C2-033"
    assert recorded["namespace"] == "svc"
    assert recorded["config"]["tenant_id"] == "default"
    assert recorded["config"]["sample_data"]["past_tours_path"].endswith(".sample.json")
