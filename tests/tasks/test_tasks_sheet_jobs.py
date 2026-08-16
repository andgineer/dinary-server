"""Tests for ``inv requeue-sheet-jobs`` in :mod:`tasks.sheet_jobs`.

The local path runs against a real SQLite file on ``tmp_path``; the prod path
stubs SSH so the tests pin command shape without touching a real server.
"""

import sqlite3
from unittest.mock import MagicMock

import allure
import pytest

import tasks
import tasks.sheet_jobs
from tasks.sheet_jobs import parse_poisoned_counts

_SCHEMA = (
    "CREATE TABLE sheet_logging_jobs ("
    " expense_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',"
    " claim_token TEXT, claimed_at TIMESTAMP, last_error TEXT);"
    "CREATE TABLE income_logging_jobs ("
    " year INTEGER NOT NULL, month INTEGER NOT NULL,"
    " status TEXT NOT NULL DEFAULT 'pending', claimed_at TIMESTAMP, last_error TEXT,"
    " PRIMARY KEY (year, month));"
)


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestParsePoisonedCounts:
    def test_parses_pair(self):
        assert parse_poisoned_counts("37|2") == (37, 2)

    def test_strips_whitespace(self):
        assert parse_poisoned_counts(" 1|0 \n") == (1, 0)

    def test_missing_second_field_is_zero(self):
        assert parse_poisoned_counts("5") == (5, 0)

    def test_unreadable_output_is_zero(self):
        assert parse_poisoned_counts("sqlite3: no such table") == (0, 0)

    def test_empty_output_is_zero(self):
        assert parse_poisoned_counts("") == (0, 0)


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestRequeueSheetJobsLocal:
    @staticmethod
    def _requeue(c, *, prod: bool = False, yes: bool = True) -> None:
        return tasks.requeue_sheet_jobs.body(c, prod=prod, yes=yes)

    @pytest.fixture
    def _db(self, tmp_path, monkeypatch):
        """``open_local_db`` reads ``data/dinary.db`` relative to cwd."""
        (tmp_path / "data").mkdir()
        monkeypatch.chdir(tmp_path)
        con = sqlite3.connect(tmp_path / "data" / "dinary.db")
        con.executescript(_SCHEMA)
        con.commit()
        con.close()
        return tmp_path / "data" / "dinary.db"

    @staticmethod
    def _statuses(db_path) -> list[str]:
        con = sqlite3.connect(db_path)
        try:
            return [r[0] for r in con.execute("SELECT status FROM sheet_logging_jobs ORDER BY 1")]
        finally:
            con.close()

    def test_poisoned_rows_return_to_pending(self, _db, capsys):
        con = sqlite3.connect(_db)
        con.executescript(
            "INSERT INTO sheet_logging_jobs (expense_id, status, last_error)"
            " VALUES (4842, 'poisoned', 'APIError: [429]');"
            "INSERT INTO income_logging_jobs (year, month, status, last_error)"
            " VALUES (2026, 8, 'poisoned', 'APIError: [429]');"
        )
        con.commit()
        con.close()

        self._requeue(MagicMock())

        assert self._statuses(_db) == ["pending"]
        out = capsys.readouterr().out
        assert "1 expense job(s), 1 income job(s)" in out
        assert "Requeued 2 job(s)" in out

    def test_claim_state_and_error_are_cleared(self, _db):
        con = sqlite3.connect(_db)
        con.execute(
            "INSERT INTO sheet_logging_jobs"
            " (expense_id, status, claim_token, claimed_at, last_error)"
            " VALUES (7, 'poisoned', 'tok', '2026-08-15 12:00:00', 'boom')",
        )
        con.commit()
        con.close()

        self._requeue(MagicMock())

        con = sqlite3.connect(_db)
        try:
            row = con.execute(
                "SELECT claim_token, claimed_at, last_error FROM sheet_logging_jobs",
            ).fetchone()
        finally:
            con.close()
        assert row == (None, None, None)

    def test_in_progress_rows_are_left_alone(self, _db):
        """A row the drain is working on right now must not be yanked back to pending."""
        con = sqlite3.connect(_db)
        con.execute(
            "INSERT INTO sheet_logging_jobs (expense_id, status, claim_token)"
            " VALUES (7, 'in_progress', 'tok')",
        )
        con.commit()
        con.close()

        self._requeue(MagicMock())

        assert self._statuses(_db) == ["in_progress"]

    def test_empty_queue_reports_nothing_to_do(self, _db, capsys):
        self._requeue(MagicMock())
        assert "Nothing to requeue" in capsys.readouterr().out

    def test_declining_the_prompt_exits_1(self, _db, monkeypatch, capsys):
        con = sqlite3.connect(_db)
        con.execute("INSERT INTO sheet_logging_jobs (expense_id, status) VALUES (7, 'poisoned')")
        con.commit()
        con.close()
        monkeypatch.setattr("builtins.input", lambda _prompt: "no")

        with pytest.raises(SystemExit) as excinfo:
            self._requeue(MagicMock(), yes=False)

        assert excinfo.value.code == 1
        assert "Aborted" in capsys.readouterr().err
        assert self._statuses(_db) == ["poisoned"]


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestRequeueSheetJobsProd:
    @pytest.fixture
    def _ssh_spy(self, monkeypatch):
        calls: list[str] = []

        def fake_capture(c, cmd):  # noqa: ARG001
            calls.append(cmd)
            return "3|1" if len(calls) == 1 else ""

        monkeypatch.setattr(tasks.sheet_jobs, "ssh_capture", fake_capture)
        return calls

    def test_updates_run_against_the_live_db(self, _ssh_spy, capsys):
        tasks.requeue_sheet_jobs.body(MagicMock(), prod=True, yes=True)

        update = _ssh_spy[1]
        assert "/home/ubuntu/dinary/data/dinary.db" in update
        assert "UPDATE sheet_logging_jobs SET status = 'pending'" in update
        assert "UPDATE income_logging_jobs SET status = 'pending'" in update
        assert "WHERE status = 'poisoned'" in update
        assert "Requeued 4 job(s)" in capsys.readouterr().out

    def test_nothing_poisoned_skips_the_update(self, monkeypatch, capsys):
        calls: list[str] = []

        def fake_capture(c, cmd):  # noqa: ARG001
            calls.append(cmd)
            return "0|0"

        monkeypatch.setattr(tasks.sheet_jobs, "ssh_capture", fake_capture)

        tasks.requeue_sheet_jobs.body(MagicMock(), prod=True, yes=True)

        assert len(calls) == 1
        assert "Nothing to requeue" in capsys.readouterr().out
