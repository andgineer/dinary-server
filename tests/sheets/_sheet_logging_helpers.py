"""Underscore prefix keeps pytest from collecting this as a test module."""

import shutil
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from dinary.config import settings
from dinary.db import storage
from dinary.background.sheet_logging import sheet_logging
from dinary.db.expenses import ExpensePayload, ExpenseRow, insert_expense


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "dinary.db")
    monkeypatch.setattr(settings, "sheet_logging_spreadsheet", "test-spreadsheet-id")


@pytest.fixture(autouse=True)
def _reset_backoff():
    # Circuit breaker state is module-level; clear it between tests so
    # a prior "transient error" test doesn't stall the next drain with
    # ``{backoff_active: True}``.
    sheet_logging._reset_backoff()
    yield
    sheet_logging._reset_backoff()


@pytest.fixture
def setup(tmp_path, blank_db) -> int:
    shutil.copy(blank_db, tmp_path / "dinary.db")
    con = storage.get_connection()
    try:
        con.execute(
            "INSERT INTO category_groups (id, name, sort_order, is_active)"
            " VALUES (1, 'g', 1, TRUE)",
        )
        con.execute(
            "INSERT INTO categories (id, name, group_id, is_active) VALUES (1, 'food', 1, TRUE)",
        )
        con.execute(
            "INSERT INTO sheet_mapping (row_order, category_id, event_id,"
            " sheet_category, sheet_group) VALUES (1, 1, NULL, 'Food', 'Essentials')",
        )
    finally:
        con.close()

    con = storage.get_connection()
    try:
        insert_expense(
            con,
            ExpensePayload(
                client_expense_id="exp1-client-key",
                expense_datetime=datetime(2026, 4, 14, 10),
                amount=12.0,
                amount_original=1500.0,
                currency_original="RSD",
                category_id=1,
                event_id=None,
                comment="lunch",
                sheet_category=None,
                sheet_group=None,
                tag_ids=[],
            ),
            enqueue_logging=True,
        )
        pk_row = con.execute(
            "SELECT id FROM expenses WHERE client_expense_id = 'exp1-client-key'",
        ).fetchone()
    finally:
        con.close()
    assert pk_row is not None
    return int(pk_row[0])


@pytest.fixture
def sheet_view() -> sheet_logging._SheetView:
    """Stands in for the grid snapshot ``drain_pending`` reads once per sweep."""
    values = [["header"], ["row1"], ["row2"], ["row3"]]
    ws = MagicMock()
    ws.get_all_values.return_value = values
    return sheet_logging._SheetView(
        ws=ws,
        all_values=values,
        years_by_row=[None] * len(values),
    )


def _expense_row(
    *,
    amount: Decimal,
    amount_original: Decimal,
    currency_original: str,
) -> ExpenseRow:
    return ExpenseRow(
        id=1,
        client_expense_id="x",
        datetime=datetime(2026, 4, 14, 10),
        amount=amount,
        amount_original=amount_original,
        currency_original=currency_original,
        category_id=1,
        event_id=None,
        comment=None,
        sheet_category=None,
        sheet_group=None,
    )


__all__ = ["_expense_row", "_reset_backoff", "data_dir", "setup", "sheet_view"]
