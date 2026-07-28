"""PATCH /api/rules/:id/category — rule approval by rule ID."""

import allure

from dinary.db import storage

from _api_helpers import db  # noqa: F401


def _seed(con, *, rule_category_id=1, expense_confidence=3):
    """Seed: shop_chain → store → receipt → classification_rule → expenses with rule_id."""
    con.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
    con.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
    con.execute(
        "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
        " VALUES (1, 'r1', 'https://x', 1)"
    )
    con.execute(
        "INSERT INTO classification_rules"
        " (id, chain_id, item_name_normalized, category_id, confidence_level, source)"
        f" VALUES (10, 1, 'hleb', {rule_category_id}, {expense_confidence}, 'llm')"
    )
    con.execute(
        "INSERT INTO receipt_items (id, receipt_id, name_raw, name_normalized,"
        " total_price, quantity, unit_price)"
        " VALUES (1, 1, 'hleb raw', 'hleb', 100.0, 1, 100.0)"
    )
    con.execute(
        "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
        "                      category_id, confidence_level, receipt_id, store_id, rule_id)"
        f" VALUES (1, '2026-05-01T10:00:00', 100.0, 100.0, 'RSD', {rule_category_id},"
        f"         {expense_confidence}, 1, 1, 10)"
    )
    con.execute("UPDATE receipt_items SET expense_id = 1 WHERE id = 1")
    # Second expense linked to the same rule
    con.execute(
        "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
        " VALUES (2, 'r2', 'https://y', 1)"
    )
    con.execute(
        "INSERT INTO receipt_items (id, receipt_id, name_raw, name_normalized,"
        " total_price, quantity, unit_price)"
        " VALUES (2, 2, 'hleb raw', 'hleb', 80.0, 1, 80.0)"
    )
    con.execute(
        "INSERT INTO expenses (id, datetime, amount, amount_original, currency_original,"
        "                      category_id, confidence_level, receipt_id, store_id, rule_id)"
        f" VALUES (2, '2026-05-02T10:00:00', 80.0, 80.0, 'RSD', {rule_category_id},"
        f"         {expense_confidence}, 2, 1, 10)"
    )
    con.execute("UPDATE receipt_items SET expense_id = 2 WHERE id = 2")


@allure.epic("Review & Rules")
@allure.feature("API")
class TestApproveRuleCategory:
    def test_approve_rule_updates_rule_to_confidence_4(self, client, db):  # noqa: ARG002
        con = storage.get_connection()
        try:
            _seed(con)
        finally:
            con.close()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})
        assert resp.status_code == 200

        con = storage.get_connection()
        try:
            rule = con.execute(
                "SELECT category_id, confidence_level, source"
                " FROM classification_rules WHERE id = 10"
            ).fetchone()
        finally:
            con.close()

        assert rule[0] == 2
        assert rule[1] == 4
        assert rule[2] == "user_correction"

    def test_approve_rule_updates_all_linked_expenses(self, client, db):  # noqa: ARG002
        con = storage.get_connection()
        try:
            _seed(con)
        finally:
            con.close()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated_expenses_count"] == 2

        con = storage.get_connection()
        try:
            exp1 = con.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = 1"
            ).fetchone()
            exp2 = con.execute(
                "SELECT category_id, confidence_level FROM expenses WHERE id = 2"
            ).fetchone()
        finally:
            con.close()

        assert exp1[0] == 2
        assert exp1[1] == 4
        assert exp2[0] == 2
        assert exp2[1] == 4

    def test_approve_rule_with_different_category(self, client, db):  # noqa: ARG002
        con = storage.get_connection()
        try:
            _seed(con, rule_category_id=1)
        finally:
            con.close()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})
        assert resp.status_code == 200

        con = storage.get_connection()
        try:
            rule_cat = con.execute(
                "SELECT category_id FROM classification_rules WHERE id = 10"
            ).fetchone()[0]
            exp1_cat = con.execute("SELECT category_id FROM expenses WHERE id = 1").fetchone()[0]
            exp2_cat = con.execute("SELECT category_id FROM expenses WHERE id = 2").fetchone()[0]
        finally:
            con.close()

        assert rule_cat == 2
        assert exp1_cat == 2
        assert exp2_cat == 2

    def test_approve_rule_with_no_linked_expenses(self, client, db):  # noqa: ARG002
        con = storage.get_connection()
        try:
            con.execute("INSERT OR IGNORE INTO shop_chains (id, name) VALUES (1, 'Lidl')")
            con.execute("INSERT INTO stores (id, name, chain_id, pib) VALUES (1, 'Lidl', 1, '100')")
            con.execute(
                "INSERT INTO receipts (id, client_receipt_id, url, store_id)"
                " VALUES (1, 'r1', 'https://x', 1)"
            )
            con.execute(
                "INSERT INTO classification_rules"
                " (id, chain_id, item_name_normalized, category_id, confidence_level, source)"
                " VALUES (10, 1, 'mleko', 1, 3, 'llm')"
            )
        finally:
            con.close()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})
        assert resp.status_code == 200
        assert resp.json()["updated_expenses_count"] == 0

    def test_approve_nonexistent_rule_returns_404(self, client, db):  # noqa: ARG002
        resp = client.patch("/api/rules/999/category", json={"category_id": 1})
        assert resp.status_code == 404

    def test_approve_rule_inactive_category_returns_422(self, client, db):  # noqa: ARG002
        con = storage.get_connection()
        try:
            _seed(con)
        finally:
            con.close()

        resp = client.patch("/api/rules/10/category", json={"category_id": 3})
        assert resp.status_code == 422


@allure.epic("Review & Rules")
@allure.feature("Model quality")
class TestApproveRuleRatesModel:
    """The endpoint must hand the rating to the broker — the decision logic is
    unit-tested elsewhere, what matters here is that the route is wired to it.
    """

    def _seed_llm_rule(self, con, *, llm_name="groq", alternatives="[2]"):
        _seed(con)
        con.execute(
            "UPDATE classification_rules SET llm_name = ?, alternative_category_ids = ?"
            " WHERE id = 10",
            [llm_name, alternatives],
        )

    def test_rating_reaches_the_broker(self, client, db):  # noqa: ARG002
        calls: list[tuple] = []

        class _Broker:
            async def record_quality(self, name, operation, score):
                calls.append((name, operation, score))

        con = storage.get_connection()
        try:
            self._seed_llm_rule(con)
        finally:
            con.close()
        client.app.state.llms = _Broker()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})

        assert resp.status_code == 200
        assert calls == [("groq", "receipt_classification", 0.5)]

    def test_rule_without_model_rates_nothing(self, client, db):  # noqa: ARG002
        calls: list[tuple] = []

        class _Broker:
            async def record_quality(self, name, operation, score):
                calls.append((name, operation, score))

        con = storage.get_connection()
        try:
            _seed(con)
        finally:
            con.close()
        client.app.state.llms = _Broker()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})

        assert resp.status_code == 200
        assert calls == []

    def test_broker_failure_does_not_fail_the_correction(self, client, db):  # noqa: ARG002
        class _Broker:
            async def record_quality(self, name, operation, score):
                raise RuntimeError("telemetry down")

        con = storage.get_connection()
        try:
            self._seed_llm_rule(con)
        finally:
            con.close()
        client.app.state.llms = _Broker()

        resp = client.patch("/api/rules/10/category", json={"category_id": 2})

        assert resp.status_code == 200
        con = storage.get_connection()
        try:
            rule = con.execute(
                "SELECT category_id, source FROM classification_rules WHERE id = 10"
            ).fetchone()
        finally:
            con.close()
        assert rule[0] == 2
        assert rule[1] == "user_correction"
