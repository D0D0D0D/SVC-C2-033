"""AGENTIC STAR Marketplace entry adapter for SVC-C2-033."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.utils.config_loader import load_config

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import Graph
from src.services.azure_llm import AzureMarketplaceLLM
from src.services.kb_backend import LazyKBDatabase, validate_sample_sources

_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-033"
_PROVIDER_AGENT_NAME = "svc_c2_033"

_REQUIRED_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "TOUR_KB_DB_PATH",
)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only deployment values consumed by this Marketplace adapter."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _REQUIRED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class MarketplaceGraph(Graph):
    """Marketplace boundary for safe context, HITL, secrets, DB, and LLM."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(config or {})
        sample_data = merged.get("sample_data") or {}
        if merged.get("db") is None:
            merged["db"] = LazyKBDatabase(
                tenant_id=merged.get("tenant_id", "default"),
                past_tours_path=sample_data.get("past_tours_path"),
                suppliers_path=sample_data.get("suppliers_path"),
            )
        if merged.get("llm") is None:
            merged["llm"] = AzureMarketplaceLLM()
        super().__init__(config=merged)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        normalized_input = user_input
        try:
            parsed = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            if isinstance(parsed, dict):
                normalized_input = json.dumps(parsed, ensure_ascii=False)
        ctx = replace(ctx, hitl_allowed=False) if ctx is not None else ctx
        return cast(
            dict[str, Any],
            super().invoke(
                normalized_input,
                session_id=session_id,
                ctx=ctx,
                input_context={},
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        sample_data = self.config.get("sample_data") or {}
        validate_sample_sources(
            sample_data.get("past_tours_path"),
            sample_data.get("suppliers_path"),
        )
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def _load_marketplace_config() -> dict[str, Any]:
    agent_dir = Path(__file__).resolve().parent
    config = load_agent_config(agent_dir)
    if not config:
        config = load_config(str(agent_dir / "config" / "config.yaml"))
    return config


def main() -> None:
    config = _load_marketplace_config()
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
