"""Tests for the unified SQLite migration in ``db_migrations.migrate_db``.

After the storage-engine port there is only one migration target:
``data/dinary.db``. These tests verify that applying the bundled
migrations to a fresh file produces the expected schema and seed rows.
"""

import asyncio
import sqlite3

import allure
import llmbroker
import pytest
from llmbroker.backends import spec as llmbroker_spec

from dinary.config import settings
from dinary.db import db_migrations, storage
from dinary.db.catalog import get_catalog_version


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point ``ledger_repo`` at an empty tmp file and apply all migrations."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "dinary.db")
    db_migrations.migrate_db(storage.DB_PATH)
    return storage.DB_PATH


def _connect(path) -> sqlite3.Connection:
    # Foreign-key enforcement matches runtime; the yoyo bookkeeping
    # table is tolerated in listings below.
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _table_names(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'",
    ).fetchall()
    return {r[0] for r in rows}


def _column_names(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
    return {r[1] for r in rows}


@allure.epic("Infrastructure")
@allure.feature("Migrations")
class TestInitialSchema:
    def test_creates_expected_catalog_tables(self, fresh_db):
        con = _connect(fresh_db)
        try:
            tables = _table_names(con)
        finally:
            con.close()

        expected = {
            "category_groups",
            "categories",
            "events",
            "tags",
            "exchange_rates",
            "import_mapping",
            "import_mapping_tags",
            "sheet_mapping",
            "sheet_mapping_tags",
            "app_metadata",
        }
        assert expected.issubset(tables)
        assert "import_sources" not in tables, (
            "import_sources migrated out of the ledger — the registry now "
            "lives in .deploy/import_sources.json (see dinary.config)."
        )

    def test_creates_expected_ledger_tables(self, fresh_db):
        con = _connect(fresh_db)
        try:
            tables = _table_names(con)
        finally:
            con.close()

        assert {"expenses", "expense_tags", "sheet_logging_jobs", "income"}.issubset(tables)

    def test_no_old_config_or_budget_tables(self, fresh_db):
        """The old split-DB refactor dropped these legacy artefacts."""
        con = _connect(fresh_db)
        try:
            tables = _table_names(con)
        finally:
            con.close()

        assert "expense_id_registry" not in tables

    def test_catalog_tables_have_is_active_column(self, fresh_db):
        con = _connect(fresh_db)
        try:
            for table in ("category_groups", "categories", "events", "tags"):
                assert "is_active" in _column_names(con, table), table
        finally:
            con.close()

    def test_app_metadata_is_key_value(self, fresh_db):
        con = _connect(fresh_db)
        try:
            cols = _column_names(con, "app_metadata")
            row = con.execute(
                "SELECT value FROM app_metadata WHERE key = 'catalog_version'",
            ).fetchone()
        finally:
            con.close()
        assert cols == {"key", "value"}
        assert row is not None
        assert row[0] == "1"

    def test_expenses_has_client_expense_id_unique(self, fresh_db):
        con = _connect(fresh_db)
        try:
            assert "client_expense_id" in _column_names(con, "expenses")
            con.execute(
                "INSERT INTO category_groups (id, name, sort_order) VALUES (1, 'g', 1)",
            )
            con.execute(
                "INSERT INTO categories (id, name, group_id) VALUES (1, 'c', 1)",
            )
            con.execute(
                "INSERT INTO expenses (client_expense_id, datetime, amount,"
                " amount_original, currency_original, category_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ["cid-1", "2026-04-15 12:00:00", 100, 100, "RSD", 1],
            )
            # Re-inserting the same client_expense_id must violate UNIQUE.
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO expenses (client_expense_id, datetime, amount,"
                    " amount_original, currency_original, category_id)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    ["cid-1", "2026-04-15 12:00:00", 100, 100, "RSD", 1],
                )
            # NULL client_expense_id is allowed many times over (bootstrap rows).
            con.execute(
                "INSERT INTO expenses (client_expense_id, datetime, amount,"
                " amount_original, currency_original, category_id)"
                " VALUES (NULL, ?, ?, ?, ?, ?)",
                ["2026-04-15 12:00:00", 100, 100, "RSD", 1],
            )
            con.execute(
                "INSERT INTO expenses (client_expense_id, datetime, amount,"
                " amount_original, currency_original, category_id)"
                " VALUES (NULL, ?, ?, ?, ?, ?)",
                ["2026-04-16 12:00:00", 50, 50, "RSD", 1],
            )
        finally:
            con.close()

    def test_sheet_logging_jobs_is_keyed_by_expense_id(self, fresh_db):
        con = _connect(fresh_db)
        try:
            cols = _column_names(con, "sheet_logging_jobs")
        finally:
            con.close()
        assert "expense_id" in cols
        assert "status" in cols
        assert "claim_token" in cols

    def test_idempotent_reapply(self, fresh_db):
        """Running migrate_db twice is a no-op (yoyo records applied migrations)."""
        db_migrations.migrate_db(fresh_db)
        db_migrations.migrate_db(fresh_db)

        con = _connect(fresh_db)
        try:
            row = con.execute(
                "SELECT value FROM app_metadata WHERE key = 'catalog_version'",
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        assert row[0] == "1"


@allure.epic("Infrastructure")
@allure.feature("Migrations")
class TestCategoryTemplateSchema:
    def test_new_columns_and_tables_exist(self, fresh_db):
        con = _connect(fresh_db)
        try:
            category_cols = _column_names(con, "categories")
            group_cols = _column_names(con, "category_groups")
            tables = _table_names(con)
        finally:
            con.close()

        assert {"code", "is_hidden", "is_retired"}.issubset(category_cols)
        assert "code" in group_cols
        assert {"category_templates", "category_translations"}.issubset(tables)

    def test_foreign_keys_intact(self, fresh_db):
        con = _connect(fresh_db)
        try:
            problems = con.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            con.close()
        assert problems == []

    def test_duplicate_category_and_group_names_allowed(self, fresh_db):
        con = _connect(fresh_db)
        try:
            con.execute(
                "INSERT INTO category_groups (name, sort_order, code) VALUES ('Group', 1, 'a')",
            )
            con.execute(
                "INSERT INTO category_groups (name, sort_order, code) VALUES ('Group', 2, 'b')",
            )
            con.execute(
                "INSERT INTO categories (name, code) VALUES ('Category', 'c1')",
            )
            con.execute(
                "INSERT INTO categories (name, code) VALUES ('Category', 'c2')",
            )
        finally:
            con.close()

    def test_category_code_is_unique(self, fresh_db):
        con = _connect(fresh_db)
        try:
            con.execute("INSERT INTO categories (name, code) VALUES ('A', 'dup')")
            with pytest.raises(sqlite3.IntegrityError):
                con.execute("INSERT INTO categories (name, code) VALUES ('B', 'dup')")
        finally:
            con.close()


@allure.epic("Infrastructure")
@allure.feature("Migrations")
class TestInitDbIntegration:
    def test_init_db_creates_file_and_connects(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "dinary.db")

        assert not storage.DB_PATH.exists()
        storage.init_db()
        assert storage.DB_PATH.exists()

        con = storage.get_connection()
        try:
            version = get_catalog_version(con)
        finally:
            con.close()
        assert version == 1


@allure.epic("Infrastructure")
@allure.feature("Migrations")
class TestAccountingCurrencyAnchor:
    """See ``specs/reference/currencies.md`` "Accounting currency source of truth"."""

    def _point_repo_at_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "dinary.db")

    def test_fresh_db_persists_anchor_uppercased(self, tmp_path, monkeypatch):
        """Both the stored value and ``settings.accounting_currency`` must be
        uppercased so downstream callers can trust the normalised form."""
        self._point_repo_at_tmp(tmp_path, monkeypatch)
        monkeypatch.setattr(settings, "accounting_currency", "eur")

        storage.init_db()

        con = _connect(storage.DB_PATH)
        try:
            row = con.execute(
                "SELECT value FROM app_metadata WHERE key = 'accounting_currency'",
            ).fetchone()
        finally:
            con.close()
        assert row is not None
        assert row[0] == "EUR"
        assert settings.accounting_currency == "EUR"

    def test_matching_anchor_is_noop(self, tmp_path, monkeypatch):
        """Re-running ``init_db`` with the SAME accounting currency
        must be a clean no-op (no duplicate row, no error). This is
        the hot path every server restart / test fixture hits.
        """
        self._point_repo_at_tmp(tmp_path, monkeypatch)
        monkeypatch.setattr(settings, "accounting_currency", "EUR")

        storage.init_db()
        storage.init_db()

        con = _connect(storage.DB_PATH)
        try:
            rows = con.execute(
                "SELECT value FROM app_metadata WHERE key = 'accounting_currency'",
            ).fetchall()
        finally:
            con.close()
        assert rows == [("EUR",)]

    def test_mismatched_anchor_refuses_to_start(self, tmp_path, monkeypatch):
        """Must raise instead of silently writing rows in the wrong unit; the
        message must name both currencies so the operator can tell the drift direction."""
        self._point_repo_at_tmp(tmp_path, monkeypatch)

        monkeypatch.setattr(settings, "accounting_currency", "EUR")
        storage.init_db()

        monkeypatch.setattr(settings, "accounting_currency", "RSD")
        with pytest.raises(RuntimeError, match="accounting_currency") as excinfo:
            storage.init_db()
        assert "'EUR'" in str(excinfo.value)
        assert "'RSD'" in str(excinfo.value)

    def test_case_insensitive_match(self, tmp_path, monkeypatch):
        """``EUR`` vs ``eur`` must NOT be treated as a mismatch —
        only the ISO-4217 identity matters, not the operator's
        capitalisation habits in ``.deploy/.env``.
        """
        self._point_repo_at_tmp(tmp_path, monkeypatch)

        monkeypatch.setattr(settings, "accounting_currency", "EUR")
        storage.init_db()

        monkeypatch.setattr(settings, "accounting_currency", "eur")
        storage.init_db()
        assert settings.accounting_currency == "EUR"

    def test_fresh_db_without_env_rejects(self, tmp_path, monkeypatch):
        """Fresh DB + empty ``DINARY_ACCOUNTING_CURRENCY`` has no seed
        source — we refuse to guess. The operator must pick a currency
        on the very first deploy; after that they can drop the env var.
        """
        self._point_repo_at_tmp(tmp_path, monkeypatch)
        monkeypatch.setattr(settings, "accounting_currency", "  ")

        with pytest.raises(RuntimeError, match="Fresh"):
            storage.init_db()

    def test_populated_db_without_env_reads_anchor(self, tmp_path, monkeypatch):
        """Must read the anchored value from the DB and broadcast it via
        ``settings.accounting_currency``, not fail."""
        self._point_repo_at_tmp(tmp_path, monkeypatch)

        monkeypatch.setattr(settings, "accounting_currency", "EUR")
        storage.init_db()

        monkeypatch.setattr(settings, "accounting_currency", "")
        storage.init_db()

        assert settings.accounting_currency == "EUR"
        con = _connect(storage.DB_PATH)
        try:
            row = con.execute(
                "SELECT value FROM app_metadata WHERE key = 'accounting_currency'",
            ).fetchone()
        finally:
            con.close()
        assert row[0] == "EUR"


@allure.epic("Infrastructure")
@allure.feature("Migrations")
class TestLlmbrokerUpgrade:
    """0002 drops the legacy llmbroker tables, resets PRAGMA user_version, and
    adds ``classification_rules.llm_name``.

    llmbroker 0.0.11 stamped the file-global ``user_version`` to 1; llmbroker
    1.3.0 accepts only 0 or its own schema version and otherwise raises. ``DROP
    TABLE`` cannot clear a header value, so the migration must reset it or the
    0.0.11 -> 1.3.0 upgrade crashes on the first broker call.
    """

    # Every table a pre-1.3.0 dinary could leave behind: the llmbroker 0.0.11 set
    # plus the two dinary owned before the package took over provider management.
    _LEGACY_TABLES = (
        "llmbroker_registry",
        "llmbroker_calls",
        "llmbroker_secrets",
        "llmbroker_state",
        "llmbroker_providers",
        "llmbroker_call_log",
    )

    def _seed_0_0_11_database(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "dinary.db")
        con = sqlite3.connect(str(storage.DB_PATH), isolation_level=None)
        try:
            for table in self._LEGACY_TABLES:
                con.execute(f"CREATE TABLE {table} (name TEXT)")
            con.execute("PRAGMA user_version = 1")
        finally:
            con.close()
        return storage.DB_PATH

    def test_resets_user_version_and_drops_legacy_tables(self, tmp_path, monkeypatch):
        db = self._seed_0_0_11_database(tmp_path, monkeypatch)

        db_migrations.migrate_db(db)

        con = _connect(db)
        try:
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = _table_names(con)
        finally:
            con.close()
        assert user_version == 0
        assert tables.isdisjoint(self._LEGACY_TABLES)

    def test_adds_llm_name_to_classification_rules(self, fresh_db):
        con = _connect(fresh_db)
        try:
            assert "llm_name" in _column_names(con, "classification_rules")
        finally:
            con.close()

    def test_upgraded_database_gets_llm_name(self, tmp_path, monkeypatch):
        """The column must also land on a 0.0.11-era database, not only a fresh one."""
        db = self._seed_0_0_11_database(tmp_path, monkeypatch)

        db_migrations.migrate_db(db)

        con = _connect(db)
        try:
            assert "llm_name" in _column_names(con, "classification_rules")
        finally:
            con.close()

    def test_fresh_broker_owns_every_remaining_llmbroker_table(self, tmp_path, monkeypatch):
        """No llmbroker_-prefixed table may outlive the cleanup unless the running
        llmbroker recreated it — a stale one holds dead rows (and plaintext keys)
        that nothing migrates or reads again."""
        db = self._seed_0_0_11_database(tmp_path, monkeypatch)
        db_migrations.migrate_db(db)

        preset = tmp_path / "llms.toml"
        preset.write_text("# no providers\n")

        async def _run() -> None:
            broker = llmbroker.AsyncBroker(f"sqlite://{db}", optimize=llmbroker.Optimizer())
            await broker.sync(preset)
            await broker.aclose()

        asyncio.run(_run())

        con = _connect(db)
        try:
            tables = _table_names(con)
        finally:
            con.close()
        current = {spec.name for spec in llmbroker_spec.TABLES.values()}
        assert {t for t in tables if t.startswith("llmbroker_")} <= current

    def test_broker_starts_on_upgraded_db(self, tmp_path, monkeypatch):
        """After the migration a real llmbroker broker provisions its schema on
        the upgraded DB without raising the stale-version error."""
        db = self._seed_0_0_11_database(tmp_path, monkeypatch)
        db_migrations.migrate_db(db)

        preset = tmp_path / "llms.toml"
        preset.write_text("# no providers\n")  # enough to trigger schema setup

        async def _run() -> None:
            broker = llmbroker.AsyncBroker(f"sqlite://{db}", optimize=llmbroker.Optimizer())
            # Would raise "schema version 1 found, this release expects N" if
            # 0002 had not reset user_version.
            await broker.sync(preset)
            await broker.aclose()

        asyncio.run(_run())

        con = _connect(db)
        try:
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
        # llmbroker took over the freed header and stamped its own schema version.
        assert user_version != 1
