from __future__ import annotations

import json

import pytest

from framework.errors import ConfigError
from src.services import azure_llm
from src.services.azure_llm import _AzureOpenAIClientCompat
from src.graph.graph import Graph
from src.services.kb_backend import create_database, seed_sample_data, validate_sample_sources
from src.services.kb_service import KBService, SupplierService
from src.services.market_data_service import MarketDataService
from src.services.sqlite_database import SQLiteDatabase


def test_sqlite_database_crud_and_transaction_rollback():
    db = SQLiteDatabase(":memory:")
    db.execute(
        "INSERT INTO past_tours (tour_id, tenant_id, tour_name, theme) "
        "VALUES (:tour_id, :tenant_id, :tour_name, :theme)",
        {
            "tour_id": "T001",
            "tenant_id": "default",
            "tour_name": "test tour",
            "theme": "温泉",
        },
    )
    assert db.fetch_one("SELECT tour_name FROM past_tours WHERE tour_id = :tour_id", {"tour_id": "T001"}) == {
        "tour_name": "test tour"
    }
    assert db.fetch_many("SELECT tour_id FROM past_tours") == [{"tour_id": "T001"}]

    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute(
                "UPDATE past_tours SET tour_name = :name WHERE tour_id = :tour_id",
                {"name": "rolled back", "tour_id": "T001"},
            )
            raise RuntimeError("rollback")
    assert db.fetch_one("SELECT tour_name FROM past_tours WHERE tour_id = :tour_id", {"tour_id": "T001"}) == {
        "tour_name": "test tour"
    }
    db.close()


def test_empty_database_returns_zero_win_pattern_samples():
    db = SQLiteDatabase(":memory:")
    assert KBService(db, "default").win_pattern_signals({"theme": "温泉"}) == {
        "sample_count": 0,
        "top_themes": [],
        "avg_rating": None,
    }


def test_sample_seed_is_idempotent():
    db = SQLiteDatabase(":memory:")
    first = seed_sample_data(
        db,
        "default",
        "config/past_tours.sample.json",
        "config/suppliers.sample.json",
    )
    second = seed_sample_data(
        db,
        "default",
        "config/past_tours.sample.json",
        "config/suppliers.sample.json",
    )
    assert first == second == {"past_tours": 20, "suppliers": 10}
    assert db.fetch_one("SELECT COUNT(*) AS count FROM past_tours") == {"count": 20}
    assert db.fetch_one("SELECT COUNT(*) AS count FROM suppliers") == {"count": 10}


def test_unconfigured_sample_sources_are_disabled():
    db = SQLiteDatabase(":memory:")
    assert seed_sample_data(db, "default", None, "") == {
        "past_tours": 0,
        "suppliers": 0,
    }
    assert validate_sample_sources(None, "") == {"past_tours": 0, "suppliers": 0}


def test_configured_missing_sample_file_fails_loud(tmp_path):
    missing = tmp_path / "missing.sample.json"
    with pytest.raises(FileNotFoundError) as exc_info:
        validate_sample_sources(str(missing), None)
    assert str(missing) in str(exc_info.value)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not-json", "Invalid JSON"),
        ("null", "non-empty JSON array"),
        ("[]", "non-empty JSON array"),
        ('{"tour_id":"SAMPLE-T001"}', "non-empty JSON array"),
        ('[{"tour_id":"SAMPLE-T001","tour_name":"ok"},42]', "row 2: expected an object"),
    ],
)
def test_configured_invalid_sample_file_fails_loud(tmp_path, content, message):
    source = tmp_path / "invalid.sample.json"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message) as exc_info:
        validate_sample_sources(str(source), None)
    assert str(source) in str(exc_info.value)


def test_configured_non_utf8_sample_file_fails_loud(tmp_path):
    source = tmp_path / "invalid-encoding.sample.json"
    source.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="not readable UTF-8") as exc_info:
        validate_sample_sources(str(source), None)
    assert str(source) in str(exc_info.value)


def test_seed_rejects_missing_required_key_with_file_row_and_key(tmp_path):
    source = tmp_path / "past_tours.sample.json"
    source.write_text(json.dumps([{"tour_name": "missing id"}]), encoding="utf-8")
    db = SQLiteDatabase(":memory:")
    with pytest.raises(ValueError, match="row 1.*tour_id") as exc_info:
        seed_sample_data(db, "default", str(source), None)
    assert str(source) in str(exc_info.value)
    assert "missing id" not in str(exc_info.value)


def test_validation_requires_supplier_category(tmp_path):
    source = tmp_path / "suppliers.sample.json"
    secret_value = "do-not-leak-supplier-name"
    source.write_text(
        json.dumps([{"supplier_id": "SAMPLE-S001", "supplier_name": secret_value}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="row 1.*category") as exc_info:
        validate_sample_sources(None, str(source))
    assert str(source) in str(exc_info.value)
    assert secret_value not in str(exc_info.value)


def test_repo_relative_sample_paths_work_from_another_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    paths = ("config/past_tours.sample.json", "config/suppliers.sample.json")
    assert validate_sample_sources(*paths) == {"past_tours": 20, "suppliers": 10}

    db = SQLiteDatabase(":memory:")
    assert seed_sample_data(db, "default", *paths) == {"past_tours": 20, "suppliers": 10}
    assert db.fetch_one("SELECT COUNT(*) AS count FROM past_tours") == {"count": 20}
    assert db.fetch_one("SELECT COUNT(*) AS count FROM suppliers") == {"count": 10}


def test_real_sqlite_ingest_rows_supports_ids_and_deterministic_ids():
    db = SQLiteDatabase(":memory:")
    kb = KBService(db, "default")
    suppliers = SupplierService(db, "default")

    assert (
        kb.ingest_rows(
            [
                {"tour_id": "T-EXPLICIT", "ツアー名": "明示IDツアー", "テーマ": "温泉"},
                {"ツアー名": "決定IDツアー", "地域": "箱根"},
            ]
        )
        == 2
    )
    assert (
        suppliers.ingest_rows(
            [
                {"supplier_id": "S-EXPLICIT", "取引先名": "明示ID業者", "種別": "宿泊"},
                {"取引先名": "決定ID業者", "種別": "交通"},
            ]
        )
        == 2
    )

    tours = db.fetch_many("SELECT tour_id FROM past_tours ORDER BY tour_name")
    supplier_rows = db.fetch_many("SELECT supplier_id FROM suppliers ORDER BY supplier_name")
    assert len(tours) == 2 and any(row["tour_id"].startswith("TOUR-") for row in tours)
    assert len(supplier_rows) == 2 and any(row["supplier_id"].startswith("SUPPLIER-") for row in supplier_rows)


def test_create_database_preserves_injected_backend():
    db = SQLiteDatabase(":memory:")
    assert create_database(None, database=db) is db
    with pytest.raises(ValueError, match="path"):
        create_database(None)


def _injected_service_config() -> dict:
    return {
        "kb_service": object(),
        "supplier_service": object(),
        "market_service": MarketDataService("", fetcher=lambda url, params: {}),
        "plan_service": object(),
    }


def test_graph_fails_closed_when_kb_service_and_database_are_missing():
    secret_value = "customer-db-secret-value"
    config = {
        **_injected_service_config(),
        "kb_service": None,
        "db": None,
        "TOUR_KB_DB_PATH": secret_value,
    }
    with pytest.raises(ConfigError, match="KB service.*TOUR_KB_DB_PATH") as exc_info:
        Graph(config=config).compile()
    assert secret_value not in str(exc_info.value)


def test_graph_fails_closed_when_supplier_service_and_database_are_missing():
    secret_value = "customer-db-secret-value"
    config = {
        **_injected_service_config(),
        "supplier_service": None,
        "db": None,
        "TOUR_KB_DB_PATH": secret_value,
    }
    with pytest.raises(ConfigError, match="Supplier service.*TOUR_KB_DB_PATH") as exc_info:
        Graph(config=config).compile()
    assert secret_value not in str(exc_info.value)


def test_graph_fails_closed_when_plan_service_and_llm_are_missing():
    secret_value = "customer-azure-secret-value"
    config = {
        **_injected_service_config(),
        "plan_service": None,
        "llm": None,
        "AZURE_OPENAI_API_KEY": secret_value,
    }
    with pytest.raises(ConfigError, match="Plan service.*AZURE_OPENAI") as exc_info:
        Graph(config=config).compile()
    assert secret_value not in str(exc_info.value)


def test_graph_compiles_when_database_and_llm_are_injected():
    db = SQLiteDatabase(":memory:")
    try:
        Graph(config={"db": db, "llm": object()}).compile()
    finally:
        db.close()


def test_azure_compat_forwards_generation_options(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}],"model":"sample"}'

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(azure_llm, "urlopen", fake_urlopen)
    client = _AzureOpenAIClientCompat(
        {
            "api_key": "sample",
            "azure_endpoint": "https://azure.example.invalid",
            "azure_deployment": "sample-deployment",
            "temperature": 0.2,
            "max_tokens": 321,
        }
    )
    assert client.complete([{"role": "user", "content": "hello"}])["content"] == "ok"
    assert captured["body"]["temperature"] == 0.2
    assert captured["body"]["max_tokens"] == 321
