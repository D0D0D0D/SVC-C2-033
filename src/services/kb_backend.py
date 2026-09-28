"""Construction seam, lazy database proxy, and sample-data seeding."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from framework.secrets.context import current_secrets
from shared.services.database.base_db import BaseDatabase

from src.services.sqlite_database import SQLiteDatabase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAST_TOUR_REQUIRED_KEYS = ("tour_id", "tour_name")
_SUPPLIER_REQUIRED_KEYS = ("supplier_id", "supplier_name", "category")


def create_database(
    sqlite_path: str | None,
    *,
    database: BaseDatabase | None = None,
) -> BaseDatabase:
    """Build the current backend while preserving a future backend seam."""

    if database is not None:
        return database
    if sqlite_path is None:
        raise ValueError("SQLite database path secret is required")
    return SQLiteDatabase(sqlite_path)


def _resolve_source(path: str) -> Path:
    source = Path(path)
    return source if source.is_absolute() else _REPO_ROOT / source


def _load_rows(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    source = _resolve_source(path)
    if not source.is_file():
        raise FileNotFoundError(f"Sample data file does not exist: {source}")
    try:
        content = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Sample data file is not readable UTF-8: {source}") from exc
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in sample data file {source} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Sample data file must contain a non-empty JSON array: {source}")
    for row_number, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Sample data file {source}, row {row_number}: expected an object")
    return payload


def _missing_required_keys(row: dict[str, Any], required_keys: tuple[str, ...]) -> list[str]:
    return [key for key in required_keys if not row.get(key)]


def _raise_missing_keys(path: str, row_number: int, missing_keys: list[str]) -> None:
    source = _resolve_source(path)
    keys = ", ".join(missing_keys)
    raise ValueError(f"Sample data file {source}, row {row_number}: missing required key(s): {keys}")


def _validate_required_rows(
    rows: list[dict[str, Any]],
    path: str | None,
    required_keys: tuple[str, ...],
) -> None:
    if not path:
        return
    for row_number, row in enumerate(rows, start=1):
        missing_keys = _missing_required_keys(row, required_keys)
        if missing_keys:
            _raise_missing_keys(path, row_number, missing_keys)


def validate_sample_sources(
    past_tours_path: str | None,
    suppliers_path: str | None,
) -> dict[str, int]:
    """Validate configured sample corpora without opening the database."""

    tour_rows = _load_rows(past_tours_path)
    supplier_rows = _load_rows(suppliers_path)
    _validate_required_rows(tour_rows, past_tours_path, _PAST_TOUR_REQUIRED_KEYS)
    _validate_required_rows(supplier_rows, suppliers_path, _SUPPLIER_REQUIRED_KEYS)
    return {"past_tours": len(tour_rows), "suppliers": len(supplier_rows)}


def seed_sample_data(
    db: BaseDatabase,
    tenant_id: str,
    past_tours_path: str | None = None,
    suppliers_path: str | None = None,
) -> dict[str, int]:
    """Idempotently upsert explicitly labelled sample rows."""

    tour_rows = _load_rows(past_tours_path)
    supplier_rows = _load_rows(suppliers_path)
    tour_count = 0
    supplier_count = 0

    for row_number, row in enumerate(tour_rows, start=1):
        missing_keys = _missing_required_keys(row, _PAST_TOUR_REQUIRED_KEYS)
        if missing_keys:
            _raise_missing_keys(past_tours_path or "", row_number, missing_keys)
        params = {
            key: row.get(key)
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
        params["tenant_id"] = tenant_id
        db.execute(
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
        tour_count += 1

    for row_number, row in enumerate(supplier_rows, start=1):
        missing_keys = _missing_required_keys(row, _SUPPLIER_REQUIRED_KEYS)
        if missing_keys:
            _raise_missing_keys(suppliers_path or "", row_number, missing_keys)
        params = {
            key: row.get(key)
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
        params["tenant_id"] = tenant_id
        db.execute(
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
        supplier_count += 1

    return {"past_tours": tour_count, "suppliers": supplier_count}


class LazyKBDatabase(BaseDatabase):
    """Resolve the deployment-owned SQLite path on first database operation."""

    def __init__(
        self,
        *,
        tenant_id: str = "default",
        past_tours_path: str | None = None,
        suppliers_path: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._past_tours_path = past_tours_path
        self._suppliers_path = suppliers_path
        self._database: BaseDatabase | None = None
        self._lock = threading.Lock()

    def _get_database(self) -> BaseDatabase:
        database = self._database
        if database is not None:
            return database
        with self._lock:
            database = self._database
            if database is None:
                path = current_secrets().require("TOUR_KB_DB_PATH")
                database = create_database(path)
                seed_sample_data(
                    database,
                    self._tenant_id,
                    self._past_tours_path,
                    self._suppliers_path,
                )
                self._database = database
        return database

    def fetch_one(self, query: str, params: dict | None = None) -> dict | None:
        return self._get_database().fetch_one(query, params)

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        return self._get_database().fetch_many(query, params)

    def execute(self, query: str, params: dict | None = None) -> Any:
        return self._get_database().execute(query, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        database = self._get_database()
        transaction = getattr(database, "transaction", None)
        if transaction is None:
            yield
            return
        with transaction():
            yield

    def close(self) -> None:
        database = self._database
        if database is not None:
            close = getattr(database, "close", None)
            if close is not None:
                close()
