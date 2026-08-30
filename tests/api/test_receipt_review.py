"""Receipt review API tests."""

import json
from datetime import UTC, datetime, timedelta

import allure
import pytest

from dinary.background.classification.persist import JOURNAL_CORRECTION_COMMENT
from dinary.db import storage

from _api_helpers import db  # noqa: F401


def _seed_review_data(conn):
    # category_groups id=1 and categories id=1 are seeded by the db fixture already
    conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
    conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
    conn.execute(
        "INSERT INTO receipts (id, client_receipt_id, url, store_id) VALUES (1, 'r1', 'https://x', 1)"
    )
    conn.execute(
        "INSERT INTO classification_rules"
        " (chain_id, item_name_normalized, category_id, confidence_level, source)"
        " VALUES (1, 'hleb', 1, 3, 'llm')"
    )
    conn.execute(
        "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original, category_id,"
        "                      confidence_level, receipt_id, store_id)"
        " VALUES (42, '2026-05-01T10:00:00', 120.0, 120.0, 'RSD', 1, 3, 1, 1)"
    )
    conn.execute(
        "INSERT INTO receipt_items"
        " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
        " VALUES (1, 1, 'hleb raw', 'hleb', 120.0, 1, 120.0, 42)"
    )


def _seed_certain_rule(conn):
    conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
    conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
    conn.execute(
        "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
        " VALUES (1, 'r1', 'https://x', 1)"
    )
    conn.execute(
        "INSERT INTO classification_rules"
        " (chain_id, item_name_normalized, category_id, confidence_level, source)"
        " VALUES (1, 'mleko', 1, 4, 'llm')"
    )
    conn.execute(
        "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
        "                      category_id, confidence_level, receipt_id, store_id)"
        " VALUES (10, '2026-05-01T10:00:00', 200.0, 200.0, 'RSD', 1, 4, 1, 1)"
    )
    conn.execute(
        "INSERT INTO receipt_items"
        " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
        " VALUES (1, 1, 'mleko raw', 'mleko', 200.0, 1, 200.0, 10)"
    )


def _seed_pending_journal_correction(conn, expense_id: int = 90) -> None:
    conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
    conn.execute(
        "INSERT OR IGNORE INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')"
    )
    conn.execute(
        "INSERT INTO receipts (id, client_receipt_id, url, store_id, total_amount)"
        " VALUES (90, 'correction-r1', 'https://x', 1, 100)"
    )
    conn.execute(
        "INSERT INTO expenses"
        " (id, datetime, amount, amount_original, currency_original, category_id,"
        "  confidence_level, comment, receipt_id, store_id)"
        " VALUES (?, '2026-05-01T10:00:00', 80, 80, 'RSD', 1, 1, ?, 90, 1)",
        [expense_id, JOURNAL_CORRECTION_COMMENT],
    )


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestReviewFeed:
    def test_empty_feed(self, client, db):  # noqa: ARG002
        resp = client.get("/api/rules/feed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["doubtful_count"] == 0
        assert "receipts_queue" in data
        q = data["receipts_queue"]
        assert q["pending"] == 0
        assert q["in_progress"] == 0
        assert q["sleeping"] == 0
        assert q["poisoned"] == 0

    def test_doubtful_item_in_block1(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            _seed_review_data(conn)
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        data = resp.json()
        assert data["doubtful_count"] == 1
        assert len(data["items"]) >= 1
        doubtful = [i for i in data["items"] if i["is_doubtful"]]
        assert len(doubtful) == 1
        d = doubtful[0]
        assert d["name"] == "hleb"
        assert d["store"] == "Lidl"
        assert d["total"] == 120.0
        assert d["count"] == 1
        assert d["currency"] == "RSD"
        assert d["confidence_level"] == 3
        assert d["category_id"] == 1
        assert d["expense_id"] == 42
        assert "id" in d

    def test_journal_correction_requires_review(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            _seed_pending_journal_correction(conn)
        finally:
            conn.close()

        response = client.get("/api/rules/feed")

        assert response.status_code == 200
        data = response.json()
        assert data["doubtful_count"] == 1
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["review_kind"] == "expense_correction"
        assert item["is_doubtful"] is True
        assert item["expense_id"] == 90
        assert item["total"] == 80
        assert item["receipt_total"] == 100
        assert item["confidence_level"] == 1
        assert item["comment"] == JOURNAL_CORRECTION_COMMENT
        assert item["has_rule"] is False
        assert item["item_name"] is None

    def test_correction_and_rule_paginate_without_collision(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            _seed_pending_journal_correction(conn, expense_id=42)
            conn.execute(
                "INSERT INTO classification_rules"
                " (id, chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (42, 1, 'hleb', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original, category_id,"
                "  confidence_level, receipt_id, store_id, rule_id)"
                " VALUES (43, '2026-05-01T10:00:00', 20, 20, 'RSD', 1, 3, 90, 1, 42)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (receipt_id, name_raw, name_normalized, total_price, quantity, unit_price,"
                "  expense_id) VALUES (90, 'hleb', 'hleb', 20, 1, 20, 43)"
            )
        finally:
            conn.close()

        page_one = client.get("/api/rules/feed?page=1&page_size=1").json()
        page_two = client.get("/api/rules/feed?page=2&page_size=1").json()

        assert page_one["doubtful_count"] == 2
        assert page_one["items"][0]["review_kind"] == "expense_correction"
        assert page_two["items"][0]["id"] == 42
        assert "review_kind" not in page_two["items"][0]

    def test_pagination(self, client, db):  # noqa: ARG002
        resp = client.get("/api/rules/feed?page=1&page_size=5")
        assert resp.status_code == 200

    def test_page_size_limit(self, client, db):  # noqa: ARG002
        resp = client.get("/api/rules/feed?page_size=101")
        assert resp.status_code == 422

    def test_two_block_pagination_uses_sql_limit_not_memory_slice(self, client, db):  # noqa: ARG002
        """Page 2 must return a different rule than page 1 with page_size=1."""
        conn = storage.get_connection()
        try:
            conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
            conn.execute(
                "INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')"
            )
            conn.execute(
                "INSERT INTO receipts (id, client_receipt_id, url, store_id, created_at)"
                " VALUES (1, 'r1', 'https://x', 1, '2026-05-02T10:00:00')"
            )
            conn.execute(
                "INSERT INTO receipts (id, client_receipt_id, url, store_id, created_at)"
                " VALUES (2, 'r2', 'https://y', 1, '2026-05-01T10:00:00')"
            )
            conn.execute(
                "INSERT INTO classification_rules"
                " (id, chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (10, 1, 'item_a', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO classification_rules"
                " (id, chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (11, 1, 'item_b', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original,"
                "  category_id, confidence_level, receipt_id, store_id)"
                " VALUES (10, '2026-05-02T10:00:00', 100.0, 100.0, 'RSD', 1, 3, 1, 1)"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original,"
                "  category_id, confidence_level, receipt_id, store_id)"
                " VALUES (11, '2026-05-01T10:00:00', 200.0, 200.0, 'RSD', 1, 3, 2, 1)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
                " VALUES (10, 1, 'item_a raw', 'item_a', 100.0, 1, 100.0, 10)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
                " VALUES (11, 2, 'item_b raw', 'item_b', 200.0, 1, 200.0, 11)"
            )
        finally:
            conn.close()

        p1 = client.get("/api/rules/feed?page=1&page_size=1&doubtful_only=false").json()
        p2 = client.get("/api/rules/feed?page=2&page_size=1&doubtful_only=false").json()

        assert len(p1["items"]) == 1
        assert p1["has_more"] is True
        assert len(p2["items"]) == 1
        assert p2["has_more"] is False
        # Pages must contain different rules
        assert p1["items"][0]["id"] != p2["items"][0]["id"]


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestReviewFeedCertainRules:
    def test_certain_rule_in_feed(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            _seed_certain_rule(conn)
            # Add a doubtful rule so d_total > 0 and the feed query runs
            conn.execute(
                "INSERT INTO classification_rules"
                " (chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (1, 'jogurt', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original,"
                "  category_id, confidence_level, receipt_id, store_id)"
                " VALUES (99, '2026-05-01T10:00:00', 50.0, 50.0, 'RSD', 1, 3, 1, 1)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
                " VALUES (99, 1, 'jogurt raw', 'jogurt', 50.0, 1, 50.0, 99)"
            )
        finally:
            conn.close()

        resp = client.get("/api/rules/feed?doubtful_only=false")
        data = resp.json()
        certain = [i for i in data["items"] if not i["is_doubtful"]]
        assert len(certain) == 1
        c = certain[0]
        assert c["total"] == 200.0
        assert c["currency"] == "RSD"
        assert c["store"] == "Lidl"
        assert c["confidence_level"] == 4
        assert c["name"] == "mleko"
        assert c["count"] == 1
        assert "id" in c
        assert "datetime" in c

    def test_certain_rule_not_included_in_doubtful_count(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            _seed_certain_rule(conn)
        finally:
            conn.close()

        resp = client.get("/api/rules/feed?doubtful_only=false")
        assert resp.json()["doubtful_count"] == 0

    def test_rule_name_comes_from_classification_rules(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            _seed_certain_rule(conn)
            # Add a doubtful rule so d_total > 0 and the feed query runs
            conn.execute(
                "INSERT INTO classification_rules"
                " (chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (1, 'jogurt', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original,"
                "  category_id, confidence_level, receipt_id, store_id)"
                " VALUES (99, '2026-05-01T10:00:00', 50.0, 50.0, 'RSD', 1, 3, 1, 1)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
                " VALUES (99, 1, 'jogurt raw', 'jogurt', 50.0, 1, 50.0, 99)"
            )
        finally:
            conn.close()

        resp = client.get("/api/rules/feed?doubtful_only=false")
        certain = [i for i in resp.json()["items"] if not i["is_doubtful"]]
        assert len(certain) == 1
        assert certain[0]["name"] == "mleko"

    def test_doubtful_rule_appears_as_doubtful(self, client, db):  # noqa: ARG002
        """A low-confidence rule with a matching receipt_item appears as doubtful."""
        conn = storage.get_connection()
        try:
            conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
            conn.execute(
                "INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')"
            )
            conn.execute(
                "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
                " VALUES (1, 'r1', 'https://x', 1)"
            )
            conn.execute(
                "INSERT INTO classification_rules"
                " (chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (1, 'jogurt', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original,"
                "  category_id, confidence_level, receipt_id, store_id)"
                " VALUES (11, '2026-05-01T10:00:00', 150.0, 150.0, 'RSD', 1, 3, 1, 1)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
                " VALUES (1, 1, 'jogurt raw', 'jogurt', 150.0, 1, 150.0, 11)"
            )
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        data = resp.json()
        doubtful = [i for i in data["items"] if i["is_doubtful"]]
        assert len(doubtful) == 1
        assert doubtful[0]["total"] == 150.0
        assert doubtful[0]["confidence_level"] == 3


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestCategoryCorrection:
    def _seed_correction(self, conn):
        conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
        conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (1, 'r1', 'https://x', 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (1, 1, 'hleb raw', 'hleb', 120.0, 1, 120.0)"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id, store_id)"
            " VALUES (1, '2026-05-01T10:00:00', 120.0, 120.0, 'RSD', 1, 3, 1, 1)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 1 WHERE id = 1")

    def test_correction_sets_conf4(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_correction(conn)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["corrected_expense_id"] == 1

        conn = storage.get_connection()
        try:
            exp = conn.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

        assert exp[0] == 2
        assert exp[1] == 4

    def test_correction_creates_rule(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_correction(conn)
        finally:
            conn.close()

        client.patch("/api/expenses/1/category", json={"category_id": 2})

        conn = storage.get_connection()
        try:
            rule = conn.execute(
                "SELECT category_id, confidence_level, source"
                " FROM classification_rules WHERE item_name_normalized = 'hleb'"
            ).fetchone()
        finally:
            conn.close()

        assert rule is not None
        assert rule[0] == 2
        assert rule[1] == 4
        assert rule[2] == "user_correction"

    def test_journal_correction_confirmation_does_not_create_rule(
        self,
        client,
        db,  # noqa: ARG002
    ):
        conn = storage.get_connection()
        try:
            _seed_pending_journal_correction(conn)
        finally:
            conn.close()

        response = client.patch("/api/expenses/90/category", json={"category_id": 2})

        assert response.status_code == 200
        conn = storage.get_connection()
        try:
            expense = conn.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = 90"
            ).fetchone()
            rule_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
            feed = client.get("/api/rules/feed").json()
        finally:
            conn.close()

        assert tuple(expense) == (2, 4)
        assert rule_count == 0
        assert feed["doubtful_count"] == 0

    def test_correction_unknown_expense_returns_404(self, client, db):  # noqa: ARG002
        resp = client.patch("/api/expenses/9999/category", json={"category_id": 1})
        assert resp.status_code == 404

    def test_correction_inactive_category_is_activated_on_use(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_correction(conn)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 3})
        assert resp.status_code == 200, resp.text

        conn = storage.get_connection()
        try:
            (is_active,) = conn.execute(
                "SELECT is_active FROM categories WHERE id = 3",
            ).fetchone()
        finally:
            conn.close()
        assert bool(is_active) is True

    def test_correction_retired_category_returns_422(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_correction(conn)
            conn.execute("UPDATE categories SET is_retired = TRUE WHERE id = 3")
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 3})
        assert resp.status_code == 422

    def test_correction_hidden_category_returns_422(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_correction(conn)
            conn.execute("UPDATE categories SET is_hidden = TRUE WHERE id = 3")
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 3})
        assert resp.status_code == 422

    def test_correction_on_non_receipt_expense(self, client, db):  # noqa: ARG002
        """Correcting a manual (non-receipt) expense updates category only — no rules, no items."""
        conn = storage.get_connection()
        try:
            conn.execute(
                "INSERT INTO expenses"
                " (datetime, amount, amount_original, currency_original, category_id, confidence_level)"
                " VALUES ('2026-05-01T10:00:00', 150.0, 150.0, 'RSD', 1, 3)"
            )
            expense_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

        resp = client.patch(f"/api/expenses/{expense_id}/category", json={"category_id": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["corrected_expense_id"] == expense_id
        assert data["batch_updated_count"] == 0

        conn = storage.get_connection()
        try:
            exp = conn.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = ?", [expense_id]
            ).fetchone()
            rule_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        finally:
            conn.close()

        assert exp[0] == 2
        assert exp[1] == 3  # manual expense: confidence_level unchanged
        assert rule_count == 0


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestBatchPropagation:
    def _seed(self, conn):
        conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
        conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (1, 'r1', 'https://x', 1)"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id, store_id)"
            " VALUES (1, '2026-05-01T10:00:00', 100.0, 100.0, 'RSD', 1, 3, 1, 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (1, 1, 'hleb raw', 'hleb', 100.0, 1, 100.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 1 WHERE id = 1")

        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (2, 'r2', 'https://y', 1)"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id, store_id)"
            " VALUES (2, '2026-05-02T10:00:00', 80.0, 80.0, 'RSD', 1, 3, 2, 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (2, 2, 'hleb raw', 'hleb', 80.0, 1, 80.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 2 WHERE id = 2")

    def test_correcting_one_expense_updates_same_item_in_other_receipts(
        self,
        client,
        db,  # noqa: ARG002
    ):
        conn = storage.get_connection()
        try:
            self._seed(conn)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_updated_count"] == 1

        conn = storage.get_connection()
        try:
            # Direct expense corrected
            exp1 = conn.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = 1"
            ).fetchone()
            # Batch: expense2 had only "hleb" → category updated
            exp2 = conn.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = 2"
            ).fetchone()
        finally:
            conn.close()

        assert exp1[0] == 2
        assert exp2[0] == 2
        assert exp2[1] == 4

    def test_batch_creates_rule_for_corrected_item(
        self,
        client,
        db,  # noqa: ARG002
    ):
        conn = storage.get_connection()
        try:
            self._seed(conn)
        finally:
            conn.close()

        client.patch("/api/expenses/1/category", json={"category_id": 2})

        conn = storage.get_connection()
        try:
            rule = conn.execute(
                "SELECT category_id, confidence_level, source"
                " FROM classification_rules WHERE item_name_normalized = 'hleb'"
            ).fetchone()
        finally:
            conn.close()

        assert rule is not None
        assert rule[0] == 2
        assert rule[1] == 4
        assert rule[2] == "user_correction"


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestBatchPropagationNullStore:
    """Batch correction must use NULL-safe store matching (IS instead of =)."""

    def _seed(self, conn):
        # Receipt 1: store_id=NULL (unresolved)
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url) VALUES (10, 'null-r1', 'https://x')"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id)"
            " VALUES (10, '2026-05-01T10:00:00', 100.0, 100.0, 'RSD', 1, 3, 10)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (10, 10, 'hleb raw', 'hleb', 100.0, 1, 100.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 10 WHERE id = 10")

        # Receipt 2: store_id=NULL (also unresolved) — should be batch-updated
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url) VALUES (11, 'null-r2', 'https://y')"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id)"
            " VALUES (11, '2026-05-02T10:00:00', 80.0, 80.0, 'RSD', 1, 3, 11)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (11, 11, 'hleb raw', 'hleb', 80.0, 1, 80.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 11 WHERE id = 11")

        # Receipt 3: store_id=1 (known store) — must NOT be batch-updated
        conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
        conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (12, 'lidl-r3', 'https://z', 1)"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id, store_id)"
            " VALUES (12, '2026-05-03T10:00:00', 60.0, 60.0, 'RSD', 1, 3, 12, 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (12, 12, 'hleb raw', 'hleb', 60.0, 1, 60.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 12 WHERE id = 12")

    def test_null_store_batch_does_not_spill_into_known_store_receipts(
        self,
        client,
        db,  # noqa: ARG002
    ):
        """Correcting an unresolved-store expense only propagates to other unresolved-store receipts."""
        conn = storage.get_connection()
        try:
            self._seed(conn)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/10/category", json={"category_id": 2})
        assert resp.status_code == 200
        assert resp.json()["batch_updated_count"] == 1  # only the null-store receipt 11

        conn = storage.get_connection()
        try:
            exp11 = conn.execute("SELECT category_id FROM expenses WHERE id = 11").fetchone()
            exp12 = conn.execute("SELECT category_id FROM expenses WHERE id = 12").fetchone()
        finally:
            conn.close()

        assert exp11[0] == 2, "null-store receipt 11 must be batch-updated"
        assert exp12[0] == 1, "known-store Lidl receipt 12 must NOT be batch-updated"

    def test_null_store_batch_propagates_to_other_null_store_receipts(
        self,
        client,
        db,  # noqa: ARG002
    ):
        """Correcting an unresolved-store expense propagates to all null-store receipts."""
        conn = storage.get_connection()
        try:
            self._seed(conn)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/10/category", json={"category_id": 2})
        assert resp.status_code == 200

        conn = storage.get_connection()
        try:
            exp11 = conn.execute("SELECT category_id FROM expenses WHERE id = 11").fetchone()
        finally:
            conn.close()

        assert exp11[0] == 2


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestCategoryBatchCorrection:
    """scope=all updates each matched expense directly — no rows created or split."""

    def _seed(self, conn):
        conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
        conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
        # receipt1: one "hleb" item → one expense (per-item model)
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (1, 'r1', 'https://x', 1)"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id, store_id)"
            " VALUES (1, '2026-05-01T10:00:00', 100.0, 100.0, 'RSD', 1, 3, 1, 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (1, 1, 'hleb raw', 'hleb', 100.0, 1, 100.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 1 WHERE id = 1")

        # receipt2: one "hleb" item → separate expense
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (2, 'r2', 'https://y', 1)"
        )
        conn.execute(
            "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            "                      category_id, confidence_level, receipt_id, store_id)"
            " VALUES (2, '2026-05-02T10:00:00', 80.0, 80.0, 'RSD', 1, 3, 2, 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (2, 2, 'hleb raw', 'hleb', 80.0, 1, 80.0)"
        )
        conn.execute("UPDATE receipt_items SET expense_id = 2 WHERE id = 2")

    def test_scope_all_updates_other_expense_in_place(self, client, db):  # noqa: ARG002
        """scope=all updates matching expenses directly — no new rows created."""
        conn = storage.get_connection()
        try:
            self._seed(conn)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/2/category", json={"category_id": 2})
        assert resp.status_code == 200
        assert resp.json()["batch_updated_count"] == 1

        conn = storage.get_connection()
        try:
            exp1 = conn.execute(
                "SELECT amount, category_id, confidence_level FROM expenses WHERE id = 1"
            ).fetchone()
            # expense1 updated in-place: category changed, amount unchanged, no new rows
            assert exp1[1] == 2
            assert exp1[2] == 4
            assert float(exp1[0]) == pytest.approx(100.0)

            total_expenses = conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
            assert total_expenses == 2  # no new rows created
        finally:
            conn.close()

    def test_scope_all_no_new_rows_created(self, client, db):  # noqa: ARG002
        """Batch correction never inserts expense rows."""
        conn = storage.get_connection()
        try:
            self._seed(conn)
        finally:
            conn.close()

        before = storage.get_connection().execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        storage.get_connection().close()

        client.patch("/api/expenses/1/category", json={"category_id": 2})

        after = storage.get_connection().execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        storage.get_connection().close()
        assert after == before


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestScopedCorrections:
    def _seed(self, conn, expense_date: str, expense_id: int = 1):
        conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
        conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
        conn.execute(
            f"INSERT INTO receipts (id, client_receipt_id, url, store_id, created_at)"
            f" VALUES (1, 'r1', 'https://x', 1, '{expense_date}')"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            " VALUES (1, 1, 'hleb raw', 'hleb', 120.0, 1, 120.0)"
        )
        conn.execute(
            f"INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            f"                      category_id, confidence_level, receipt_id, store_id)"
            f" VALUES ({expense_id}, '{expense_date}', 120.0, 120.0, 'RSD', 1, 3, 1, 1)"
        )
        conn.execute(f"UPDATE receipt_items SET expense_id = {expense_id} WHERE id = 1")

    def _seed_other_expense(self, conn, expense_date: str, receipt_id: int, expense_id: int):
        conn.execute(
            f"INSERT INTO receipts (id, client_receipt_id, url, store_id, created_at)"
            f" VALUES ({receipt_id}, 'r{receipt_id}', 'https://z{receipt_id}', 1, '{expense_date}')"
        )
        conn.execute(
            f"INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
            f"                      category_id, confidence_level, receipt_id, store_id)"
            f" VALUES ({expense_id}, '{expense_date}', 80.0, 80.0, 'RSD', 1, 3, {receipt_id}, 1)"
        )
        conn.execute(
            f"INSERT INTO receipt_items"
            f" (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price)"
            f" VALUES ({expense_id}, {receipt_id}, 'hleb raw', 'hleb', 80.0, 1, 80.0)"
        )
        conn.execute(f"UPDATE receipt_items SET expense_id = {expense_id} WHERE id = {expense_id}")

    def test_scope_single_skips_batch(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed(conn, "2026-05-01T10:00:00", expense_id=1)
            self._seed_other_expense(conn, "2026-05-01T10:00:00", receipt_id=2, expense_id=2)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 2, "scope": "single"})
        assert resp.status_code == 200
        assert resp.json()["batch_updated_count"] == 0

        conn = storage.get_connection()
        try:
            exp2 = conn.execute("SELECT category_id FROM expenses WHERE id = 2").fetchone()
        finally:
            conn.close()

        assert exp2[0] == 1, "other expense must not be updated with scope=single"

    def test_scope_single_still_upserts_rule(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed(conn, "2026-05-01T10:00:00", expense_id=1)
        finally:
            conn.close()

        client.patch("/api/expenses/1/category", json={"category_id": 2, "scope": "single"})

        conn = storage.get_connection()
        try:
            rule = conn.execute(
                "SELECT category_id FROM classification_rules WHERE item_name_normalized = 'hleb'"
            ).fetchone()
        finally:
            conn.close()

        assert rule is not None
        assert rule[0] == 2

    def test_scope_all_updates_all_history(self, client, db):  # noqa: ARG002
        old_date = "2020-01-01T10:00:00"
        conn = storage.get_connection()
        try:
            self._seed(conn, "2026-05-01T10:00:00", expense_id=1)
            self._seed_other_expense(conn, old_date, receipt_id=2, expense_id=2)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 2, "scope": "all"})
        assert resp.status_code == 200
        assert resp.json()["batch_updated_count"] == 1

        conn = storage.get_connection()
        try:
            exp2 = conn.execute("SELECT category_id FROM expenses WHERE id = 2").fetchone()
        finally:
            conn.close()

        assert exp2[0] == 2

    def test_scope_month_only_updates_recent_expenses(self, client, db):  # noqa: ARG002
        recent_date = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        old_date = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
        conn = storage.get_connection()
        try:
            self._seed(conn, recent_date, expense_id=1)
            self._seed_other_expense(conn, recent_date, receipt_id=2, expense_id=2)
            self._seed_other_expense(conn, old_date, receipt_id=3, expense_id=3)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 2, "scope": "month"})
        assert resp.status_code == 200
        assert resp.json()["batch_updated_count"] == 1

        conn = storage.get_connection()
        try:
            exp2 = conn.execute("SELECT category_id FROM expenses WHERE id = 2").fetchone()
            exp3 = conn.execute("SELECT category_id FROM expenses WHERE id = 3").fetchone()
        finally:
            conn.close()

        assert exp2[0] == 2, "recent expense must be batch-updated"
        assert exp3[0] == 1, "old expense outside 30-day window must not be updated"

    def test_scope_year_only_updates_this_year(self, client, db):  # noqa: ARG002
        this_year_date = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        last_year_date = f"{datetime.now(UTC).year - 1}-06-15T10:00:00"
        conn = storage.get_connection()
        try:
            self._seed(conn, this_year_date, expense_id=1)
            self._seed_other_expense(conn, this_year_date, receipt_id=2, expense_id=2)
            self._seed_other_expense(conn, last_year_date, receipt_id=3, expense_id=3)
        finally:
            conn.close()

        resp = client.patch("/api/expenses/1/category", json={"category_id": 2, "scope": "year"})
        assert resp.status_code == 200
        assert resp.json()["batch_updated_count"] == 1

        conn = storage.get_connection()
        try:
            exp2 = conn.execute("SELECT category_id FROM expenses WHERE id = 2").fetchone()
            exp3 = conn.execute("SELECT category_id FROM expenses WHERE id = 3").fetchone()
        finally:
            conn.close()

        assert exp2[0] == 2, "this-year expense must be batch-updated"
        assert exp3[0] == 1, "last-year expense must not be updated"


@allure.epic("Receipts")
@allure.feature("API")
@allure.story("Receipt review")
class TestRulesFeedAlternativesAndTags:
    """Rules feed items must include alternative_categories and tags fields."""

    def _seed_rule_with_alternatives_and_tags(self, conn):
        conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
        conn.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
        conn.execute(
            "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
            " VALUES (1, 'alt-r1', 'https://x', 1)"
        )
        conn.execute("INSERT INTO tags (id, name, is_active) VALUES (10, 'special', 1)")
        conn.execute(
            "INSERT INTO classification_rules"
            " (chain_id, item_name_normalized, category_id, confidence_level, source,"
            "  alternative_category_ids, tag_ids)"
            " VALUES (1, 'keks', 1, 3, 'llm', ?, ?)",
            [json.dumps([2]), json.dumps([10])],
        )
        conn.execute(
            "INSERT INTO expenses"
            " (id, datetime, amount, amount_original, currency_original,"
            "  category_id, confidence_level, receipt_id, store_id)"
            " VALUES (1, '2026-05-01T10:00:00', 50.0, 50.0, 'RSD', 1, 3, 1, 1)"
        )
        conn.execute(
            "INSERT INTO receipt_items"
            " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
            " VALUES (1, 1, 'keks raw', 'keks', 50.0, 1, 50.0, 1)"
        )

    def test_feed_items_include_alternative_categories_field(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_rule_with_alternatives_and_tags(conn)
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        item = items[0]
        assert "alternative_categories" in item
        assert isinstance(item["alternative_categories"], list)

    def test_feed_items_include_tags_field(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_rule_with_alternatives_and_tags(conn)
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        item = items[0]
        assert "tags" in item
        assert isinstance(item["tags"], list)

    def test_alternative_categories_resolved_to_name_id_objects(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_rule_with_alternatives_and_tags(conn)
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        item = resp.json()["items"][0]
        alt_cats = item["alternative_categories"]
        # category id=2 (transit) is in the alternatives; should be resolved
        assert len(alt_cats) == 1
        assert alt_cats[0]["id"] == 2
        assert isinstance(alt_cats[0]["name"], str)

    def test_tags_resolved_to_name_id_objects(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            self._seed_rule_with_alternatives_and_tags(conn)
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        item = resp.json()["items"][0]
        tags = item["tags"]
        assert len(tags) == 1
        assert tags[0]["id"] == 10
        assert tags[0]["name"] == "special"

    def test_alternative_categories_empty_when_not_set(self, client, db):  # noqa: ARG002
        conn = storage.get_connection()
        try:
            conn.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
            conn.execute(
                "INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')"
            )
            conn.execute(
                "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
                " VALUES (1, 'noalt-r1', 'https://x', 1)"
            )
            conn.execute(
                "INSERT INTO classification_rules"
                " (chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (1, 'voda', 1, 3, 'llm')"
            )
            conn.execute(
                "INSERT INTO expenses"
                " (id, datetime, amount, amount_original, currency_original,"
                "  category_id, confidence_level, receipt_id, store_id)"
                " VALUES (1, '2026-05-01T10:00:00', 30.0, 30.0, 'RSD', 1, 3, 1, 1)"
            )
            conn.execute(
                "INSERT INTO receipt_items"
                " (id, receipt_id, name_raw, name_normalized, total_price, quantity, unit_price, expense_id)"
                " VALUES (1, 1, 'voda raw', 'voda', 30.0, 1, 30.0, 1)"
            )
        finally:
            conn.close()

        resp = client.get("/api/rules/feed")
        item = resp.json()["items"][0]
        assert item["alternative_categories"] == []
        assert item["tags"] == []
