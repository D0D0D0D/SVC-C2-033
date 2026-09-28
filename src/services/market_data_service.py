"""AgentCore Platform v1.0 — market (tourism) data service (SVC-C2-033).

MarketDataService fetches seasonal / event / area info. Default source is the
Tokyo tourism open-data API, but the source is a config-swappable seam
(customer-provided URL / other municipality API).

Graceful degradation (design L-02): on API failure / rate-limit / timeout the
service returns {} instead of raising, so plan drafting can continue on internal
KB signals alone. The caller emits a "market_fetch_degraded" trace event and
instructs the LLM not to fabricate external facts when market_data is empty.

The HTTP fetcher is injected (constructor seam) so tests run offline.
"""

from __future__ import annotations

from typing import Any, Callable


class MarketDataService:
    """Fetch public tourism data with graceful degradation."""

    def __init__(
        self,
        source_url: str,
        fetcher: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self._source_url = source_url
        # fetcher(url, params) -> dict. Injected in tests; default uses urllib.
        self._fetcher = fetcher or self._default_fetcher

    def fetch(self, planning_request: dict) -> tuple[dict, bool]:
        """Return (market_data, degraded).

        degraded=True means the fetch failed and market_data is {} — the caller
        must ground the LLM on internal KB only.
        """
        params = {
            "area": planning_request.get("area", ""),
            "season": planning_request.get("season", ""),
            "theme": planning_request.get("theme", ""),
        }
        try:
            data = self._fetcher(self._source_url, params)
            return (data or {}), False
        except Exception:
            # Graceful degradation — never propagate a fetch failure.
            return {}, True

    @staticmethod
    def _default_fetcher(url: str, params: dict) -> dict:
        """Default HTTP GET returning JSON. Kept minimal; tests inject a fake."""
        import json
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        full = f"{url}?{query}" if query else url
        with urllib.request.urlopen(full, timeout=10) as resp:  # noqa: S310
            payload: Any = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"items": payload}
