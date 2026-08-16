"""Tests for healthcheck helpers in :mod:`tasks.healthcheck`.

Covers the pure-Python helpers that parse query results and emit
``OK:`` / ``FAIL:`` lines.  The SQL execution path (local) is tested
via a real SQLite file on ``tmp_path`` using ``monkeypatch.chdir``
— the same pattern as ``test_tasks_db.py``.
"""

import sqlite3
from unittest.mock import MagicMock

import allure
import pytest

from tasks.healthcheck import (
    _healthcheck_last_expense_info,
    _healthcheck_run_queries,
    _healthcheck_sheet_log,
    _healthcheck_sheet_queue,
)


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestHealthcheckSheetLog:
    def test_logged_to_sheet(self, capsys):
        assert _healthcheck_sheet_log({"sheet": "3889|"}) is False
        assert "logged to sheet" in capsys.readouterr().out

    def test_pending_shows_in_progress(self, capsys):
        assert _healthcheck_sheet_log({"sheet": "3889|pending"}) is False
        out = capsys.readouterr().out
        assert "3889" in out
        assert "in progress" in out

    def test_in_progress_shows_in_progress(self, capsys):
        assert _healthcheck_sheet_log({"sheet": "3889|in_progress"}) is False
        assert "in progress" in capsys.readouterr().out

    def test_poisoned_fails(self, capsys):
        assert _healthcheck_sheet_log({"sheet": "3889|poisoned"}) is True
        err = capsys.readouterr().err
        assert "3889" in err
        assert "failed" in err

    def test_unexpected_status_fails(self, capsys):
        assert _healthcheck_sheet_log({"sheet": "3889|weird_status"}) is True
        err = capsys.readouterr().err
        assert "unexpected" in err
        assert "weird_status" in err

    def test_no_expenses_in_db(self, capsys):
        assert _healthcheck_sheet_log({"sheet": ""}) is False
        assert "no expenses" in capsys.readouterr().out


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestHealthcheckSheetQueue:
    """The whole queue is checked, not only the newest expense: poisoned rows are
    terminal, so a later successful expense used to silence the alert for good."""

    def test_empty_queue_is_ok(self, capsys):
        assert _healthcheck_sheet_queue({"sheet_poisoned": "0|0|"}) is False
        assert "no poisoned" in capsys.readouterr().out

    def test_poisoned_expense_jobs_fail_with_ids(self, capsys):
        assert _healthcheck_sheet_queue({"sheet_poisoned": "37|0|4842,4781,4780"}) is True
        err = capsys.readouterr().err
        assert "37 expense job(s)" in err
        assert "4842,4781,4780" in err
        assert "inv requeue-sheet-jobs" in err

    def test_poisoned_income_jobs_fail(self, capsys):
        assert _healthcheck_sheet_queue({"sheet_poisoned": "0|2|"}) is True
        assert "2 income job(s)" in capsys.readouterr().err

    def test_missing_key_is_ok(self, capsys):
        assert _healthcheck_sheet_queue({}) is False
        assert "no poisoned" in capsys.readouterr().out


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestHealthcheckLastExpenseInfo:
    def test_shows_whole_amount_currency_category(self, capsys):
        _healthcheck_last_expense_info(
            {"last_expense": "1500.00|RSD|Groceries", "prev_day_total": ""}
        )
        assert "1500 RSD (Groceries)" in capsys.readouterr().out

    def test_shows_fractional_amount(self, capsys):
        _healthcheck_last_expense_info(
            {"last_expense": "1500.50|RSD|Groceries", "prev_day_total": ""}
        )
        assert "1500.50 RSD" in capsys.readouterr().out

    def test_shows_yesterday_total_single_currency(self, capsys):
        _healthcheck_last_expense_info({"last_expense": "", "prev_day_total": "RSD:8200.00"})
        assert "yesterday total 8200 RSD" in capsys.readouterr().out

    def test_shows_yesterday_total_multiple_currencies(self, capsys):
        _healthcheck_last_expense_info(
            {"last_expense": "", "prev_day_total": "RSD:5000.00,EUR:20.50"}
        )
        out = capsys.readouterr().out
        assert "5000 RSD" in out
        assert "20.50 EUR" in out

    def test_empty_results_prints_nothing(self, capsys):
        _healthcheck_last_expense_info({})
        assert capsys.readouterr().out == ""


@allure.epic("Infrastructure")
@allure.feature("Deploy")
class TestHealthcheckRunQueriesLocal:
    @pytest.fixture
    def _cwd(self, tmp_path, monkeypatch):
        (tmp_path / "data").mkdir()
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_returns_results_keyed_by_query_name(self, _cwd):
        db_path = _cwd / "data" / "dinary.db"
        with sqlite3.connect(db_path) as con:
            con.execute("CREATE TABLE t (v TEXT)")
            con.execute("INSERT INTO t VALUES ('hello')")

        results = _healthcheck_run_queries(
            MagicMock(),
            False,
            greeting="SELECT v FROM t LIMIT 1",
            count="SELECT count(*) FROM t",
        )

        assert results["greeting"] == "hello"
        assert results["count"] == "1"

    def test_exits_1_when_db_missing(self, _cwd, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _healthcheck_run_queries(MagicMock(), False, q="SELECT 1")
        assert excinfo.value.code == 1
        assert "No local DB" in capsys.readouterr().err
