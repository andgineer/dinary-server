"""Income sheet logging: drain income_logging_jobs to the 'Income' worksheet.

Writes one row per calendar month to a worksheet named "Income" in the
same logging spreadsheet.  Column layout:
  A = First day of month (YYYY-MM-DD, USER_ENTERED date)
  B = App-currency amount
  C = EUR approximation formula =IF(D{r}="","",B{r}/D{r})
  D = Manual rate (set-if-missing)
  E = Month number 1-12 (literal)
  F = Idempotency marker "{year}-{month}"
"""

import contextlib
import logging
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import gspread
from gspread.utils import ValueInputOption

from dinary.adapters.rates.service import get_rate
from dinary.adapters.sheets_client import get_sheet
from dinary.background.sheet_logging.sheet_logging import (
    _is_transient,
    get_logging_spreadsheet_id,
)
from dinary.config import settings
from dinary.db import storage
from dinary.db.income import get_income_total_for_month
from dinary.db.storage import best_effort_rollback
from dinary.sheets.sheets import fetch_row_years

logger = logging.getLogger(__name__)

_income_backoff_until: datetime | None = None
_income_current_backoff_sec: float = 0.0
_BACKOFF_INITIAL_SEC = 60.0
_BACKOFF_MAX_SEC = 1800.0

_INC_COL_DATE = 1
_INC_COL_AMOUNT = 2
_INC_COL_RATE = 4
_INC_COL_MONTH = 5
_INC_COL_MARKER = 6

_INCOME_WS_TITLE = "Income"
_INCOME_HEADER_ROWS = 1


# ---------------------------------------------------------------------------
# Backoff helpers (independent from the expense drain's backoff state)
# ---------------------------------------------------------------------------


def _activate_backoff() -> None:
    global _income_backoff_until, _income_current_backoff_sec  # noqa: PLW0603
    if _income_current_backoff_sec == 0:
        _income_current_backoff_sec = _BACKOFF_INITIAL_SEC
    else:
        _income_current_backoff_sec = min(_income_current_backoff_sec * 2, _BACKOFF_MAX_SEC)
    _income_backoff_until = datetime.now() + timedelta(seconds=_income_current_backoff_sec)
    logger.warning("Income drain circuit breaker: backoff for %.0fs", _income_current_backoff_sec)


def _reset_backoff() -> None:
    global _income_backoff_until, _income_current_backoff_sec  # noqa: PLW0603
    _income_backoff_until = None
    _income_current_backoff_sec = 0.0


# ---------------------------------------------------------------------------
# Income logging jobs queue helpers
# ---------------------------------------------------------------------------


def _list_income_jobs(con: sqlite3.Connection) -> list[tuple[int, int]]:
    rows = con.execute(
        "SELECT year, month FROM income_logging_jobs WHERE status = 'pending'"
        " ORDER BY year DESC, month DESC",
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _claim_income_job(con: sqlite3.Connection, year: int, month: int) -> bool:
    now = datetime.now()
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return False
    try:
        row = con.execute(
            "SELECT status FROM income_logging_jobs WHERE year = ? AND month = ?",
            [year, month],
        ).fetchone()
        if row is None or row[0] != "pending":
            con.execute("COMMIT")
            return False
        con.execute(
            "UPDATE income_logging_jobs SET status = 'in_progress', claimed_at = ?"
            " WHERE year = ? AND month = ?",
            [now, year, month],
        )
        con.execute("COMMIT")
        return True
    except Exception:
        best_effort_rollback(con, context=f"_claim_income_job({year}, {month}) generic error")
        raise


def _clear_income_job(con: sqlite3.Connection, year: int, month: int) -> None:
    con.execute(
        "DELETE FROM income_logging_jobs WHERE year = ? AND month = ?",
        [year, month],
    )


def _poison_income_job(con: sqlite3.Connection, year: int, month: int, error: str) -> None:
    con.execute(
        "UPDATE income_logging_jobs SET status = 'poisoned', last_error = ?"
        " WHERE year = ? AND month = ?",
        [error, year, month],
    )


# ---------------------------------------------------------------------------
# Income worksheet helpers
# ---------------------------------------------------------------------------


def _get_or_create_income_worksheet(ss: gspread.Spreadsheet) -> gspread.Worksheet:
    for ws in ss.worksheets():
        if ws.title == _INCOME_WS_TITLE:
            return ws
    ws = ss.add_worksheet(title=_INCOME_WS_TITLE, rows=200, cols=10)
    ws.batch_update(
        [{"range": "A1:F1", "values": [["Date", "Amount", "EUR", "Rate", "Month", "Key"]]}],
        value_input_option=ValueInputOption.user_entered,
    )
    return ws


def _find_income_row(
    all_values: list[list[str]],
    years_by_row: list[int | None],
    target_year: int,
    target_month: int,
) -> int | None:
    for i, row in enumerate(all_values[_INCOME_HEADER_ROWS:], start=_INCOME_HEADER_ROWS + 1):
        col_month = row[_INC_COL_MONTH - 1].strip() if len(row) >= _INC_COL_MONTH else ""
        if col_month != str(target_month):
            continue
        row_year = years_by_row[i - 1] if i - 1 < len(years_by_row) else None
        if row_year is None or row_year == target_year:
            return i
    return None


def _get_rate_str(con: sqlite3.Connection, for_date: date) -> str | None:
    try:
        accounting = settings.accounting_currency.upper()
        app = settings.app_currency.upper()
        if accounting == app:
            return None
        rate = get_rate(con, for_date, accounting, app)
        return str(rate)
    except (ValueError, OSError):
        return None


def _derive_app_amount(total: Decimal, rate_str: str | None) -> float:
    accounting = settings.accounting_currency.upper()
    app = settings.app_currency.upper()
    if accounting == app:
        return float(total)
    if rate_str is not None:
        rate = Decimal(rate_str)
        return float((total * rate).quantize(Decimal("0.01")))
    return float(total)


def _write_row_to_worksheet(
    ws: gspread.Worksheet,
    all_values: list[list[str]],
    years_by_row: list[int | None],
    year: int,
    month: int,
    app_amount: float,
    marker: str,
    rate_str: str | None,
) -> str:
    """Find target row and write income. Returns 'appended', 'updated', or 'skipped'."""
    target_row = _find_income_row(all_values, years_by_row, year, month)
    if target_row is not None:
        row_data = all_values[target_row - 1]
        existing_marker = (
            row_data[_INC_COL_MARKER - 1].strip() if len(row_data) >= _INC_COL_MARKER else ""
        )
        existing_b = (
            row_data[_INC_COL_AMOUNT - 1].strip() if len(row_data) >= _INC_COL_AMOUNT else ""
        )
        try:
            amounts_match = abs(float(existing_b) - app_amount) < 0.005 if existing_b else False
        except ValueError:
            amounts_match = False
        if existing_marker == marker and amounts_match:
            return "skipped"
        r = target_row
        existing_rate = (
            row_data[_INC_COL_RATE - 1].strip() if len(row_data) >= _INC_COL_RATE else ""
        )
        updates: list[dict[str, Any]] = [
            {"range": f"B{r}", "values": [[app_amount]]},
            {"range": f"F{r}", "values": [[marker]]},
        ]
        if not existing_rate and rate_str:
            updates.append({"range": f"D{r}", "values": [[rate_str]]})
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
        return "updated"

    r = len(all_values) + 1
    date_str = date(year, month, 1).strftime("%Y-%m-%d")
    ws.append_row(
        [date_str, app_amount, f'=IF(D{r}="","",B{r}/D{r})', rate_str or "", month, marker],
        value_input_option=ValueInputOption.user_entered,
    )
    return "appended"


def _drain_one(year: int, month: int, *, spreadsheet_id: str) -> str:
    """Drain one income job. Returns 'appended', 'updated', 'skipped', or 'orphan'."""
    with storage.connection() as con:
        if not _claim_income_job(con, year, month):
            return "skipped"

    try:
        with storage.connection() as con:
            total = get_income_total_for_month(con, year, month)
            if total is None:
                logger.warning("Income job (%d, %d) has no income rows; clearing", year, month)
                _clear_income_job(con, year, month)
                return "orphan"
            rate_str = _get_rate_str(con, date(year, month, 1))

        app_amount = _derive_app_amount(total, rate_str)
        marker = f"{year}-{month}"
        ss = get_sheet(spreadsheet_id)
        ws = _get_or_create_income_worksheet(ss)
        all_values = ws.get_all_values()
        years_by_row = fetch_row_years(ws, len(all_values))
        result = _write_row_to_worksheet(
            ws,
            all_values,
            years_by_row,
            year,
            month,
            app_amount,
            marker,
            rate_str,
        )

        if result == "skipped":
            logger.info("Income (%d, %d) already logged; skipping", year, month)
        else:
            logger.info("Income (%d, %d) %s to sheet", year, month, result)
        with storage.connection() as con:
            _clear_income_job(con, year, month)
        return result

    except Exception:
        with storage.connection() as con, contextlib.suppress(Exception):
            con.execute(
                "UPDATE income_logging_jobs SET status = 'pending'"
                " WHERE year = ? AND month = ? AND status = 'in_progress'",
                [year, month],
            )
        raise


# ---------------------------------------------------------------------------
# Public drain entry point
# ---------------------------------------------------------------------------


def drain_income_pending() -> dict:
    """Drain income_logging_jobs; returns a summary dict."""
    spreadsheet_id = get_logging_spreadsheet_id()
    if spreadsheet_id is None:
        return {"disabled": True}

    now = datetime.now()
    if _income_backoff_until is not None and now < _income_backoff_until:
        return {"backoff_active": True}

    summary: dict = {
        "attempted": 0,
        "appended": 0,
        "updated": 0,
        "skipped": 0,
        "orphan": 0,
        "failed": 0,
        "poisoned": 0,
    }

    with storage.connection() as con:
        jobs = _list_income_jobs(con)

    if not jobs:
        _reset_backoff()
        return summary

    for year, month in jobs:
        summary["attempted"] += 1
        try:
            outcome = _drain_one(year, month, spreadsheet_id=spreadsheet_id)
            summary[outcome] = summary.get(outcome, 0) + 1
        except Exception as exc:
            logger.exception("Error draining income (%d, %d)", year, month)
            if _is_transient(exc):
                _activate_backoff()
                summary["failed"] += 1
                return summary
            with storage.connection() as con:
                _poison_income_job(con, year, month, f"{type(exc).__name__}: {exc}")
            summary["poisoned"] += 1

    _reset_backoff()
    return summary
