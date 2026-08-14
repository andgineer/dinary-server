"""Tests for ``inv verify-db`` and ``inv restore-yoyo`` in :mod:`tasks.db`.

Local verify-db path uses real SQLite files on ``tmp_path``. The restore-yoyo task
is shell-only: SSH calls are stubbed so tests pin command shape and guard
conditions without touching a real server.
"""

import sqlite3
from unittest.mock import MagicMock

import allure
import pytest

import tasks
import tasks.db


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestVerifyDbLocal:
    """Uses real SQLite files on ``tmp_path`` (not mocks) so a regression that
    reorders the two PRAGMA statements or drops the output-line check is caught."""

    @staticmethod
    def _verify_db(c, *, prod: bool = False) -> None:
        return tasks.verify_db.body(c, prod=prod)

    @pytest.fixture
    def _cwd(self, tmp_path, monkeypatch):
        """``verify_db`` reads ``data/dinary.db`` relative to cwd."""
        (tmp_path / "data").mkdir()
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_passes_on_healthy_db(self, _cwd, capsys):
        db_path = _cwd / "data" / "dinary.db"

        con = sqlite3.connect(db_path)
        con.executescript(
            "PRAGMA foreign_keys=ON;"
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child ("
            "  id INTEGER PRIMARY KEY,"
            "  parent_id INTEGER NOT NULL REFERENCES parent(id)"
            ");"
            "INSERT INTO parent (id) VALUES (1);"
            "INSERT INTO child (id, parent_id) VALUES (1, 1);"
        )
        con.close()
        c = MagicMock()
        self._verify_db(c)
        out = capsys.readouterr().out
        assert "ok" in out
        assert "=== verify-db OK ===" in out

    def test_fails_on_foreign_key_violation(self, _cwd, capsys):
        """FKs are off by default at write time, letting the test create a
        deliberately orphaned row — exactly what foreign_key_check must catch."""
        db_path = _cwd / "data" / "dinary.db"

        con = sqlite3.connect(db_path)
        con.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child ("
            "  id INTEGER PRIMARY KEY,"
            "  parent_id INTEGER NOT NULL REFERENCES parent(id)"
            ");"
            # FKs are OFF by default on a fresh connection, so
            # this orphan insert succeeds even though parent_id=42
            # does not exist.
            "INSERT INTO child (id, parent_id) VALUES (1, 42);"
        )
        con.close()
        c = MagicMock()
        with pytest.raises(SystemExit) as excinfo:
            self._verify_db(c)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "=== verify-db FAILED ===" in captured.err
        # The orphaned row must be in the reported output — otherwise
        # the test is passing for the wrong reason.
        assert "child" in captured.out

    def test_fails_cleanly_when_db_is_missing(self, _cwd, capsys):
        """First-run UX: an operator who never ran ``inv dev`` or
        ``inv backup`` has no local DB. The task must exit 1 with a
        clear message, not a cryptic sqlite3 error.
        """
        # Note: _cwd already created ``data/`` but not ``dinary.db``.
        c = MagicMock()
        with pytest.raises(SystemExit) as excinfo:
            self._verify_db(c)
        assert excinfo.value.code == 1
        assert "No local DB" in capsys.readouterr().err


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestVerifyDbProd:
    """The exact emitted shell command is the contract — reordering or dropping
    either pragma would silently hide post-migration data corruption. Shell-only
    is enough here; the Python side is already covered by ``TestVerifyDbLocal``."""

    @pytest.fixture
    def _spy(self, monkeypatch):
        class Spy:
            cmd: str | None = None
            payload: bytes = b"ok\n"

        spy = Spy()

        def fake_bytes(cmd: str) -> bytes:
            spy.cmd = cmd
            return spy.payload

        monkeypatch.setattr(tasks.db, "ssh_capture_bytes", fake_bytes)
        return spy

    def test_prod_snapshots_live_db_before_pragma_checks(self, _spy):
        tasks.verify_db.body(MagicMock(), prod=True)
        cmd = _spy.cmd or ""
        # set -e so a failed backup doesn't silently run pragmas on stale /tmp residue.
        assert cmd.startswith("set -e; ")
        assert "SNAP=/tmp/dinary-verify-db-$$.db" in cmd
        assert 'sqlite3 "/home/ubuntu/dinary/data/dinary.db"' in cmd
        assert '.backup \\"$SNAP\\"' in cmd
        assert 'trap "rm -f \\"$SNAP\\"" EXIT' in cmd
        assert cmd.index("trap") < cmd.index("sqlite3")

    def test_prod_runs_both_pragma_checks_against_snapshot(self, _spy):
        tasks.verify_db.body(MagicMock(), prod=True)
        cmd = _spy.cmd or ""
        # Must target $SNAP, not the live DB path (would race WAL checkpoints).
        assert 'sqlite3 "$SNAP" "PRAGMA integrity_check; PRAGMA foreign_key_check;"' in cmd

    def test_prod_propagates_pragma_failure_as_exit_1(self, _spy, capsys):
        """When the remote snapshot reports any issue, the local side
        must still honour the ``lines == ["ok"]`` contract and exit 1
        with the pragma output visible to the operator.
        """
        _spy.payload = b"ok\nchild|1|parent|0\n"
        with pytest.raises(SystemExit) as excinfo:
            tasks.verify_db.body(MagicMock(), prod=True)
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "child|1|parent|0" in captured.out
        assert "=== verify-db FAILED ===" in captured.err

    def test_prod_reports_ok_when_snapshot_is_healthy(self, _spy, capsys):
        _spy.payload = b"ok\n"
        tasks.verify_db.body(MagicMock(), prod=True)
        out = capsys.readouterr().out
        assert "=== verify-db OK ===" in out


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestRestoreYoyo:
    """``inv restore-yoyo --to=<prefix>`` rolls back server migrations.

    SSH calls are stubbed so these tests run without a live server. The
    service-running guard must refuse to proceed and emit a clear message.
    """

    _TWO_MIGRATIONS = ["0001_initial_schema", "0002_add_something"]

    @pytest.fixture
    def _two_migrations(self, monkeypatch):
        monkeypatch.setattr(tasks.db, "migration_ids", lambda: self._TWO_MIGRATIONS)

    @pytest.fixture
    def _service_inactive(self, monkeypatch):
        monkeypatch.setattr(tasks.db, "ssh_capture", lambda c, cmd: "inactive\n")

    @pytest.fixture
    def _service_active(self, monkeypatch):
        monkeypatch.setattr(tasks.db, "ssh_capture", lambda c, cmd: "active\n")

    @pytest.fixture
    def _ssh_run_spy(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(tasks.db, "ssh_run", lambda c, cmd: calls.append(cmd))
        return calls

    def test_invalid_prefix_exits_1(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            tasks.restore_yoyo.body(MagicMock(), to="9999")
        assert excinfo.value.code == 1
        assert "9999" in capsys.readouterr().err

    def test_nothing_to_rollback_prints_message(self, capsys):
        """Rolling back --to the latest migration finds the target but has
        nothing to roll back. Must print a message and return without
        contacting the server at all.
        """
        latest = tasks.db.migration_ids()[-1]
        tasks.restore_yoyo.body(MagicMock(), to=latest)
        out = capsys.readouterr().out
        assert "nothing to roll back" in out

    def test_service_running_exits_1_without_rollback(
        self, _two_migrations, _service_active, _ssh_run_spy, capsys
    ):
        """If dinary is active the task must refuse to proceed — the operator
        must stop the service first to avoid mid-migration crashes.
        """
        with pytest.raises(SystemExit) as excinfo:
            tasks.restore_yoyo.body(MagicMock(), to="0001")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "dinary" in err
        assert "stop" in err.lower()
        assert _ssh_run_spy == []

    def test_happy_path_runs_yoyo_rollback_command(
        self, _two_migrations, _service_inactive, _ssh_run_spy
    ):
        tasks.restore_yoyo.body(MagicMock(), to="0001")
        assert len(_ssh_run_spy) == 1
        cmd = _ssh_run_spy[0]
        assert "yoyo rollback" in cmd
        assert "--batch" in cmd
        assert "-r 0002_add_something" in cmd
