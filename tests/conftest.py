import shutil
import socket
import sqlite3
import unittest.mock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinary import main
from dinary.adapters.rates import helpers
from dinary.config import settings
from dinary.db import category_seed, db_migrations, storage
from dinary.main import create_app
from dinary.sheets import sheet_mapping

# Tests that need the built Vue PWA must depend on ``built_static_dir``;
# the fixture FAILS LOUDLY when ``_static/`` is absent (instead of
# silently skipping) so a missing build never hides a regression.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BUILT_STATIC = _PROJECT_ROOT / "_static"

_REAL_ENSURE_FRESH = sheet_mapping.ensure_fresh


def _migration_connect(self, dburi):
    con = sqlite3.connect(
        str(self.uri.database), detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None
    )
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA busy_timeout=5000")
    return con


@pytest.fixture(scope="session", autouse=True)
def _stub_socket_getfqdn():
    """Prevent yoyo's migration logger from hanging on macOS CI reverse-DNS."""
    with unittest.mock.patch.object(socket, "getfqdn", return_value="localhost"):
        yield


@pytest.fixture(scope="session")
def blank_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db_template") / "dinary.db"
    with unittest.mock.patch.object(db_migrations.SQLiteBackend, "connect", _migration_connect):
        db_migrations.migrate_db(path)
    return path


@pytest.fixture
def db(tmp_path, monkeypatch, blank_db):
    dst = tmp_path / "dinary.db"
    shutil.copy(blank_db, dst)
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", dst)


@pytest.fixture(autouse=True)
def _reset_db_connection():
    """Reset repo-level DB state before AND after each test (currently a no-op placeholder)."""
    yield


@pytest.fixture(autouse=True)
def _empty_llm_provider_preset(monkeypatch, tmp_path):
    """Tests assert on broker state starting from an empty pool; point the preset
    at a non-existent file so the lifespan ``sync`` mirrors nothing and the
    operator's real ``.deploy/llms.toml`` never interferes. Tests that need a
    populated pool override ``settings.llm_providers_file`` themselves."""
    monkeypatch.setattr(settings, "llm_providers_file", tmp_path / "no-llms.toml")


@pytest.fixture(autouse=True)
def _no_operator_dotenv(monkeypatch):
    """The lifespan bootstraps API keys from the operator's real ``.deploy/.env``;
    on a developer machine that silently seeds provider keys tests expect to be
    absent. Tests that want a key set it via ``monkeypatch.setenv``."""
    monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _disable_drain_loop(monkeypatch):
    """Dedicated lifespan tests in test_main.py opt back in by overriding this
    setting inside their own body."""
    monkeypatch.setattr(settings, "sheet_logging_drain_interval_sec", 0)
    monkeypatch.setattr(settings, "receipt_classification_enabled", False)


@pytest.fixture(autouse=True)
def _disable_sheet_mapping_preload(monkeypatch):
    """Skips the lifespan's real Drive+Sheets warm-up by default — costs 1-2s per
    test (up to 10s on a bad connection) and makes CI flaky under Google rate-limiting.
    Tests exercising ``reload_now`` directly patch the Drive/Sheets calls instead."""
    monkeypatch.setattr(settings, "warm_sheet_mapping_timeout_sec", 0)


@pytest.fixture(autouse=True)
def _stub_sheet_mapping_ensure_fresh(monkeypatch):
    """Neutralises ``ensure_fresh`` (a real Drive ``modifiedTime`` GET) by default —
    the ~0.3-0.7s round-trip is swallowed by a broad except, invisible without
    ``--durations``. Tests exercising it directly patch ``drive_get_modified_time``."""
    monkeypatch.setattr(sheet_mapping, "ensure_fresh", lambda: None)


@pytest.fixture
def real_ensure_pool():
    """No-op retained for tests that provision a real pool from their own preset.

    ``ensure_pool`` is no longer stubbed globally (the empty pool now comes from an
    absent preset file), so nothing needs to be restored — the fixture only marks
    intent at the call site."""
    return


@pytest.fixture
def real_ensure_fresh(monkeypatch):
    """Opt out of ``_stub_sheet_mapping_ensure_fresh`` for tests that drive
    ``ensure_fresh`` through its real branches."""
    monkeypatch.setattr(sheet_mapping, "ensure_fresh", _REAL_ENSURE_FRESH)


@pytest.fixture(scope="session")
def built_static_dir() -> Path:
    """Fails loudly (not skip) if absent — silently skipping bundle tests would
    mask real regressions."""
    if not _BUILT_STATIC.is_dir():
        pytest.fail(
            "_static/ not built — run `uv run inv build-static` "
            "(or `npm --prefix webapp run build`) before the tests.",
        )
    return _BUILT_STATIC


@pytest.fixture
def client(db):  # noqa: ARG001
    """``raise_server_exceptions=False`` makes unhandled exceptions come back as a
    production-like 500 instead of re-raising into the test body. Stubs the
    ``rate_prefetch_task`` network call, ``migrate_db`` (temp DB is pre-migrated via
    ``blank_db``), and ``bootstrap_categories`` (most tests seed their own category
    rows; ``tests/category_templates/`` calls it explicitly)."""
    with (
        unittest.mock.patch.object(helpers, "_get_json_or_none", return_value=None),
        unittest.mock.patch.object(db_migrations, "migrate_db"),
        unittest.mock.patch.object(category_seed, "bootstrap_categories"),
    ):
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
