"""AgentCore Platform v1.0 — LLM tour-plan drafting service (SVC-C2-033).

PlanDraftService wraps the framework LLM client
(`shared.services.llm.base_llm.BaseLLM`) and turns KB signals + market data
into a structured tour-plan draft (concept / itinerary / differentiation / target).

The LLM is injected (constructor seam) so tests use a BaseLLM-compatible fake.
Grounding rule (anti-fabrication): when market_data is empty (degraded fetch),
the prompt explicitly instructs the model to plan from internal performance only
and not invent external facts (event names, venues).
"""

from __future__ import annotations

import json
from typing import Any


class PlanDraftService:
    """Draft a tour plan from internal signals + (optional) market data."""

    def __init__(self, llm: Any) -> None:
        # llm implements BaseLLM.complete(messages: list) -> dict
        self._llm = llm

    def draft(self, planning_request: dict, kb_signals: dict, market_data: dict) -> dict:
        """Return a structured plan draft dict."""
        degraded = not market_data
        system = (
            "あなたは旅行会社の商品企画担当を支援するアシスタントです。"
            "社内の勝ちパターン（過去実績シグナル）に根ざしてツアー企画ドラフトを作成します。"
            "出力は JSON で concept / itinerary / differentiation / target の4キー。"
        )
        if degraded:
            system += (
                "【注意】外部観光データが取得できませんでした。社内実績シグナルのみで起案し、"
                "存在の確認できないイベント名・施設名など外部固有名詞を創作しないこと。"
            )
        user = json.dumps(
            {
                "planning_request": planning_request,
                "win_pattern_signals": kb_signals,
                "market_data": market_data,
            },
            ensure_ascii=False,
            default=str,
        )
        resp = self._llm.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        content = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> dict:
        """Parse the LLM JSON response; fall back to a concept-only structure."""
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return {
                    "concept": parsed.get("concept", ""),
                    "itinerary": parsed.get("itinerary", []),
                    "differentiation": parsed.get("differentiation", ""),
                    "target": parsed.get("target", ""),
                }
        except (json.JSONDecodeError, TypeError):
            pass
        return {"concept": content, "itinerary": [], "differentiation": "", "target": ""}
