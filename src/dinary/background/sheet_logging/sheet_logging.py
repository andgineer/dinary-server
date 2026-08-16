"""Optional sheet logging: append expense rows to Google Sheets.

Enabled when ``DINARY_SHEET_LOGGING_SPREADSHEET`` is set. Disabled
(silent no-op) when empty. Single writer: periodic ``drain_pending``.

Circuit breaker: transient Sheets errors halt the sweep with
exponential backoff (60s -> 30min cap). Permanent errors mark individual
queue rows as ``poisoned`` and continue.
"""

import asyncio
import dataclasses
import enum
import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import gspread

from dinary.adapters.rates.service import get_rate
from dinary.adapters.sheets_client import get_sheet
from dinary.background.sheet_logging.logging_jobs import (
    claim_logging_job,
    clear_logging_job,
    force_clear_logging_job,
    list_logging_jobs,
    poison_logging_job,
    release_logging_claim,
)
from dinary.background.sheet_logging.sheets_write import append_expense_atomic, ensure_category_row
from dinary.config import settings, spreadsheet_id_from_setting
from dinary.db import storage
from dinary.db.catalog import logging_projection
from dinary.db.expenses import ExpenseRow, get_expense_by_id, get_expense_tags
from dinary.sheets import sheet_mapping
from dinary.sheets.sheets import fetch_row_years

logger = logging.getLogger(__name__)

_backoff_until: datetime | None = None
_current_backoff_sec: float = 0.0
_BACKOFF_INITIAL_SEC = 60.0
_BACKOFF_MAX_SEC = 1800.0

_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_CLIENT_ERROR_MAX = 499
_HTTP_TOO_MANY_REQUESTS = 429
_QUOTA_STATUS = "RESOURCE_EXHAUSTED"
_QUOTA_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})

# Wake-up channel: the lifespan drain loop registers an asyncio.Event
# and its loop reference at startup; producers (e.g. POST /api/expenses)
# call `notify_new_work` after committing a fresh ledger row so the
# drain runs immediately instead of waiting for the next periodic tick.
# The periodic timer stays as the canonical fallback for process
# restarts and for crash-recovery of claims left by a previous worker.
_wake_event: asyncio.Event | None = None
_wake_loop: asyncio.AbstractEventLoop | None = None


def get_logging_spreadsheet_id() -> str | None:
    return spreadsheet_id_from_setting(settings.sheet_logging_spreadsheet)


def register_wake_channel(
    event: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """On shutdown the drain loop calls `clear_wake_channel` so stale
    `notify_new_work` calls from a parallel test can't touch a closed loop."""
    global _wake_event, _wake_loop  # noqa: PLW0603
    _wake_event = event
    _wake_loop = loop


def clear_wake_channel() -> None:
    """Detach the wake-up channel (lifespan shutdown / tests teardown)."""
    global _wake_event, _wake_loop  # noqa: PLW0603
    _wake_event = None
    _wake_loop = None


def notify_new_work() -> None:
    """Thread-safe: callable from the event loop, a `to_thread` worker, or sync
    context. Silent no-op if no drain loop registered — the periodic timer remains
    the canonical wakeup source, so a missed notify never loses work."""
    ev = _wake_event
    loop = _wake_loop
    if ev is None or loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        # Loop finished between the `is_closed` check and the schedule
        # call. Dropping the notify is safe: the next lifespan startup
        # will sweep any enqueued jobs on its first iteration.
        return


class DrainResult(enum.Enum):
    APPENDED = "appended"
    ALREADY_LOGGED = "already_logged"
    FAILED = "failed"
    RECOVERED_WITH_DUPLICATE = "recovered_with_duplicate"
    NOOP_ORPHAN = "noop_orphan"
    POISONED = "poisoned"


def _api_error_code(exc: gspread.exceptions.APIError) -> int:
    code = getattr(exc, "code", None) or getattr(
        getattr(exc, "response", None),
        "status_code",
        500,
    )
    try:
        return int(code)
    except (TypeError, ValueError):
        return 500


def _is_quota_error(exc: gspread.exceptions.APIError) -> bool:
    """Sheets answers a rate limit with 429/``RESOURCE_EXHAUSTED``, but per-user limits
    still arrive as a plain 403 carrying only ``reason: userRateLimitExceeded``."""
    error = getattr(exc, "error", None)
    if not isinstance(error, dict):
        return False
    if error.get("status") == _QUOTA_STATUS:
        return True
    details = error.get("errors")
    if not isinstance(details, list):
        return False
    return any(isinstance(d, dict) and d.get("reason") in _QUOTA_REASONS for d in details)


def _is_transient(exc: Exception) -> bool:
    """Return True for errors that should trigger the circuit breaker backoff."""
    if isinstance(exc, gspread.exceptions.APIError):
        code = _api_error_code(exc)
        if code == _HTTP_TOO_MANY_REQUESTS or _is_quota_error(exc):
            return True
        return not (_HTTP_CLIENT_ERROR_MIN <= code <= _HTTP_CLIENT_ERROR_MAX)
    return isinstance(exc, ConnectionError | TimeoutError | OSError)


def _activate_backoff() -> None:
    global _backoff_until, _current_backoff_sec  # noqa: PLW0603
    if _current_backoff_sec == 0:
        _current_backoff_sec = _BACKOFF_INITIAL_SEC
    else:
        _current_backoff_sec = min(_current_backoff_sec * 2, _BACKOFF_MAX_SEC)
    _backoff_until = datetime.now() + timedelta(seconds=_current_backoff_sec)
    logger.warning("Circuit breaker: backoff for %.0fs", _current_backoff_sec)


def _reset_backoff() -> None:
    global _backoff_until, _current_backoff_sec  # noqa: PLW0603
    _backoff_until = None
    _current_backoff_sec = 0.0


# ---------------------------------------------------------------------------
# Single-job drain
# ---------------------------------------------------------------------------


def _derive_app_currency_amount_for_sheet(
    con,
    expense: ExpenseRow,
    app_currency_rate: Decimal | None,
    expense_date: date,
) -> float | None:
    """Uses ``amount_original`` verbatim when typed in app_currency (bit-identical
    to what the user saw), otherwise converts via the pre-fetched rate or an
    on-demand lookup. Returns None if a needed rate is unavailable, so the caller
    requeues for the next sweep."""
    app_currency = settings.app_currency.upper()
    currency_original = (expense.currency_original or "").upper()
    if currency_original == app_currency:
        return float(expense.amount_original)

    accounting_currency = settings.accounting_currency.upper()
    if accounting_currency == app_currency:
        return float(expense.amount)

    if app_currency_rate is not None:
        return float((expense.amount * app_currency_rate).quantize(Decimal("0.01")))

    try:
        rate = get_rate(con, expense_date, accounting_currency, app_currency)
    except (ValueError, OSError):
        return None
    return float((expense.amount * rate).quantize(Decimal("0.01")))


@dataclasses.dataclass(slots=True)
class _SheetView:
    """Grid snapshot shared by every row of one sweep. The drain is the sheet's only
    writer, so the snapshot only goes stale on a manual edit mid-sweep — a window the
    per-row re-read never closed either, since it re-read before the same append."""

    ws: gspread.Worksheet
    all_values: list[list[str]]
    years_by_row: list[int | None]


def _open_sheet_view(spreadsheet_id: str) -> _SheetView:
    ws = get_sheet(spreadsheet_id).sheet1
    all_values = ws.get_all_values()
    return _SheetView(
        ws=ws,
        all_values=all_values,
        years_by_row=fetch_row_years(ws, len(all_values)),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _ResolvedJob:
    expense: ExpenseRow
    claim_token: str
    marker_key: str
    sheet_category: str
    sheet_group: str


def _release_claim_best_effort(con, expense_pk: int, claim_token: str) -> None:
    try:
        release_logging_claim(con, expense_pk, claim_token)
    except Exception:
        logger.exception("Failed to release claim for pk=%d", expense_pk)


def _claim_and_resolve(
    con,
    expense_pk: int,
) -> _ResolvedJob | DrainResult:
    """Claim the job and resolve the sheet target; handles orphan/poison internally."""
    claim_token = claim_logging_job(con, expense_pk)
    if claim_token is None:
        return DrainResult.FAILED

    try:
        expense = get_expense_by_id(con, expense_pk)
        if expense is None:
            logger.warning("Queue row for missing expense pk=%d; clearing", expense_pk)
            clear_logging_job(con, expense_pk, claim_token)
            return DrainResult.NOOP_ORPHAN

        if expense.client_expense_id is None:
            logger.error(
                "Queue row for pk=%d has no client_expense_id; "
                "poisoning (runtime rows must carry a UUID)",
                expense_pk,
            )
            poison_logging_job(
                con,
                expense_pk,
                f"Runtime expense pk={expense_pk} has no client_expense_id",
            )
            return DrainResult.POISONED

        tag_ids = get_expense_tags(con, expense_pk)
        projection = logging_projection(
            con,
            category_id=expense.category_id,
            event_id=expense.event_id,
            tag_ids=tag_ids,
        )
        if projection is None:
            logger.error(
                "Logging projection: unknown category_id=%d for expense pk=%d",
                expense.category_id,
                expense_pk,
            )
            poison_logging_job(
                con,
                expense_pk,
                f"No sheet_mapping fallback possible for category_id={expense.category_id}",
            )
            return DrainResult.POISONED

        sheet_category, sheet_group = projection
        return _ResolvedJob(
            expense=expense,
            claim_token=claim_token,
            marker_key=expense.client_expense_id,
            sheet_category=sheet_category,
            sheet_group=sheet_group,
        )
    except Exception:
        _release_claim_best_effort(con, expense_pk, claim_token)
        raise


def _drain_one_job(
    expense_pk: int,
    *,
    view: _SheetView,
) -> DrainResult:
    """Atomically claim, append, and clear one queue row."""
    with storage.connection() as con:
        resolved = _claim_and_resolve(con, expense_pk)
        if isinstance(resolved, DrainResult):
            return resolved

        expense = resolved.expense
        expense_date = expense.datetime.date()
        accounting_currency = settings.accounting_currency.upper()
        app_currency = settings.app_currency.upper()
        rate: Decimal | None = None
        rate_str: str | None = None
        try:
            rate = get_rate(con, expense_date, accounting_currency, app_currency)
            rate_str = str(rate)
        except (ValueError, OSError):
            pass

        amount_app = _derive_app_currency_amount_for_sheet(con, expense, rate, expense_date)
        if amount_app is None:
            logger.warning(
                "Skip sheet append for pk=%d: no rate on %s to convert %s %s to %s;"
                " will retry on next sweep",
                expense_pk,
                expense_date,
                expense.amount,
                accounting_currency,
                app_currency,
            )
            _release_claim_best_effort(con, expense_pk, resolved.claim_token)
            return DrainResult.FAILED

        try:
            wrote_new_row = _append_row_to_sheet(
                view,
                _ExpenseSheetRow(
                    expense_pk=expense_pk,
                    marker_key=resolved.marker_key,
                    month=expense.datetime.month,
                    sheet_category=resolved.sheet_category,
                    sheet_group=resolved.sheet_group,
                    amount=amount_app,
                    comment=expense.comment or "",
                    expense_date=expense_date,
                    rate=rate_str,
                ),
            )
            cleared = clear_logging_job(con, expense_pk, resolved.claim_token)
            if not cleared:
                deleted = force_clear_logging_job(con, expense_pk)
                if deleted:
                    logger.error(
                        "Append succeeded for pk=%d but claim stolen; force-deleted",
                        expense_pk,
                    )
                return DrainResult.RECOVERED_WITH_DUPLICATE
            return DrainResult.APPENDED if wrote_new_row else DrainResult.ALREADY_LOGGED
        except Exception:
            logger.exception("Append to sheet failed for expense pk=%d", expense_pk)
            _release_claim_best_effort(con, expense_pk, resolved.claim_token)
            raise


@dataclasses.dataclass(frozen=True, slots=True)
class _ExpenseSheetRow:
    expense_pk: int
    marker_key: str
    month: int
    sheet_category: str
    sheet_group: str
    amount: float
    comment: str
    expense_date: date
    rate: str | None


def _append_row_to_sheet(
    view: _SheetView,
    expense_row: "_ExpenseSheetRow",
) -> bool:
    """``marker_key`` is written verbatim into column J (last-key-only
    idempotency, see ``specs/reference/sheets.md``)."""
    ws = view.ws
    years_by_row = view.years_by_row
    target_year = expense_row.expense_date.year

    sheet_row, all_values = ensure_category_row(
        ws,
        view.all_values,
        expense_row.month,
        expense_row.sheet_category,
        expense_row.sheet_group,
        expense_row.expense_date,
        years_by_row=years_by_row,
        rate=expense_row.rate,
    )

    if len(all_values) > len(years_by_row):
        years_by_row = years_by_row[: sheet_row - 1] + [target_year] + years_by_row[sheet_row - 1 :]
    view.all_values = all_values
    view.years_by_row = years_by_row

    written = append_expense_atomic(
        ws,
        sheet_row,
        marker_key=expense_row.marker_key,
        amount_app=expense_row.amount,
        comment=expense_row.comment,
        rate=expense_row.rate,
    )

    if written:
        logger.info(
            "Appended +%s for %s/%s in %d-%02d (pk=%d)",
            expense_row.amount,
            expense_row.sheet_category,
            expense_row.sheet_group,
            expense_row.expense_date.year,
            expense_row.month,
            expense_row.expense_pk,
        )
    else:
        logger.info(
            "Skipped duplicate for %s/%s in %d-%02d (pk=%d, marker present)",
            expense_row.sheet_category,
            expense_row.sheet_group,
            expense_row.expense_date.year,
            expense_row.month,
            expense_row.expense_pk,
        )
    return written


# ---------------------------------------------------------------------------
# Periodic drain
# ---------------------------------------------------------------------------


def _poison_failing_job(expense_pk: int, exc: Exception) -> None:
    with storage.connection() as con:
        poison_logging_job(con, expense_pk, f"{type(exc).__name__}: {exc}")


def _update_drain_summary(summary: dict, outcome: DrainResult) -> None:
    if outcome is DrainResult.APPENDED:
        summary["appended"] += 1
    elif outcome is DrainResult.ALREADY_LOGGED:
        summary["already_logged"] += 1
    elif outcome is DrainResult.RECOVERED_WITH_DUPLICATE:
        summary["recovered_with_duplicate"] += 1
    elif outcome is DrainResult.NOOP_ORPHAN:
        summary["noop_orphan"] += 1
    elif outcome is DrainResult.POISONED:
        summary["poisoned"] += 1
    else:
        summary["failed"] += 1


def _new_drain_summary() -> dict:
    return {
        "attempted": 0,
        "appended": 0,
        "already_logged": 0,
        "failed": 0,
        "recovered_with_duplicate": 0,
        "noop_orphan": 0,
        "poisoned": 0,
        "cap_reached": False,
    }


def _sweep_jobs(expense_pks: list[int], view: _SheetView, summary: dict) -> bool:
    """Drain each queue row in turn. Returns True when the sweep halted on a transient
    failure, having already armed the backoff — the caller must not reset it."""
    attempts = 0
    max_attempts = settings.sheet_logging_drain_max_attempts_per_iteration
    delay = settings.sheet_logging_drain_inter_row_delay_sec

    for expense_pk in expense_pks:
        if attempts >= max_attempts:
            summary["cap_reached"] = True
            return False
        if delay > 0 and attempts > 0:
            time.sleep(delay)
        summary["attempted"] += 1
        try:
            outcome = _drain_one_job(expense_pk, view=view)
        except Exception as exc:
            logger.exception("Error draining expense pk=%d", expense_pk)
            if _is_transient(exc):
                _activate_backoff()
                summary["failed"] += 1
                return True
            _poison_failing_job(expense_pk, exc)
            summary["poisoned"] += 1
            attempts += 1
            continue
        _update_drain_summary(summary, outcome)
        attempts += 1
    return False


def drain_pending() -> dict:
    """Drain ``sheet_logging_jobs`` from the single ``dinary.db``."""
    spreadsheet_id = get_logging_spreadsheet_id()
    if spreadsheet_id is None:
        return {"disabled": True}

    now = datetime.now()
    if _backoff_until is not None and now < _backoff_until:
        return {"backoff_active": True}

    summary = _new_drain_summary()
    with storage.connection() as con:
        expense_pks = list_logging_jobs(con)

    if not expense_pks:
        _reset_backoff()
        return summary

    # Lazy sheet-mapping refresh: cheap modifiedTime check via Drive API;
    # only reparses the map tab when it actually changed.
    sheet_mapping.ensure_fresh()

    try:
        view = _open_sheet_view(spreadsheet_id)
    except Exception as exc:
        logger.exception("Failed to open the logging sheet; skipping sweep")
        if _is_transient(exc):
            _activate_backoff()
        summary["failed"] += 1
        return summary

    halted = _sweep_jobs(expense_pks, view, summary)
    if not halted and not summary["cap_reached"]:
        _reset_backoff()
    return summary
