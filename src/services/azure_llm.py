"""Lazy Azure OpenAI client for Marketplace and API runtime paths."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import quote
from urllib.request import Request, urlopen

from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

_AZURE_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


def _require_clean(provider: SecretProvider, key: str) -> str:
    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Secret {key!r} must be a clean non-empty string")
    return value


class _AzureOpenAIClientCompat(BaseLLM):
    """urllib client for wheels without the Azure OpenAI integration extra."""

    def __init__(self, config: dict[str, Any], *, tools: tuple[Any, ...] = ()) -> None:
        self._config = dict(config)
        self._tools = tools

    def complete(self, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        body: dict[str, Any] = {"messages": messages}
        for key in ("temperature", "max_tokens"):
            value = kwargs.get(key, self._config.get(key))
            if value is not None:
                body[key] = value
        if self._tools:
            body["tools"] = list(self._tools)
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps(body).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        model = payload.get("model", self._config["azure_deployment"])
        if isinstance(usage, dict) and usage:
            usage = {**usage, "model": usage.get("model", model)}
        return {
            "content": message.get("content", "") if isinstance(message, dict) else "",
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": model,
            "usage": usage,
        }

    def stream(self, messages: list[Any], **kwargs: Any) -> Iterator[str]:
        content = self.complete(messages, **kwargs).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return type(self)(self._config, tools=tuple(tools))


class AzureMarketplaceLLM(BaseLLM):
    """Resolve clean Azure connection values separately for every operation."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self, **kwargs: Any) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_SECRET_KEYS}
        config: dict[str, Any] = {
            "api_key": values["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
        }
        for key in ("temperature", "max_tokens"):
            if kwargs.get(key) is not None:
                config[key] = kwargs[key]

        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError:
            client: BaseLLM = _AzureOpenAIClientCompat(config)
        else:
            client = cast(BaseLLM, AzureOpenAIClient(config))

        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client(**kwargs).complete(messages))

    def stream(self, messages: list[Any], **kwargs: Any) -> Iterator[str]:
        yield from self._new_client(**kwargs).stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return type(self)(tools=tuple(tools))
