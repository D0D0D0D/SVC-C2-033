"""SQLite implementation of the SDK ``BaseDatabase`` persistence port."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.services.database.base_db import BaseDatabase


_SCHEMA = """
CREATE TABLE IF NOT EXISTS past_tours (
    tour_id      TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    tour_name    TEXT NOT NULL,
    period       TEXT,
    theme        TEXT,
    area         TEXT,
    price_band   TEXT,
    units_sold   INTEGER,
    gross_margin NUMERIC,
    rating       NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_past_tours_tenant_id ON past_tours (tenant_id);
CREATE INDEX IF NOT EXISTS idx_past_tours_theme ON past_tours (theme);
CREATE INDEX IF NOT EXISTS idx_past_tours_area ON past_tours (area);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id    TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    supplier_name  TEXT NOT NULL,
    category       TEXT,
    area           TEXT,
    contact        TEXT,
    standard_terms TEXT,
    lead_time_days INTEGER
);
CREATE INDEX IF NOT EXISTS idx_suppliers_tenant_id ON suppliers (tenant_id);
CREATE INDEX IF NOT EXISTS idx_suppliers_category ON suppliers (category);
CREATE INDEX IF NOT EXISTS idx_suppliers_area ON suppliers (area);
"""


class SQLiteDatabase(BaseDatabase):
    """Concrete SQLite adapter with transaction and in-memory support."""

    def __init__(self, path: str):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("SQLite database path must be a non-empty string")

        configured_path = path.strip()
        self._local = threading.local()
        self._keeper: sqlite3.Connection | None = None
        if configured_path == ":memory:":
            self._database = f"file:svc_c2_033_{uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._open_connection()
            schema_connection = self._keeper
        else:
            database_path = Path(configured_path).expanduser()
            if not database_path.parent.exists():
                raise ValueError(f"SQLite database parent directory does not exist: {database_path.parent}")
            self._database = str(database_path)
            self._uri = False
            schema_connection = self._open_connection()

        try:
            schema_connection.executescript(_SCHEMA)
            schema_connection.commit()
        finally:
            if schema_connection is not self._keeper:
                schema_connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30.0, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @property
    def _transaction_connection(self) -> sqlite3.Connection | None:
        return getattr(self._local, "connection", None)

    @staticmethod
    def _adapt_params(params: dict | None) -> dict[str, Any]:
        adapted: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if isinstance(value, bool):
                adapted[key] = int(value)
            elif isinstance(value, (date, datetime)):
                adapted[key] = value.isoformat()
            else:
                adapted[key] = value
        return adapted

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run calls in this block on one atomic write transaction."""

        if self._transaction_connection is not None:
            raise RuntimeError("nested SQLite transactions are not supported")
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            try:
                yield
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        finally:
            self._local.connection = None
            connection.close()

    def fetch_one(self, query: str, params: dict | None = None) -> dict | None:
        connection = self._transaction_connection or self._open_connection()
        owns_connection = connection is not self._transaction_connection
        try:
            row = connection.execute(query, self._adapt_params(params)).fetchone()
            return dict(row) if row is not None else None
        finally:
            if owns_connection:
                connection.close()

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        connection = self._transaction_connection or self._open_connection()
        owns_connection = connection is not self._transaction_connection
        try:
            rows = connection.execute(query, self._adapt_params(params)).fetchall()
            return [dict(row) for row in rows]
        finally:
            if owns_connection:
                connection.close()

    def execute(self, query: str, params: dict | None = None) -> Any:
        connection = self._transaction_connection or self._open_connection()
        owns_connection = connection is not self._transaction_connection
        try:
            cursor = connection.execute(query, self._adapt_params(params))
            if owns_connection:
                connection.commit()
            return cursor.rowcount
        except BaseException:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection.close()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
