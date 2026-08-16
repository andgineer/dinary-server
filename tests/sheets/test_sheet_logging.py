"""Edge-case tests for the sheet-logging queue: idempotency marker, spreadsheet-unset
short-circuit, circuit-breaker state, job-lock conflict, rate-limit knobs. Sibling
files cover derive (``test_sheet_logging_derive.py``), drain happy-path
(``test_sheet_logging_drain.py``), and single-job drain (``test_sheet_logging_drain_one.py``)."""

import json
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

import allure
import gspread
import requests

from dinary.config import settings
from dinary.db import storage
from dinary.background.sheet_logging import sheet_logging
from dinary.background.sheet_logging.logging_jobs import claim_logging_job, list_logging_jobs
from dinary.db.expenses import ExpensePayload, insert_expense

from _sheet_logging_helpers import (  # noqa: F401  (autouse + fixtures)
    _reset_backoff,
    data_dir,
    setup,
)


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestIdempotencyMarker:
    """When ``append_expense_atomic`` returns False (marker already
    present on the row), the drain must count it as ``ALREADY_LOGGED``
    and still clear the queue row."""

    @patch("dinary.background.sheet_logging.sheet_logging.get_sheet")
    @patch("dinary.background.sheet_logging.sheet_logging.get_rate", return_value="117.0")
    @patch("dinary.background.sheet_logging.sheet_logging.ensure_category_row")
    @patch(
        "dinary.background.sheet_logging.sheet_logging.append_expense_atomic", return_value=False
    )
    def test_marker_present_returns_already_logged_and_clears_queue(
        self,
        _aea,
        mock_ecr,
        _gr,
        mock_sheet,
        setup,
    ):
        ws = MagicMock()
        values = [["header"], ["row1"], ["row2"], ["row3"]]
        ws.get_all_values.return_value = values
        mock_sheet.return_value.worksheet.return_value = ws
        mock_sheet.return_value.sheet1 = ws
        mock_ecr.return_value = (3, values)

        result = sheet_logging.drain_pending()

        assert result["appended"] == 0
        assert result["already_logged"] == 1
        assert result["failed"] == 0
        assert result["recovered_with_duplicate"] == 0

        con = storage.get_connection()
        try:
            assert list_logging_jobs(con) == []
        finally:
            con.close()


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestSheetLoggingDisabled:
    """When ``DINARY_SHEET_LOGGING_SPREADSHEET`` is empty, the drain
    is a no-op that returns a bare ``{"disabled": True}``."""

    def test_drain_pending_returns_disabled(self, setup, monkeypatch):
        monkeypatch.setattr(settings, "sheet_logging_spreadsheet", "")
        result = sheet_logging.drain_pending()
        assert result == {"disabled": True}


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestCircuitBreaker:
    """Module-level backoff state means a transient failure stalls the
    next drain attempt with ``{backoff_active: True}`` instead of
    re-hammering Sheets."""

    def test_backoff_active_short_circuits_drain(self, setup):
        sheet_logging._activate_backoff()
        result = sheet_logging.drain_pending()
        assert result == {"backoff_active": True}


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestClaimLoggingJobLockConflict:
    """A ``sqlite3.OperationalError`` raised by SQLite's write-lock
    timeout when two workers race on the same row surfaces as a clean
    ``None`` return — the caller treats ``None`` as "skip this row, the
    winner will handle it"."""

    def test_lock_conflict_on_begin_returns_none(self, setup):
        expense_pk = setup
        # A real write-lock conflict (timeout=0, not a mock): the exact shape
        # two drain workers hit racing on the same queue row.
        holder = storage.get_connection()
        loser = sqlite3.connect(
            str(storage.DB_PATH),
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            timeout=0,
        )
        try:
            holder.execute("BEGIN IMMEDIATE")
            token = claim_logging_job(loser, expense_pk)
            assert token is None
            holder.execute("COMMIT")
        finally:
            loser.close()
            holder.close()


def _insert_additional_expenses(n: int, currency: str = "EUR") -> None:
    con = storage.get_connection()
    try:
        for i in range(n):
            insert_expense(
                con,
                ExpensePayload(
                    client_expense_id=f"extra-{i:03d}",
                    expense_datetime=datetime(2026, 6, 1 + i % 25, 10),
                    amount=10.0,
                    amount_original=10.0,
                    currency_original=currency,
                    category_id=1,
                    event_id=None,
                    comment="",
                    sheet_category=None,
                    sheet_group=None,
                    tag_ids=[],
                ),
                enqueue_logging=True,
            )
    finally:
        con.close()


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestDrainRateLimit:
    """Rate-limiting and inter-row sleep on ``drain_pending``:
    ``max_attempts_per_iteration`` and ``inter_row_delay_sec``."""

    @patch("dinary.background.sheet_logging.sheet_logging.get_sheet")
    @patch("dinary.background.sheet_logging.sheet_logging.fetch_row_years", return_value=[])
    @patch("dinary.background.sheet_logging.sheet_logging._drain_one_job")
    def test_cap_honored(self, mock_drain_one, _fry, _gs, setup, monkeypatch):
        """Hard cap stops the sweep after ``max_attempts``."""
        monkeypatch.setattr(settings, "sheet_logging_drain_max_attempts_per_iteration", 5)
        monkeypatch.setattr(settings, "sheet_logging_drain_inter_row_delay_sec", 0)

        _insert_additional_expenses(25)

        mock_drain_one.return_value = sheet_logging.DrainResult.APPENDED
        summary = sheet_logging.drain_pending()

        assert mock_drain_one.call_count == 5
        assert summary["cap_reached"] is True
        assert summary["attempted"] == 5

    @patch("dinary.background.sheet_logging.sheet_logging.get_sheet")
    @patch("dinary.background.sheet_logging.sheet_logging.fetch_row_years", return_value=[])
    @patch("dinary.background.sheet_logging.sheet_logging._drain_one_job")
    def test_inter_row_sleep_observed(self, mock_drain_one, _fry, _gs, setup, monkeypatch):
        """Sleep is called between attempts (before each except the first)."""
        monkeypatch.setattr(settings, "sheet_logging_drain_max_attempts_per_iteration", 10)
        monkeypatch.setattr(settings, "sheet_logging_drain_inter_row_delay_sec", 0.001)

        _insert_additional_expenses(3)

        mock_drain_one.return_value = sheet_logging.DrainResult.APPENDED
        sleep_mock = MagicMock()
        monkeypatch.setattr(sheet_logging.time, "sleep", sleep_mock)

        sheet_logging.drain_pending()

        # 1 expense from setup + 3 new = 4 total attempts; sleep before
        # 2nd, 3rd, 4th.
        assert sleep_mock.call_count == 3
        for call in sleep_mock.call_args_list:
            assert call.args[0] == 0.001


def _api_error(status_code: int, body: dict) -> gspread.exceptions.APIError:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(body).encode()
    return gspread.exceptions.APIError(response)


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestTransientClassification:
    """A rate limit is a 4xx that self-heals, so it must reach the circuit breaker
    instead of poisoning the row — poisoned is terminal and needs a manual requeue."""

    def test_rate_limit_429_is_transient(self):
        exc = _api_error(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "Quota exceeded for quota metric 'Read requests'",
                    "status": "RESOURCE_EXHAUSTED",
                },
            },
        )
        assert sheet_logging._is_transient(exc) is True

    def test_per_user_rate_limit_403_is_transient(self):
        exc = _api_error(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "User Rate Limit Exceeded",
                    "status": "PERMISSION_DENIED",
                    "errors": [{"reason": "userRateLimitExceeded"}],
                },
            },
        )
        assert sheet_logging._is_transient(exc) is True

    def test_permission_denied_403_stays_permanent(self):
        exc = _api_error(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "The caller does not have permission",
                    "status": "PERMISSION_DENIED",
                },
            },
        )
        assert sheet_logging._is_transient(exc) is False

    def test_not_found_404_stays_permanent(self):
        exc = _api_error(404, {"error": {"code": 404, "message": "Requested entity was not found"}})
        assert sheet_logging._is_transient(exc) is False

    def test_server_error_500_is_transient(self):
        exc = _api_error(500, {"error": {"code": 500, "message": "Internal error"}})
        assert sheet_logging._is_transient(exc) is True

    def test_connection_error_is_transient(self):
        assert sheet_logging._is_transient(ConnectionError("reset by peer")) is True

    def test_plain_exception_is_permanent(self):
        assert sheet_logging._is_transient(ValueError("bad category")) is False


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
class TestSweepReadsGridOnce:
    """Read quota is the drain's binding constraint: a per-row grid re-read put a
    multi-item receipt over the Sheets per-minute limit."""

    @patch("dinary.background.sheet_logging.sheet_logging.get_rate", return_value="117.0")
    @patch("dinary.background.sheet_logging.sheet_logging.ensure_category_row")
    @patch("dinary.background.sheet_logging.sheet_logging.append_expense_atomic", return_value=True)
    @patch("dinary.background.sheet_logging.sheet_logging.fetch_row_years", return_value=[None])
    @patch("dinary.background.sheet_logging.sheet_logging.get_sheet")
    def test_grid_is_fetched_once_per_sweep(
        self,
        mock_sheet,
        mock_years,
        _aea,
        mock_ecr,
        _gr,
        setup,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "sheet_logging_drain_inter_row_delay_sec", 0)
        ws = MagicMock()
        values = [["header"], ["row1"], ["row2"], ["row3"]]
        ws.get_all_values.return_value = values
        mock_sheet.return_value.sheet1 = ws
        mock_ecr.return_value = (3, values)

        _insert_additional_expenses(5, currency="RSD")
        summary = sheet_logging.drain_pending()

        assert summary["appended"] == 6
        assert mock_sheet.call_count == 1
        assert ws.get_all_values.call_count == 1
        assert mock_years.call_count == 1

    @patch("dinary.background.sheet_logging.sheet_logging.get_sheet")
    def test_rate_limited_grid_read_backs_off_without_poisoning(self, mock_sheet, setup):
        mock_sheet.side_effect = _api_error(
            429,
            {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
        )

        summary = sheet_logging.drain_pending()

        assert summary["failed"] == 1
        assert summary["poisoned"] == 0
        assert sheet_logging._backoff_until is not None
        con = storage.get_connection()
        try:
            assert list_logging_jobs(con) == [setup]
        finally:
            con.close()
