"""Delayed model-quality rating on user category corrections.

A user correction of an llm-created rule rates the model that made it: partial
credit when the corrected-to category was one of the model's own alternatives,
full negative otherwise. Repeated corrections of the same rule rate only once
(the first write flips the rule to ``user_correction``). Both correction paths
are covered: by expense (review list) and by rule id (review queue).
"""

import asyncio
import shutil
import sqlite3
import unittest.mock

import allure
import pytest

from dinary.api.controllers.correction_ratings import (
    pending_rating_for_item,
    pending_rating_for_rule,
    record_correction_ratings,
)
from dinary.api.controllers.expense_corrections import (
    CategoryCorrectionRequest,
    correct_category_sync,
)
from dinary.api.controllers.expenses import ExpenseEditRequest, edit_expense_sync
from dinary.api.controllers.rules import approve_rule_category
from dinary.db import db_migrations, storage
from dinary.db.classification_rules import RuleSpec, create_or_update_rule


@pytest.fixture
def conn(tmp_path, monkeypatch):
    dst = tmp_path / "dinary.db"
    blank_src = tmp_path / "blank.db"

    def _migration_connect(self, dburi):  # noqa: ARG001
        con = sqlite3.connect(str(self.uri.database), isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    with unittest.mock.patch.object(db_migrations.SQLiteBackend, "connect", _migration_connect):
        db_migrations.migrate_db(blank_src)

    shutil.copy(blank_src, dst)
    monkeypatch.setattr(storage, "DB_PATH", dst)
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)

    c = storage.get_connection()
    c.execute("INSERT INTO category_groups (id, name, sort_order) VALUES (1, 'Food', 1)")
    c.execute("INSERT INTO categories (id, name, group_id, code) VALUES (1, 'Groceries', 1, 'g')")
    c.execute("INSERT INTO categories (id, name, group_id, code) VALUES (2, 'Drinks', 1, 'd')")
    c.execute("INSERT INTO categories (id, name, group_id, code) VALUES (3, 'Sweets', 1, 's')")
    yield c
    c.close()


def _seed_expense_with_item(
    conn: sqlite3.Connection,
    *,
    name_norm: str,
    category_id: int,
    rule: RuleSpec | None,
) -> int:
    """Insert receipt + receipt_item + expense; optionally pre-create a rule. Returns expense id."""
    conn.execute(
        "INSERT INTO receipts (id, client_receipt_id, url) VALUES (1, 'r1', 'http://x')",
    )
    conn.execute(
        "INSERT INTO expenses"
        " (id, client_expense_id, datetime, amount, amount_original, currency_original,"
        "  category_id, receipt_id, confidence_level)"
        " VALUES (1, 'e1', '2026-01-01T00:00:00', 100, 100, 'RSD', ?, 1, 3)",
        [category_id],
    )
    conn.execute(
        "INSERT INTO receipt_items (id, receipt_id, name_raw, name_normalized, expense_id)"
        " VALUES (1, 1, ?, ?, 1)",
        [name_norm, name_norm],
    )
    if rule is not None:
        create_or_update_rule(conn, None, name_norm, rule)
    return 1


@allure.epic("Review & Rules")
@allure.feature("Model quality")
class TestPendingRatingForCorrection:
    def test_alternative_gets_partial_credit(self, conn):
        create_or_update_rule(
            conn, None, "cola", RuleSpec(1, 3, "llm", alternative_category_ids=(2, 3), llm_name="m")
        )
        assert pending_rating_for_item(conn, None, "cola", 2) == ("m", 0.5)

    def test_non_alternative_gets_full_negative(self, conn):
        create_or_update_rule(
            conn, None, "cola", RuleSpec(1, 3, "llm", alternative_category_ids=(3,), llm_name="m")
        )
        assert pending_rating_for_item(conn, None, "cola", 2) == ("m", 0.0)

    def test_confirming_primary_category_not_rated(self, conn):
        create_or_update_rule(
            conn, None, "cola", RuleSpec(1, 3, "llm", alternative_category_ids=(2, 3), llm_name="m")
        )
        # Correcting to category 1 == the rule's own primary pick is a confirmation,
        # not a miss, so the model must not be rated.
        assert pending_rating_for_item(conn, None, "cola", 1) is None

    def test_user_sourced_rule_not_rated(self, conn):
        create_or_update_rule(conn, None, "cola", RuleSpec(1, 3, "user_correction", llm_name="m"))
        assert pending_rating_for_item(conn, None, "cola", 2) is None

    def test_llm_rule_without_model_not_rated(self, conn):
        create_or_update_rule(conn, None, "cola", RuleSpec(1, 3, "llm"))
        assert pending_rating_for_item(conn, None, "cola", 2) is None

    def test_missing_rule_not_rated(self, conn):
        assert pending_rating_for_item(conn, None, "cola", 2) is None


@allure.epic("Review & Rules")
@allure.feature("Model quality")
class TestCorrectCategoryRatings:
    def test_correction_of_llm_rule_records_negative(self, conn):
        _seed_expense_with_item(
            conn,
            name_norm="cola",
            category_id=1,
            rule=RuleSpec(1, 3, "llm", alternative_category_ids=(3,), llm_name="groq"),
        )
        pending: list[tuple[str, float]] = []
        correct_category_sync(
            1, CategoryCorrectionRequest(category_id=2), conn, pending_ratings=pending
        )
        assert pending == [("groq", 0.0)]

    def test_correction_to_alternative_records_partial(self, conn):
        _seed_expense_with_item(
            conn,
            name_norm="cola",
            category_id=1,
            rule=RuleSpec(1, 3, "llm", alternative_category_ids=(2,), llm_name="groq"),
        )
        pending: list[tuple[str, float]] = []
        correct_category_sync(
            1, CategoryCorrectionRequest(category_id=2), conn, pending_ratings=pending
        )
        assert pending == [("groq", 0.5)]

    def test_second_correction_records_nothing(self, conn):
        _seed_expense_with_item(
            conn,
            name_norm="cola",
            category_id=1,
            rule=RuleSpec(1, 3, "llm", alternative_category_ids=(3,), llm_name="groq"),
        )
        first: list[tuple[str, float]] = []
        correct_category_sync(
            1, CategoryCorrectionRequest(category_id=2), conn, pending_ratings=first
        )
        assert first == [("groq", 0.0)]
        # The rule is now source='user_correction'; a second correction rates nothing.
        second: list[tuple[str, float]] = []
        correct_category_sync(
            1, CategoryCorrectionRequest(category_id=3), conn, pending_ratings=second
        )
        assert second == []

    def test_user_sourced_rule_records_nothing(self, conn):
        _seed_expense_with_item(
            conn,
            name_norm="cola",
            category_id=1,
            rule=RuleSpec(1, 4, "user_correction"),
        )
        pending: list[tuple[str, float]] = []
        correct_category_sync(
            1, CategoryCorrectionRequest(category_id=2), conn, pending_ratings=pending
        )
        assert pending == []

    def test_skip_rule_mode_records_nothing(self, conn):
        """In skip_rule mode the rule is left untouched, so the verdict is read
        later by the caller's own rule update (see ``TestEditExpenseRatings``).
        """
        _seed_expense_with_item(
            conn,
            name_norm="cola",
            category_id=1,
            rule=RuleSpec(1, 3, "llm", alternative_category_ids=(3,), llm_name="groq"),
        )
        pending: list[tuple[str, float]] = []
        correct_category_sync(
            1,
            CategoryCorrectionRequest(category_id=2),
            conn,
            skip_rule=True,
            pending_ratings=pending,
        )
        assert pending == []


@allure.epic("Review & Rules")
@allure.feature("Model quality")
class TestEditExpenseRatings:
    """Editing an expense with "apply to rule" is the third path that flips an
    llm rule to ``user_correction``, and it must rate the model like the others.
    """

    def _seed_expense_on_llm_rule(self, conn, *, alternatives=(2,)) -> None:
        _seed_expense_with_item(
            conn,
            name_norm="cola",
            category_id=1,
            rule=RuleSpec(1, 3, "llm", alternative_category_ids=alternatives, llm_name="groq"),
        )
        rule_id = conn.execute("SELECT id FROM classification_rules").fetchone()[0]
        conn.execute("UPDATE expenses SET rule_id = ? WHERE id = 1", [rule_id])

    def test_rule_update_records_rating(self, conn):
        self._seed_expense_on_llm_rule(conn)
        pending: list[tuple[str, float]] = []
        edit_expense_sync(
            1,
            ExpenseEditRequest(category_id=2, update_rule=True),
            conn,
            pending_ratings=pending,
        )
        assert pending == [("groq", 0.5)]

    def test_edit_without_rule_update_records_nothing(self, conn):
        self._seed_expense_on_llm_rule(conn)
        pending: list[tuple[str, float]] = []
        edit_expense_sync(
            1,
            ExpenseEditRequest(category_id=2, update_rule=False),
            conn,
            pending_ratings=pending,
        )
        # update_rule=False routes through the plain correction path, which
        # upserts the rule itself and rates there.
        assert pending == [("groq", 0.5)]


@allure.epic("Review & Rules")
@allure.feature("Model quality")
class TestApproveRuleRatings:
    """The review queue corrects by rule id, which must rate the model too —
    it is the path a user hits most, and it also flips the rule away from its
    llm origin, so a signal missed here is lost for good.
    """

    def _llm_rule(self, conn, *, alternatives=(3,)) -> int:
        return create_or_update_rule(
            conn,
            None,
            "cola",
            RuleSpec(1, 3, "llm", alternative_category_ids=alternatives, llm_name="groq"),
        )

    def test_alternative_gets_partial_credit(self, conn):
        rule_id = self._llm_rule(conn, alternatives=(2, 3))
        assert pending_rating_for_rule(conn, rule_id, 2) == ("groq", 0.5)

    def test_non_alternative_gets_full_negative(self, conn):
        rule_id = self._llm_rule(conn)
        assert pending_rating_for_rule(conn, rule_id, 2) == ("groq", 0.0)

    def test_confirming_primary_category_not_rated(self, conn):
        rule_id = self._llm_rule(conn)
        assert pending_rating_for_rule(conn, rule_id, 1) is None

    def test_missing_rule_not_rated(self, conn):
        assert pending_rating_for_rule(conn, 999, 2) is None

    def test_approve_records_rating(self, conn):
        rule_id = self._llm_rule(conn, alternatives=(2,))
        pending: list[tuple[str, float]] = []
        approve_rule_category(rule_id, 2, conn, pending)
        assert pending == [("groq", 0.5)]

    def test_second_approve_records_nothing(self, conn):
        rule_id = self._llm_rule(conn)
        first: list[tuple[str, float]] = []
        approve_rule_category(rule_id, 2, conn, first)
        assert first == [("groq", 0.0)]
        second: list[tuple[str, float]] = []
        approve_rule_category(rule_id, 3, conn, second)
        assert second == []

    def test_user_sourced_rule_records_nothing(self, conn):
        rule_id = create_or_update_rule(
            conn, None, "cola", RuleSpec(1, 4, "user_correction", llm_name="groq")
        )
        pending: list[tuple[str, float]] = []
        approve_rule_category(rule_id, 2, conn, pending)
        assert pending == []


@allure.epic("Review & Rules")
@allure.feature("Model quality")
class TestRecordCorrectionRatings:
    def test_records_each_rating(self):
        calls: list[tuple] = []

        class _Broker:
            async def record_quality(self, name, operation, score):
                calls.append((name, operation, score))

        asyncio.run(record_correction_ratings(_Broker(), [("groq", 0.0), ("openrouter", 0.5)]))
        assert calls == [
            ("groq", "receipt_classification", 0.0),
            ("openrouter", "receipt_classification", 0.5),
        ]

    def test_none_broker_is_noop(self):
        asyncio.run(record_correction_ratings(None, [("groq", 0.0)]))

    def test_rating_failure_swallowed(self):
        class _Broker:
            async def record_quality(self, name, operation, score):
                raise RuntimeError("telemetry down")

        # Must not raise.
        asyncio.run(record_correction_ratings(_Broker(), [("groq", 0.0)]))
