"""Tests for the receipt classification drain loop."""

import asyncio
import contextlib
import json
import shutil
import sqlite3
import time
import unittest.mock
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import allure
import pytest

import dinary.background.classification.task as drain_mod
from dinary.background.classification.receipt_classifier import (
    ClassificationResult,
    ClassifyOutcome,
)
from dinary.background.classification.persist import JOURNAL_CORRECTION_COMMENT
import llmbroker
import httpx

from dinary.adapters.receipts.types import (
    ParsedReceipt,
    ParserNotIndexedError,
    ParserParseError,
    ParserRequestError,
    ReceiptItem,
)
from dinary.background.classification.task import (
    _drain_all_pending,
    _process_job,
    _save_parsed,
    notify_new_receipt,
    receipt_classification_task,
)
from dinary.config import settings
from dinary.db import db_migrations, storage
from dinary.db.receipts import (
    ReceiptJobRow,
    claim_next_job,
    classification_job_counts,
    count_pending_classification_jobs,
    release_job,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures for process-job integration tests
# ---------------------------------------------------------------------------


def _migration_connect(self, dburi):
    con = sqlite3.connect(str(self.uri.database), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


@pytest.fixture
def drain_db(tmp_path, monkeypatch):
    blank = tmp_path / "blank.db"
    with unittest.mock.patch.object(db_migrations.SQLiteBackend, "connect", _migration_connect):
        db_migrations.migrate_db(blank)
    dst = tmp_path / "dinary.db"
    shutil.copy(blank, dst)
    monkeypatch.setattr(storage, "DB_PATH", dst)
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "accounting_currency", "RSD")
    yield dst


def _seed_drain_db(conn):
    """Insert the minimum catalog rows required by the drain."""
    conn.execute(
        "INSERT INTO category_groups (id, name, sort_order, is_active) VALUES (1, 'Food', 1, 1)"
    )
    conn.execute(
        "INSERT INTO categories (id, name, group_id, is_active) VALUES (1, 'groceries', 1, 1)"
    )


# ---------------------------------------------------------------------------
# Process-job integration tests
# ---------------------------------------------------------------------------


@allure.epic("Receipts")
@allure.feature("Background tasks")
@allure.story("Receipt drain")
class TestProcessJobEdgeCases:
    def test_parsed_with_no_items_poisons_job(self, drain_db):  # noqa: ARG002
        """A receipt whose parser returned no items is a parse error — job must be poisoned."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, parsed_at)"
                " VALUES ('no-items', 'https://x', '2026-05-01 10:00:00')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="",
            parsed_at="2026-05-01 10:00:00",
            used_journal_fallback=False,
            claim_token="tok",
        )
        asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            job_row = conn.execute(
                "SELECT status FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
            exp_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
        finally:
            conn.close()

        assert exp_count == 0
        assert job_row is not None, "job must NOT be deleted — it must remain as an error record"
        assert job_row[0] == "poisoned", (
            f"empty-items receipt must poison the job, got {job_row[0]!r}"
        )

    def test_broker_unavailable_releases_job_for_retry(self, drain_db):  # noqa: ARG002
        """When the LLM broker returns None (used_fallback=True), job is released for retry."""
        parsed = ParsedReceipt(
            store_name="Lidl",
            store_pib="100",
            total_amount=100.0,
            invoice_number="INV-1",
            items=[
                ReceiptItem(
                    name_raw="HLEB",
                    unit_price=100.0,
                    quantity=1.0,
                    total_price=100.0,
                    tax_label="E",
                )
            ],
            items_total=100.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute("INSERT OR IGNORE INTO shop_chains (name) VALUES ('Lidl')")
            chain_id_Lidl = conn.execute("SELECT id FROM shop_chains WHERE name='Lidl'").fetchone()[
                0
            ]
            conn.execute(
                "INSERT INTO stores (name, chain_id, pib) VALUES ('Lidl', "
                + str(chain_id_Lidl)
                + ", '100')"
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('fp-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                return_value=parsed,
            ),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult(
                            item_name_normalized="hleb", category_id=None, confidence_level=1
                        )
                    ],
                    broker_unavailable=True,
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT status FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
            exp_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
        finally:
            conn.close()

        assert exp_count == 0
        assert row is not None, "job must NOT be completed — it must be retained for retry"
        assert row[0] == "pending", f"job must be released as pending, got status={row[0]!r}"

    def test_expense_datetime_matches_receipt_created_at(self, drain_db):  # noqa: ARG002
        """Expenses use the receipt's created_at as their datetime, not classification time."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=50.0,
            invoice_number="INV-2",
            items=[
                ReceiptItem(
                    name_raw="MLEKO", unit_price=50.0, quantity=1.0, total_price=50.0, tax_label="E"
                )
            ],
            items_total=50.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        fixed_created_at = "2026-01-15 08:30:00+00:00"
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, created_at)"
                " VALUES ('dt-r1', 'https://x', ?)",
                [fixed_created_at],
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-2",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                return_value=parsed,
            ),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult(
                            item_name_normalized="mleko", category_id=1, confidence_level=4
                        )
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp = conn.execute(
                "SELECT datetime FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert exp is not None
        assert str(exp[0]).startswith("2026-01-15 09:30")

    def test_valid_journal_records_usage_without_validation_failure(
        self,
        drain_db,  # noqa: ARG002
    ):
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('fb-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO app_metadata (key, value) VALUES ('receipt_fetch_fallback_count', '3')"
            )
        finally:
            conn.close()

        parsed = ParsedReceipt(
            store_name="Maxi",
            store_pib="200",
            total_amount=80.0,
            invoice_number="INV-OK",
            items=[
                ReceiptItem(
                    name_raw="HLEB", unit_price=80.0, quantity=1.0, total_price=80.0, tax_label="E"
                )
            ],
            items_total=80.0,
            total_ok=True,
            used_journal_fallback=True,
        )
        _save_parsed(receipt_id, parsed)

        conn = storage.get_connection()
        try:
            failure = conn.execute(
                "SELECT value FROM app_metadata"
                " WHERE key = 'receipt_journal_validation_failure_last'"
            ).fetchone()
            count = conn.execute(
                "SELECT value FROM app_metadata WHERE key = 'receipt_fetch_fallback_count'"
            ).fetchone()
        finally:
            conn.close()

        assert failure is None
        assert count is not None and count[0] == "4"

    def test_invalid_journal_records_validation_failure(self, drain_db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('bad-journal', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

        parsed = ParsedReceipt(
            store_name="Maxi",
            store_pib="200",
            total_amount=80.0,
            invoice_number="INV-BAD",
            items=[
                ReceiptItem(
                    name_raw="HLEB", unit_price=70.0, quantity=1.0, total_price=70.0, tax_label=""
                )
            ],
            items_total=70.0,
            total_ok=False,
            used_journal_fallback=True,
        )
        _save_parsed(receipt_id, parsed)

        conn = storage.get_connection()
        try:
            failure = conn.execute(
                "SELECT value FROM app_metadata"
                " WHERE key = 'receipt_journal_validation_failure_last'"
            ).fetchone()
            failure_count = conn.execute(
                "SELECT value FROM app_metadata"
                " WHERE key = 'receipt_journal_validation_failure_count'"
            ).fetchone()
            fallback_count = conn.execute(
                "SELECT value FROM app_metadata WHERE key = 'receipt_fetch_fallback_count'"
            ).fetchone()
        finally:
            conn.close()

        assert failure is not None
        assert "INV-BAD" in failure[0]
        assert "item total 70.00 does not match receipt total 80.00" in failure[0]
        assert failure_count is not None and failure_count[0] == "1"
        assert fallback_count is not None and fallback_count[0] == "1"

    def test_expense_datetime_uses_purchase_datetime_when_set(self, drain_db):  # noqa: ARG002
        """Expenses use purchase_datetime from the receipt when present, not created_at."""
        purchase_dt = "2026-01-10T09:15:00+01:00"
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=60.0,
            invoice_number="INV-PD",
            items=[
                ReceiptItem(
                    name_raw="KAFA", unit_price=60.0, quantity=1.0, total_price=60.0, tax_label="E"
                )
            ],
            items_total=60.0,
            total_ok=True,
            used_journal_fallback=False,
            purchase_datetime=purchase_dt,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, created_at)"
                " VALUES ('pd-r1', 'https://x', '2026-05-01 12:00:00')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-PD",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                return_value=parsed,
            ),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult(
                            item_name_normalized="kafa", category_id=1, confidence_level=4
                        )
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_dt = conn.execute(
                "SELECT datetime FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert exp_dt is not None
        # Expense datetime must reflect the purchase time, not the submission time
        assert str(exp_dt[0]).startswith("2026-01-10"), (
            f"expected purchase date 2026-01-10, got {exp_dt[0]}"
        )

    def test_transient_error_first_fail_retries_immediately(self, drain_db):  # noqa: ARG002
        """First transient failure (retry_count=0) calls notify_new_receipt for immediate retry."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tr-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserRequestError("timeout"),
            ),
            patch("dinary.background.classification.task.notify_new_receipt") as mock_notify,
            patch("dinary.background.classification.task._schedule_wakeup") as mock_wakeup,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        mock_notify.assert_called_once()
        mock_wakeup.assert_not_called()

        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT retry_count, retry_after FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == 1
        assert row[1] is None  # no retry_after for immediate retry

    def test_transient_error_second_fail_schedules_delayed_wakeup(self, drain_db):  # noqa: ARG002
        """Second transient failure (retry_count=1) schedules a 3-second delayed wakeup."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tr-r2', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_count) VALUES (?, 1)",
                [receipt_id],
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None
        assert job.retry_count == 1

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserRequestError("timeout"),
            ),
            patch("dinary.background.classification.task.notify_new_receipt") as mock_notify,
            patch("dinary.background.classification.task._schedule_wakeup") as mock_wakeup,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        mock_notify.assert_not_called()
        mock_wakeup.assert_called_once_with(3)

        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT retry_count, retry_after FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == 2
        assert row[1] is not None
        assert "T" not in str(row[1]), f"retry_after must use SQLite datetime format, got: {row[1]}"
        assert "+00:00" not in str(row[1]), f"retry_after must not include tz suffix, got: {row[1]}"

    def test_transient_error_fourth_fail_uses_15_minute_delay(self, drain_db):  # noqa: ARG002
        """From the fourth failure onward (retry_count=3+) the delay is capped at 900 seconds."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tr-r3', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_count) VALUES (?, 5)",
                [receipt_id],
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None
        assert job.retry_count == 5

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserRequestError("timeout"),
            ),
            patch("dinary.background.classification.task._schedule_wakeup") as mock_wakeup,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        mock_wakeup.assert_called_once_with(900)

    def test_transient_error_after_one_day_uses_daily_delay(self, drain_db):  # noqa: ARG002
        """After ~1 day of retries (retry_count >= 100) the delay switches to 86400 seconds."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tr-daily', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_count) VALUES (?, 100)",
                [receipt_id],
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None
        assert job.retry_count == 100

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserRequestError("timeout"),
            ),
            patch("dinary.background.classification.task._schedule_wakeup") as mock_wakeup,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        mock_wakeup.assert_called_once_with(86400)

    def test_valid_journal_keeps_llm_confidence(self, drain_db):  # noqa: ARG002
        parsed = ParsedReceipt(
            store_name="Lidl",
            store_pib="100",
            total_amount=50.0,
            invoice_number="INV-C1",
            items=[
                ReceiptItem(
                    name_raw="NEPOZNATO",
                    unit_price=50.0,
                    quantity=1.0,
                    total_price=50.0,
                    tax_label="E",
                )
            ],
            items_total=50.0,
            total_ok=True,
            used_journal_fallback=True,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute("INSERT OR IGNORE INTO shop_chains (name) VALUES ('Lidl')")
            chain_id_Lidl = conn.execute("SELECT id FROM shop_chains WHERE name='Lidl'").fetchone()[
                0
            ]
            conn.execute(
                "INSERT INTO stores (name, chain_id, pib) VALUES ('Lidl', "
                + str(chain_id_Lidl)
                + ", '100')"
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('c1-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="Lidl",
            store_pib_raw="100",
            invoice_number="INV-C1",
            parsed_at=None,
            used_journal_fallback=True,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult(
                            item_name_normalized="nepoznato", category_id=1, confidence_level=2
                        )
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
            confidence = conn.execute(
                "SELECT confidence_level FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
            job_row = conn.execute(
                "SELECT status FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert exp_count == 1
        assert confidence == 2
        assert job_row is None, "job must be completed (deleted) after creating expense"

    def test_journal_shortfall_adds_correction_expense(self, drain_db):  # noqa: ARG002
        parsed = ParsedReceipt(
            store_name="Lidl",
            store_pib="100",
            total_amount=80.0,
            invoice_number="INV-SHORT",
            items=[
                ReceiptItem(
                    name_raw="HLEB",
                    unit_price=70.0,
                    quantity=1.0,
                    total_price=70.0,
                    tax_label="",
                )
            ],
            items_total=70.0,
            total_ok=False,
            used_journal_fallback=True,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute("INSERT INTO categories (id, name, group_id) VALUES (2, 'common', 1)")
            for index in range(3):
                conn.execute(
                    "INSERT INTO expenses"
                    " (client_expense_id, datetime, amount, amount_original, currency_original,"
                    "  category_id)"
                    " VALUES (?, datetime('now'), 1, 1, 'RSD', 2)",
                    [f"history-{index}"],
                )
            conn.execute("INSERT INTO shop_chains (name) VALUES ('Lidl')")
            chain_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO stores (name, chain_id, pib) VALUES ('Lidl', ?, '100')",
                [chain_id],
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('short-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)",
                [receipt_id],
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )
        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult(
                            item_name_normalized="hleb",
                            category_id=1,
                            confidence_level=3,
                        )
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            rows = conn.execute(
                "SELECT amount_original, category_id, confidence_level, comment"
                " FROM expenses WHERE receipt_id = ? ORDER BY id",
                [receipt_id],
            ).fetchall()
        finally:
            conn.close()

        assert [float(row[0]) for row in rows] == [70.0, 10.0]
        assert sum(float(row[0]) for row in rows) == 80.0
        assert rows[1][1] == 2
        assert rows[1][2] == 1
        assert rows[1][3] == JOURNAL_CORRECTION_COMMENT

    def test_journal_excess_replaces_items_with_full_correction(self, drain_db):  # noqa: ARG002
        parsed = ParsedReceipt(
            store_name="Lidl",
            store_pib="100",
            total_amount=80.0,
            invoice_number="INV-EXCESS",
            items=[
                ReceiptItem(
                    name_raw="BAD ITEM",
                    unit_price=90.0,
                    quantity=1.0,
                    total_price=90.0,
                    tax_label="",
                )
            ],
            items_total=90.0,
            total_ok=False,
            used_journal_fallback=True,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute("INSERT INTO categories (id, name, group_id) VALUES (2, 'common', 1)")
            for index in range(3):
                conn.execute(
                    "INSERT INTO expenses"
                    " (client_expense_id, datetime, amount, amount_original, currency_original,"
                    "  category_id)"
                    " VALUES (?, datetime('now'), 1, 1, 'RSD', 2)",
                    [f"history-{index}"],
                )
            conn.execute("INSERT INTO shop_chains (name) VALUES ('Lidl')")
            chain_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO stores (name, chain_id, pib) VALUES ('Lidl', ?, '100')",
                [chain_id],
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('excess-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)",
                [receipt_id],
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )
        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch("dinary.background.classification.task.classify_receipt") as classify_mock,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            rows = conn.execute(
                "SELECT amount_original, category_id, confidence_level, comment"
                " FROM expenses WHERE receipt_id = ?",
                [receipt_id],
            ).fetchall()
            item_count = conn.execute(
                "SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()[0]
        finally:
            conn.close()

        classify_mock.assert_not_called()
        assert item_count == 0
        assert len(rows) == 1
        assert float(rows[0][0]) == 80.0
        assert rows[0][1] == 2
        assert rows[0][2] == 1
        assert rows[0][3] == JOURNAL_CORRECTION_COMMENT


# ---------------------------------------------------------------------------
# Retry backoff DB-level tests
# ---------------------------------------------------------------------------


@allure.epic("Receipts")
@allure.feature("Background tasks")
@allure.story("Receipt drain")
class TestRetryBackoff:
    def test_claim_skips_job_with_future_retry_after(self, drain_db):  # noqa: ARG002
        """claim_next_job returns None when the only pending job has a future retry_after."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('rb-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_after)"
                " VALUES (?, datetime('now', '+1 hour'))",
                [receipt_id],
            )
            result = claim_next_job(conn)
        finally:
            conn.close()

        assert result is None, "job with future retry_after must not be claimed"

    def test_claim_picks_up_job_once_retry_after_passes(self, drain_db):  # noqa: ARG002
        """claim_next_job returns a job whose retry_after (in production format) is in the past."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('rb-r2', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_count) VALUES (?, 2)",
                [receipt_id],
            )
            job = claim_next_job(conn)
            assert job is not None
            past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            release_job(conn, receipt_id, job.claim_token, 2, past)
            result = claim_next_job(conn)
        finally:
            conn.close()

        assert result is not None, "job with past retry_after must be claimable"
        assert result.retry_count == 2

    def test_release_job_stores_retry_fields(self, drain_db):  # noqa: ARG002
        """release_job persists retry_count and retry_after to the DB."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('rb-r3', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
            assert job is not None

            retry_after = "2099-01-01 00:00:00"
            release_job(conn, receipt_id, job.claim_token, 3, retry_after)

            row = conn.execute(
                "SELECT status, retry_count, retry_after FROM receipt_classification_jobs"
                " WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
            next_job = claim_next_job(conn)
        finally:
            conn.close()

        assert row[0] == "pending"
        assert row[1] == 3
        assert str(row[2]).startswith("2099-01-01")
        assert next_job is None, "future-dated retry_after must prevent claim"

    def test_stale_in_progress_job_claimed_despite_future_retry_after(self, drain_db):  # noqa: ARG002
        """Stale in_progress jobs bypass retry_after so a crashed worker doesn't block recovery."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('rb-r4', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs"
                " (receipt_id, status, claim_token, claimed_at, retry_after)"
                " VALUES (?, 'in_progress', 'old-tok',"
                "         datetime('now', '-20 minutes'),"
                "         datetime('now', '+1 hour'))",
                [receipt_id],
            )
            result = claim_next_job(conn, stale_minutes=10)
        finally:
            conn.close()

        assert result is not None, (
            "stale in_progress job must be reclaimed regardless of retry_after"
        )

    def test_stale_claim_increments_retry_count(self, drain_db):  # noqa: ARG002
        """Re-claiming a stale in_progress job increments retry_count so crash loops back off."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('stale-rc', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs"
                " (receipt_id, status, claim_token, claimed_at, retry_count)"
                " VALUES (?, 'in_progress', 'old-tok', datetime('now', '-20 minutes'), 3)",
                [receipt_id],
            )
            result = claim_next_job(conn, stale_minutes=10)
            db_row = conn.execute(
                "SELECT retry_count FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert result is not None
        assert result.retry_count == 4, (
            f"stale reclaim must increment retry_count from 3 to 4, got {result.retry_count}"
        )
        assert db_row[0] == 4, f"retry_count must be persisted in DB as 4, got {db_row[0]}"

    def test_count_pending_excludes_sleeping_jobs(self, drain_db):  # noqa: ARG002
        """count_pending_classification_jobs does not count jobs with a future retry_after."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('cnt-1', 'https://x')"
            )
            r1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_after)"
                " VALUES (?, datetime('now', '+1 hour'))",
                [r1],
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('cnt-2', 'https://x')"
            )
            r2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [r2])
            count = count_pending_classification_jobs(conn)
        finally:
            conn.close()

        assert count == 1, f"sleeping jobs must not be counted as pending, got {count}"

    def test_transient_error_release_failure_does_not_poison(self, drain_db):  # noqa: ARG002
        """If _release raises, the job is not poisoned; stale timeout handles recovery."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tr-rel-fail', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserRequestError("timeout"),
            ),
            patch(
                "dinary.background.classification.task._release",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("dinary.background.classification.task._poison") as mock_poison,
            patch("dinary.background.classification.task.notify_new_receipt") as mock_notify,
            patch("dinary.background.classification.task._schedule_wakeup") as mock_wakeup,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        mock_poison.assert_not_called()
        mock_notify.assert_not_called()
        mock_wakeup.assert_not_called()

    def test_transient_error_third_fail_uses_60s_delay(self, drain_db):  # noqa: ARG002
        """Third transient failure (retry_count=2) schedules a 60-second delayed wakeup."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tr-r60', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_count) VALUES (?, 2)",
                [receipt_id],
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None
        assert job.retry_count == 2

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserRequestError("timeout"),
            ),
            patch("dinary.background.classification.task._schedule_wakeup") as mock_wakeup,
        ):
            asyncio.run(_process_job(job, _make_broker()))

        mock_wakeup.assert_called_once_with(60)

    def test_classification_job_counts_all_states(self, drain_db):  # noqa: ARG002
        """classification_job_counts returns correct counts for all four buckets."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('cjc-1', 'https://x')"
            )
            r_pending = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [r_pending]
            )

            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('cjc-2', 'https://x')"
            )
            r_sleeping = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, retry_after)"
                " VALUES (?, datetime('now', '+1 hour'))",
                [r_sleeping],
            )

            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('cjc-3', 'https://x')"
            )
            r_inprogress = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, status, claim_token, claimed_at)"
                " VALUES (?, 'in_progress', 'tok', datetime('now'))",
                [r_inprogress],
            )

            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('cjc-4', 'https://x')"
            )
            r_poisoned = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id, status)"
                " VALUES (?, 'poisoned')",
                [r_poisoned],
            )

            counts = classification_job_counts(conn)
        finally:
            conn.close()

        assert counts["pending"] == 1
        assert counts["sleeping"] == 1
        assert counts["in_progress"] == 1
        assert counts["poisoned"] == 1


# ---------------------------------------------------------------------------
# Sheet logging integration
# ---------------------------------------------------------------------------


@allure.epic("Receipts")
@allure.feature("Background tasks")
@allure.story("Receipt drain")
class TestReceiptSheetLogging:
    """Expenses created by the receipt drain must carry a UUID and be enqueued
    for sheet logging so the drain loop can append them to Google Sheets."""

    def test_expense_has_client_expense_id(self, drain_db):  # noqa: ARG002
        """Each receipt expense gets a non-null UUID client_expense_id."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=100.0,
            invoice_number="INV-LOG-1",
            items=[
                ReceiptItem(
                    name_raw="HLEB",
                    unit_price=100.0,
                    quantity=1.0,
                    total_price=100.0,
                    tax_label="E",
                )
            ],
            items_total=100.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('log-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-LOG-1",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                return_value=parsed,
            ),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("hleb", category_id=1, confidence_level=3)]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT client_expense_id FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] is not None, "receipt expense must have a client_expense_id"
        assert len(row[0]) == 36, "client_expense_id must be a UUID (36 chars with hyphens)"

    def test_expense_enqueued_for_sheet_logging(self, drain_db):  # noqa: ARG002
        """Each receipt expense is inserted into sheet_logging_jobs as pending."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=200.0,
            invoice_number="INV-LOG-2",
            items=[
                ReceiptItem(
                    name_raw="MLEKO",
                    unit_price=100.0,
                    quantity=1.0,
                    total_price=100.0,
                    tax_label="E",
                ),
                ReceiptItem(
                    name_raw="SIR", unit_price=100.0, quantity=1.0, total_price=100.0, tax_label="E"
                ),
            ],
            items_total=200.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('log-r2', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-LOG-2",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        # Two items → two individual expenses (one per item)
        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                return_value=parsed,
            ),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult("mleko", category_id=1, confidence_level=3),
                        ClassificationResult("sir", category_id=1, confidence_level=3),
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM expenses WHERE receipt_id = ?", [receipt_id]
                ).fetchall()
            ]
            queued = (
                [
                    r[0]
                    for r in conn.execute(
                        "SELECT expense_id FROM sheet_logging_jobs WHERE expense_id IN ({})".format(
                            ",".join("?" * len(exp_ids))
                        ),
                        exp_ids,
                    ).fetchall()
                ]
                if exp_ids
                else []
            )
        finally:
            conn.close()

        assert len(exp_ids) == 2, "two items → two individual expenses (one per item)"
        assert set(queued) == set(exp_ids), "every receipt expense must be queued for sheet logging"


# ---------------------------------------------------------------------------
# Per-item expense creation tests
# ---------------------------------------------------------------------------


def _make_broker() -> llmbroker.AsyncBroker:
    """Return a minimal broker stand-in; only count() is consulted (classify_receipt
    is patched in these tests). count()=1 keeps max_attempts at 1."""
    from unittest.mock import AsyncMock, MagicMock

    broker = MagicMock(spec=llmbroker.AsyncBroker)
    broker.count = AsyncMock(return_value=1)
    return broker


def _make_classify_outcome(
    results: list[ClassificationResult],
    broker_unavailable: bool = False,
) -> ClassifyOutcome:
    from unittest.mock import AsyncMock, MagicMock

    if broker_unavailable:
        execution = None
    else:
        execution = MagicMock()
        execution.text = "ok"
        execution.llm_name = "test-model"
        execution.record_quality = AsyncMock()
    execution_failed = not broker_unavailable and (any(r.category_id is None for r in results))
    return ClassifyOutcome(
        results=results,
        broker_unavailable=broker_unavailable,
        execution_failed=execution_failed,
        execution=execution,
    )


def _seed_drain_db_with_tags(conn):
    """Seed minimal catalog + two active tags."""
    _seed_drain_db(conn)
    conn.execute("INSERT INTO tags (id, name, is_active) VALUES (1, 'dog', 1)")
    conn.execute("INSERT INTO tags (id, name, is_active) VALUES (2, 'anya', 1)")


@allure.epic("Receipts")
@allure.feature("Background tasks")
@allure.story("Receipt drain")
class TestPerItemExpenses:
    def _setup_receipt(self, conn, client_receipt_id="pi-r1", created_at=None):
        _seed_drain_db(conn)
        if created_at:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, created_at)"
                " VALUES (?, 'https://x', ?)",
                [client_receipt_id, created_at],
            )
        else:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES (?, 'https://x')",
                [client_receipt_id],
            )
        receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
        )
        return receipt_id

    def test_three_items_two_categories_creates_three_expenses(self, drain_db):  # noqa: ARG002
        """Each receipt item gets its own expense row even when two share the same category."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=300.0,
            invoice_number="INV-PI-1",
            items=[
                ReceiptItem(
                    name_raw="A", unit_price=100.0, quantity=1.0, total_price=100.0, tax_label="E"
                ),
                ReceiptItem(
                    name_raw="B", unit_price=120.0, quantity=1.0, total_price=120.0, tax_label="E"
                ),
                ReceiptItem(
                    name_raw="C", unit_price=80.0, quantity=1.0, total_price=80.0, tax_label="E"
                ),
            ],
            items_total=300.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            receipt_id = self._setup_receipt(conn)
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-PI-1",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult("a", category_id=1, confidence_level=3),
                        ClassificationResult("b", category_id=1, confidence_level=3),
                        ClassificationResult("c", category_id=1, confidence_level=3),
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            rows = conn.execute(
                "SELECT amount FROM expenses WHERE receipt_id = ? ORDER BY amount",
                [receipt_id],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 3, f"expected 3 expenses (one per item), got {len(rows)}"
        amounts = sorted(r[0] for r in rows)
        assert amounts == pytest.approx([80.0, 100.0, 120.0])

    def test_expense_tags_inserted_from_llm_tag_ids(self, drain_db):  # noqa: ARG002
        """LLM tag_ids on a ClassificationResult become expense_tags rows."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=50.0,
            invoice_number="INV-TAGS-1",
            items=[
                ReceiptItem(
                    name_raw="X", unit_price=50.0, quantity=1.0, total_price=50.0, tax_label="E"
                ),
            ],
            items_total=50.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute("INSERT INTO tags (id, name, is_active) VALUES (1, 'dog', 1)")
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('tag-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-TAGS-1",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("x", category_id=1, confidence_level=3, tag_ids=[1])]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_id = conn.execute(
                "SELECT id FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
            assert exp_id is not None
            tag_rows = conn.execute(
                "SELECT tag_id FROM expense_tags WHERE expense_id = ?", [exp_id[0]]
            ).fetchall()
        finally:
            conn.close()

        assert len(tag_rows) == 1
        assert tag_rows[0][0] == 1

    def test_expense_tags_empty_when_no_tags(self, drain_db):  # noqa: ARG002
        """When LLM returns no tag_ids, no expense_tags rows are created."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=50.0,
            invoice_number="INV-NOTAG",
            items=[
                ReceiptItem(
                    name_raw="Y", unit_price=50.0, quantity=1.0, total_price=50.0, tax_label="E"
                ),
            ],
            items_total=50.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('notag-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-NOTAG",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("y", category_id=1, confidence_level=3)]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            tag_count = conn.execute("SELECT COUNT(*) FROM expense_tags").fetchone()[0]
        finally:
            conn.close()

        assert tag_count == 0

    def test_auto_event_attached_when_event_covers_receipt_date(self, drain_db):  # noqa: ARG002
        """When an auto-attach event covers the receipt date, expenses get event_id set."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=100.0,
            invoice_number="INV-EVT",
            items=[
                ReceiptItem(
                    name_raw="Z", unit_price=100.0, quantity=1.0, total_price=100.0, tax_label="E"
                ),
            ],
            items_total=100.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO events (id, name, date_from, date_to, auto_attach_enabled, is_active, auto_tags)"
                " VALUES (1, 'Test', '2026-01-01', '2026-12-31', 1, 1, '[]')"
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, created_at)"
                " VALUES ('evt-r1', 'https://x', '2026-06-01 12:00:00')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-EVT",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("z", category_id=1, confidence_level=3)]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp = conn.execute(
                "SELECT event_id FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert exp is not None
        assert exp[0] == 1, f"expected event_id=1, got {exp[0]}"

    def test_no_auto_event_when_no_covering_event(self, drain_db):  # noqa: ARG002
        """When no auto-attach event covers the receipt date, expenses get event_id = NULL."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=50.0,
            invoice_number="INV-NOEVT",
            items=[
                ReceiptItem(
                    name_raw="W", unit_price=50.0, quantity=1.0, total_price=50.0, tax_label="E"
                ),
            ],
            items_total=50.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            # Event does NOT cover 2020 dates
            conn.execute(
                "INSERT INTO events (id, name, date_from, date_to, auto_attach_enabled, is_active)"
                " VALUES (1, 'Future', '2027-01-01', '2027-12-31', 1, 1)"
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, created_at)"
                " VALUES ('noevt-r1', 'https://x', '2020-06-01 12:00:00')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-NOEVT",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("w", category_id=1, confidence_level=3)]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp = conn.execute(
                "SELECT event_id FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert exp is not None
        assert exp[0] is None, f"expected event_id=NULL, got {exp[0]}"

    def test_auto_event_auto_tags_merged_into_expense_tags(self, drain_db):  # noqa: ARG002
        """Event auto_tags are merged into expense_tags for auto-attached expenses."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=100.0,
            invoice_number="INV-AUTOTAG",
            items=[
                ReceiptItem(
                    name_raw="Q", unit_price=100.0, quantity=1.0, total_price=100.0, tax_label="E"
                ),
            ],
            items_total=100.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute("INSERT INTO tags (id, name, is_active) VALUES (1, 'dog', 1)")
            conn.execute(
                "INSERT INTO events (id, name, date_from, date_to, auto_attach_enabled, is_active, auto_tags)"
                " VALUES (1, 'Dog Year', '2026-01-01', '2026-12-31', 1, 1, ?)",
                [json.dumps([1])],
            )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, created_at)"
                " VALUES ('autotag-r1', 'https://x', '2026-06-01 12:00:00')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
        finally:
            conn.close()

        job = ReceiptJobRow(
            receipt_id=receipt_id,
            url="https://x",
            store_name_raw="",
            store_pib_raw="",
            invoice_number="INV-AUTOTAG",
            parsed_at=None,
            used_journal_fallback=False,
            claim_token="tok",
        )

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("q", category_id=1, confidence_level=3)]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_id = conn.execute(
                "SELECT id FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
            assert exp_id is not None
            tag_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT tag_id FROM expense_tags WHERE expense_id = ?", [exp_id[0]]
                ).fetchall()
            ]
        finally:
            conn.close()

        assert 1 in tag_ids, f"tag 'dog' (id=1) must be in expense_tags, got {tag_ids}"


# ---------------------------------------------------------------------------
# Main drain loop behaviour
# ---------------------------------------------------------------------------


@allure.epic("Receipts")
@allure.feature("Background tasks")
@allure.story("Receipt drain")
class TestDrainLoop:
    def test_wakeup_triggers_drain(self):
        """notify_new_receipt wakes the drain loop immediately."""
        drained = []

        async def mock_drain(_broker=None):
            drained.append(1)

        async def run():
            with (
                patch.object(drain_mod, "_drain_all_pending", side_effect=mock_drain),
                patch.object(drain_mod.settings, "receipt_classification_enabled", True),
            ):
                task = asyncio.create_task(receipt_classification_task(_make_broker()))
                await asyncio.sleep(0)  # let task start and run initial drain
                drained.clear()  # discard the startup drain
                notify_new_receipt()
                for _ in range(10):
                    await asyncio.sleep(0)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        assert drained, "notify_new_receipt must trigger drain"

    def test_multiple_notifies_batch_into_one_drain(self):
        """Multiple rapid notifies result in a single drain cycle (events are coalesced)."""
        drained = []

        async def mock_drain(_broker=None):
            drained.append(1)

        async def run():
            with (
                patch.object(drain_mod, "_drain_all_pending", side_effect=mock_drain),
                patch.object(drain_mod.settings, "receipt_classification_enabled", True),
            ):
                task = asyncio.create_task(receipt_classification_task(_make_broker()))
                await asyncio.sleep(0)
                drained.clear()
                notify_new_receipt()
                notify_new_receipt()  # second notify before drain starts
                notify_new_receipt()
                for _ in range(20):
                    await asyncio.sleep(0)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        assert len(drained) == 1, "rapid notifies must batch into one drain cycle"

    def test_notify_new_receipt_sets_wakeup_event(self):
        """notify_new_receipt sets the module-level event so the drain loop wakes immediately."""

        async def run():
            event = asyncio.Event()
            loop = asyncio.get_running_loop()
            drain_mod._wakeup_event = event
            drain_mod._wakeup_loop = loop
            try:
                assert not event.is_set()
                notify_new_receipt()
                await asyncio.sleep(0)  # allow call_soon_threadsafe to fire
                assert event.is_set()
            finally:
                drain_mod._wakeup_event = None
                drain_mod._wakeup_loop = None

        asyncio.run(run())

    def test_notify_new_receipt_is_noop_before_task_starts(self):
        """notify_new_receipt does not raise if called before the drain task has started."""
        original = drain_mod._wakeup_event
        drain_mod._wakeup_event = None
        try:
            notify_new_receipt()  # must not raise
        finally:
            drain_mod._wakeup_event = original

    def test_drain_all_pending_noop_with_no_jobs(self, drain_db):  # noqa: ARG002
        """_drain_all_pending completes immediately when no jobs are pending."""
        asyncio.run(_drain_all_pending(_make_broker()))  # must not raise

    def test_drain_all_pending_runs_jobs_concurrently(self, drain_db):  # noqa: ARG002
        """Multiple pending jobs all run in parallel via _drain_all_pending."""
        start_times: list[float] = []

        async def slow_job(job: ReceiptJobRow, _broker=None) -> None:
            start_times.append(time.monotonic())
            await asyncio.sleep(0.05)  # simulate I/O

        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            for i in range(3):
                conn.execute(
                    "INSERT INTO receipts (client_receipt_id, url, parsed_at)"
                    " VALUES (?, 'https://x', '2026-05-01')",
                    [f"r{i}"],
                )
                rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [rid]
                )
        finally:
            conn.close()

        with patch.object(drain_mod, "_process_job", side_effect=slow_job):
            asyncio.run(_drain_all_pending(_make_broker()))

        assert len(start_times) == 3
        # All 3 should start within a short window (concurrent, not sequential)
        assert max(start_times) - min(start_times) < 0.04, "jobs must run concurrently"

    def test_safety_net_timeout_triggers_drain(self):
        """Drain fires when the 300 s wait_for times out, even without notify_new_receipt."""
        drain_call_count = {"n": 0}
        timeout_fired = {"n": 0}

        async def mock_drain(_broker=None):
            drain_call_count["n"] += 1

        async def instant_timeout(coro, timeout):  # noqa: ARG001
            # Always wrap the coro in a task and cancel it so asyncio considers it
            # properly started — prevents "coroutine was never awaited" RuntimeWarning.
            t = asyncio.ensure_future(coro)
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
            timeout_fired["n"] += 1
            if timeout_fired["n"] == 1:
                raise asyncio.TimeoutError
            # After firing once, park here so the event loop can deliver cancellation.
            await asyncio.sleep(3600)

        async def run():
            with (
                patch.object(drain_mod, "_drain_all_pending", side_effect=mock_drain),
                patch.object(drain_mod.settings, "receipt_classification_enabled", True),
                patch(
                    "dinary.background.classification.task.asyncio.wait_for",
                    side_effect=instant_timeout,
                ),
            ):
                task = asyncio.create_task(receipt_classification_task(_make_broker()))
                for _ in range(10):
                    await asyncio.sleep(0)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        assert timeout_fired["n"] >= 1, "wait_for timeout path must be exercised"
        # startup drain (call 1) + timeout-triggered drain (call 2)
        assert drain_call_count["n"] >= 2, (
            "drain must fire after 300 s timeout, not only on startup"
        )

    def test_error_in_one_job_does_not_cancel_others(self, drain_db):  # noqa: ARG002
        """An exception in one _process_job does not prevent others from completing."""
        completed: list[int] = []

        call_count = {"n": 0}

        async def mixed_job(job: ReceiptJobRow, _broker=None) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure")
            completed.append(job.receipt_id)

        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            for i in range(3):
                conn.execute(
                    "INSERT INTO receipts (client_receipt_id, url, parsed_at)"
                    " VALUES (?, 'https://x', '2026-05-01')",
                    [f"err-r{i}"],
                )
                rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [rid]
                )
        finally:
            conn.close()

        with patch.object(drain_mod, "_process_job", side_effect=mixed_job):
            asyncio.run(_drain_all_pending(_make_broker()))

        assert len(completed) == 2, "2 of 3 jobs must complete despite one failure"


# ---------------------------------------------------------------------------
# Bullet-proof: no receipt may ever be silently lost
# ---------------------------------------------------------------------------


@allure.epic("Receipts")
@allure.feature("Background tasks")
@allure.story("Receipt drain")
class TestBulletProofNeverLoseReceipt:
    """Every receipt must end up classified, poisoned, or retrying — never silently dropped.

    Root cause of receipt id=8 (849 RSD, 2026-05-27): LLM responded OK (8s latency) but
    returned confidence=1 for 'Raid Family teč/el.ap. 60 noći' (insect repellent).
    The old code logged 'completed' and deleted the job row with zero expenses.
    """

    def test_llm_all_none_exhausted_uses_fallback_and_completes_job(self, drain_db):  # noqa: ARG002
        """LLM available but returns cat_id=None → exhaustion → fallback creates expense, job done."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=849.0,
            invoice_number="INV-CONF1",
            items=[
                ReceiptItem(
                    name_raw="Raid Family tec/el.ap. 60 noci",
                    unit_price=849.0,
                    quantity=1.0,
                    total_price=849.0,
                    tax_label="E",
                )
            ],
            items_total=849.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            # Ensure at least 5 active categories for _load_top_fallback_categories pre-check
            for i in range(2, 6):
                conn.execute(
                    f"INSERT INTO categories (id, name, group_id, is_active) VALUES ({i + 1}, 'cat{i}', 1, 1)"
                )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('raid-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult(
                            item_name_normalized="raid family tec/el.ap. 60 noci",
                            category_id=None,
                            confidence_level=1,
                        )
                    ],
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
            job_row = conn.execute(
                "SELECT status FROM receipt_classification_jobs WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert exp_count == 1, "fallback must create an expense at the top catalog category"
        assert job_row is None, "job must be completed (deleted) after fallback"

    def test_parser_parse_error_poisons_job(self, drain_db):  # noqa: ARG002
        """ParserParseError (permanent, e.g. unsupported receipt format) → job poisoned."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('pe-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with patch(
            "dinary.background.classification.task.parse_receipt",
            side_effect=ParserParseError("unsupported receipt format"),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            job_row = conn.execute(
                "SELECT status, last_error FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert job_row is not None, "job must remain in DB as poisoned"
        assert job_row[0] == "poisoned", (
            f"permanent parse error must poison the job, got {job_row[0]!r}"
        )
        assert job_row[1] == "Could not read receipt data from PURS", (
            "last_error must show a user-facing diagnostic"
        )

    def test_httpx_error_releases_for_retry(self, drain_db):  # noqa: ARG002
        """httpx.HTTPError during receipt fetching is transient → job released for retry."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('he-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch("dinary.background.classification.task.notify_new_receipt"),
            patch("dinary.background.classification.task._schedule_wakeup"),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            job_row = conn.execute(
                "SELECT status, last_error FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert job_row is not None, "job must remain in DB for retry"
        assert job_row[0] == "pending", (
            f"httpx error must release job as pending, got {job_row[0]!r}"
        )
        assert job_row[1] == "Network error, retrying", (
            "last_error must show a user-facing diagnostic even for transient retries"
        )

    def test_not_indexed_error_releases_with_diagnostic(self, drain_db):  # noqa: ARG002
        """Receipt not yet indexed by PURS → transient retry with a clear diagnostic."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('ni-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch(
                "dinary.background.classification.task.parse_receipt",
                side_effect=ParserNotIndexedError("receipt may not be indexed by SUF yet"),
            ),
            patch("dinary.background.classification.task.notify_new_receipt"),
            patch("dinary.background.classification.task._schedule_wakeup"),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            job_row = conn.execute(
                "SELECT status, last_error FROM receipt_classification_jobs WHERE receipt_id = ?",
                [receipt_id],
            ).fetchone()
        finally:
            conn.close()

        assert job_row is not None, "job must remain in DB for retry"
        assert job_row[0] == "pending", (
            f"not-indexed error must release job as pending, got {job_row[0]!r}"
        )
        assert job_row[1] == "Waiting for receipt to appear in PURS"

    def test_already_parsed_receipt_skips_parsing(self, drain_db):  # noqa: ARG002
        """On retry of an already-parsed receipt, parse_receipt is not called again."""
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url, parsed_at)"
                " VALUES ('pre-parsed-r1', 'https://x', '2026-05-01T10:00:00')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_items (receipt_id, name_raw, total_price, quantity, unit_price)"
                " VALUES (?, 'hleb', 100.0, 1, 100.0)",
                [receipt_id],
            )
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None
        assert job.parsed_at is not None, "job must carry parsed_at from the receipt row"

        with (
            patch("dinary.background.classification.task.parse_receipt") as mock_parse,
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [ClassificationResult("hleb", category_id=1, confidence_level=3)]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))
            mock_parse.assert_not_called()

        conn = storage.get_connection()
        try:
            exp_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
        finally:
            conn.close()

        assert exp_count == 1, "expense must be created from pre-existing receipt items"

    def test_mixed_items_any_none_triggers_exhaustion_fallback(self, drain_db):  # noqa: ARG002
        """2-item receipt: one cat=None triggers execution_failed → exhaustion → fallback → 2 expenses."""
        parsed = ParsedReceipt(
            store_name="",
            store_pib="",
            total_amount=150.0,
            invoice_number="INV-MIX",
            items=[
                ReceiptItem(
                    name_raw="HLEB",
                    unit_price=100.0,
                    quantity=1.0,
                    total_price=100.0,
                    tax_label="E",
                ),
                ReceiptItem(
                    name_raw="NEPOZNATO",
                    unit_price=50.0,
                    quantity=1.0,
                    total_price=50.0,
                    tax_label="E",
                ),
            ],
            items_total=150.0,
            total_ok=True,
            used_journal_fallback=False,
        )
        conn = storage.get_connection()
        try:
            _seed_drain_db(conn)
            # Ensure at least 5 active categories for _load_top_fallback_categories pre-check
            for i in range(2, 6):
                conn.execute(
                    f"INSERT INTO categories (id, name, group_id, is_active) VALUES ({i + 1}, 'cat{i}', 1, 1)"
                )
            conn.execute(
                "INSERT INTO receipts (client_receipt_id, url) VALUES ('mix-r1', 'https://x')"
            )
            receipt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO receipt_classification_jobs (receipt_id) VALUES (?)", [receipt_id]
            )
            job = claim_next_job(conn)
        finally:
            conn.close()

        assert job is not None

        with (
            patch("dinary.background.classification.task.parse_receipt", return_value=parsed),
            patch(
                "dinary.background.classification.task.classify_receipt",
                return_value=_make_classify_outcome(
                    [
                        ClassificationResult("hleb", category_id=1, confidence_level=3),
                        ClassificationResult("nepoznato", category_id=None, confidence_level=1),
                    ]
                ),
            ),
        ):
            asyncio.run(_process_job(job, _make_broker()))

        conn = storage.get_connection()
        try:
            exp_count = conn.execute(
                "SELECT COUNT(*) FROM expenses WHERE receipt_id = ?", [receipt_id]
            ).fetchone()[0]
            job_row = conn.execute(
                "SELECT status FROM receipt_classification_jobs WHERE receipt_id = ?", [receipt_id]
            ).fetchone()
        finally:
            conn.close()

        assert exp_count == 2, "fallback applies to all llm_queue items → 2 expenses"
        assert job_row is None, "job must be completed (deleted) after fallback"
