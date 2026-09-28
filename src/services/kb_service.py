"""AgentCore Platform v1.0 — internal KB services (SVC-C2-033).

KBService / SupplierService wrap the framework-provided DB client
(`shared.services.database.base_db.BaseDatabase`) over the tenant-separated
`past_tours` / `suppliers` tables (DDL in docs/02_design.md).

Sensitive-info invariant (S-3): `suppliers.contact` and `suppliers.standard_terms`
are sensitive. These services expose:
  - non-sensitive signals / match refs (safe to place in State), and
  - a resolve_* method that returns sensitive values — called ONLY from inside
    the post_process S-3 gate, never written back into checkpointed State.

Excel/CSV first-class ingest (KBService.ingest_rows / SupplierService.ingest_rows)
maps loosely-named columns into the normalized schema so travel agencies without a
structured KB can still use the agent. Ingest is an ops job (docs/07), not part of
the invoke flow; invoke only reads the normalized tables.

The DB client is injected (constructor seam) so tests use a fake BaseDatabase.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Column alias table — absorbs Excel/CSV header drift into the normalized schema.
_PAST_TOURS_ALIASES = {
    "tour_id": ("tour_id", "ツアーID", "商品ID"),
    "tour_name": ("tour_name", "name", "ツアー名", "商品名"),
    "period": ("period", "season", "期間", "シーズン"),
    "theme": ("theme", "テーマ"),
    "area": ("area", "エリア", "地域"),
    "price_band": ("price_band", "price", "単価帯", "価格帯"),
    "units_sold": ("units_sold", "sold", "販売数"),
    "gross_margin": ("gross_margin", "margin", "粗利"),
    "rating": ("rating", "評価"),
}

_SUPPLIER_ALIASES = {
    "supplier_id": ("supplier_id", "サプライヤーID", "取引先ID"),
    "supplier_name": ("supplier_name", "name", "業者名", "取引先名"),
    "category": ("category", "種別", "区分"),
    "area": ("area", "エリア", "地域"),
    "contact": ("contact", "連絡先", "担当"),
    "standard_terms": ("standard_terms", "terms", "条件", "標準条件"),
    "lead_time_days": ("lead_time_days", "lead_time", "リードタイム"),
}


def _map_row(row: dict, aliases: dict) -> dict:
    """Map one loosely-named input row into the normalized column set."""
    out: dict[str, Any] = {}
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for canonical, names in aliases.items():
        for n in names:
            if n.lower() in lowered:
                out[canonical] = lowered[n.lower()]
                break
    return out


class KBService:
    """Read win-pattern signals from `past_tours` (non-sensitive)."""

    def __init__(self, db: Any, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    def ingest_rows(self, rows: list[dict]) -> int:
        """Ops-job: UPSERT normalized past-tour rows from Excel/CSV. Returns count."""
        mapped = [_map_row(r, _PAST_TOURS_ALIASES) for r in rows]
        count = 0
        for m in mapped:
            if not m.get("tour_name"):
                continue
            if not m.get("tour_id"):
                identity = f"{self._tenant_id}:{m['tour_name']}".encode("utf-8")
                m["tour_id"] = f"TOUR-{hashlib.sha1(identity).hexdigest()[:12]}"
            params = {
                key: m.get(key)
                for key in (
                    "tour_id",
                    "tour_name",
                    "period",
                    "theme",
                    "area",
                    "price_band",
                    "units_sold",
                    "gross_margin",
                    "rating",
                )
            }
            params["tenant_id"] = self._tenant_id
            self._db.execute(
                "INSERT INTO past_tours "
                "(tour_id, tenant_id, tour_name, period, theme, area, price_band, "
                "units_sold, gross_margin, rating) VALUES "
                "(:tour_id, :tenant_id, :tour_name, :period, :theme, :area, :price_band, "
                ":units_sold, :gross_margin, :rating) "
                "ON CONFLICT(tour_id) DO UPDATE SET "
                "tenant_id=excluded.tenant_id, tour_name=excluded.tour_name, "
                "period=excluded.period, theme=excluded.theme, area=excluded.area, "
                "price_band=excluded.price_band, units_sold=excluded.units_sold, "
                "gross_margin=excluded.gross_margin, rating=excluded.rating",
                params,
            )
            count += 1
        return count

    def win_pattern_signals(self, planning_request: dict) -> dict:
        """Return NON-sensitive aggregate signals for the LLM to ground on.

        Safe to place in State — contains no PII / no sensitive columns.
        """
        rows = self._db.fetch_many(
            "SELECT theme, area, price_band, units_sold, gross_margin, rating "
            "FROM past_tours WHERE tenant_id = :tenant_id "
            "AND (:theme = '' OR theme = :theme) AND (:area = '' OR area = :area)",
            {
                "tenant_id": self._tenant_id,
                "theme": planning_request.get("theme", "") or "",
                "area": planning_request.get("area", "") or "",
            },
        ) or []
        if not rows:
            return {"sample_count": 0, "top_themes": [], "avg_rating": None}
        themes: dict[str, int] = {}
        ratings = []
        for r in rows:
            themes[r.get("theme", "?")] = themes.get(r.get("theme", "?"), 0) + 1
            if r.get("rating") is not None:
                ratings.append(float(r["rating"]))
        top_themes = sorted(themes, key=lambda t: themes[t], reverse=True)[:5]
        return {
            "sample_count": len(rows),
            "top_themes": top_themes,
            "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        }


class SupplierService:
    """Match required materials to suppliers; resolve sensitive values gate-side."""

    def __init__(self, db: Any, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    def ingest_rows(self, rows: list[dict]) -> int:
        """Ops-job: UPSERT normalized supplier rows from Excel/CSV. Returns count."""
        count = 0
        for r in rows:
            m = _map_row(r, _SUPPLIER_ALIASES)
            if not m.get("supplier_name"):
                continue
            if not m.get("supplier_id"):
                identity = f"{self._tenant_id}:{m['supplier_name']}".encode("utf-8")
                m["supplier_id"] = f"SUPPLIER-{hashlib.sha1(identity).hexdigest()[:12]}"
            params = {
                key: m.get(key)
                for key in (
                    "supplier_id",
                    "supplier_name",
                    "category",
                    "area",
                    "contact",
                    "standard_terms",
                    "lead_time_days",
                )
            }
            params["tenant_id"] = self._tenant_id
            self._db.execute(
                "INSERT INTO suppliers "
                "(supplier_id, tenant_id, supplier_name, category, area, contact, "
                "standard_terms, lead_time_days) VALUES "
                "(:supplier_id, :tenant_id, :supplier_name, :category, :area, :contact, "
                ":standard_terms, :lead_time_days) "
                "ON CONFLICT(supplier_id) DO UPDATE SET "
                "tenant_id=excluded.tenant_id, supplier_name=excluded.supplier_name, "
                "category=excluded.category, area=excluded.area, contact=excluded.contact, "
                "standard_terms=excluded.standard_terms, lead_time_days=excluded.lead_time_days",
                params,
            )
            count += 1
        return count

    def match_refs(self, required_materials: list) -> list:
        """Return match refs (supplier_id + matched flag) — NO contact/terms.

        Safe to place in State.
        """
        refs = []
        for item in required_materials:
            row = self._db.fetch_one(
                "SELECT supplier_id FROM suppliers WHERE tenant_id = :tenant_id "
                "AND category = :category LIMIT 1",
                {"tenant_id": self._tenant_id, "category": item},
            )
            refs.append(
                {"item": item, "supplier_id": row.get("supplier_id") if row else None,
                 "matched": bool(row)}
            )
        return refs

    def resolve_sensitive(self, supplier_id: str) -> dict:
        """Return sensitive contact/terms for a supplier_id.

        S-3 CONTRACT: call ONLY from inside the post_process S-3 gate, when
        building the final gated output. Never write the return value back into
        checkpointed State.
        """
        row = self._db.fetch_one(
            "SELECT supplier_name, contact, standard_terms, lead_time_days "
            "FROM suppliers WHERE tenant_id = :tenant_id AND supplier_id = :sid",
            {"tenant_id": self._tenant_id, "sid": supplier_id},
        )
        return row or {}
