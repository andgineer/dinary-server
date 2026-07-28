"""Rules feed business logic."""

import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from dinary.api.controllers.correction_ratings import pending_rating_for_rule
from dinary.db.catalog import VISIBLE_CATEGORY_PREDICATE
from dinary.db.receipts import classification_job_counts
from dinary.db.storage import transaction


def count_doubtful(con: sqlite3.Connection) -> int:
    return con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT cr.id
              FROM classification_rules cr
              JOIN receipt_items ri ON ri.name_normalized = cr.item_name_normalized
              JOIN receipts rec ON rec.id = ri.receipt_id
              LEFT JOIN stores s ON s.id = rec.store_id
             WHERE cr.confidence_level < 4
               AND (cr.chain_id IS NULL OR s.chain_id = cr.chain_id)
             GROUP BY cr.id
        )
        """,
    ).fetchone()[0]


def _resolve_ids_to_names(
    con: sqlite3.Connection,
    table: str,
    ids_json: str | None,
) -> list[dict[str, Any]]:
    """Parse a JSON id-array and resolve each to {id, name} via active rows in table."""
    if not ids_json:
        return []
    try:
        ids = [int(i) for i in json.loads(ids_json) if isinstance(i, (int, float))]
    except Exception:  # noqa: BLE001
        return []
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT id, name FROM {table} WHERE id IN ({placeholders}) AND is_active = 1",  # noqa: S608
        ids,
    ).fetchall()
    name_by_id = {int(r[0]): str(r[1]) for r in rows}
    return [{"id": i, "name": name_by_id[i]} for i in ids if i in name_by_id]


def query_rules(
    con: sqlite3.Connection,
    limit: int,
    offset: int,
    *,
    doubtful_only: bool = False,
) -> list[dict[str, Any]]:
    base_query = """
        WITH rule_stats AS (
            SELECT
                cr.id,
                cr.item_name_normalized,
                cr.category_id,
                cr.confidence_level,
                cr.alternative_category_ids,
                cr.tag_ids,
                sc.name                     AS store_chain,
                c.name                      AS category_name,
                SUM(ri.total_price)         AS amount_at_stake,
                COUNT(ri.id)                AS occurrence_count,
                MAX(ri.expense_id)          AS expense_id,
                MAX(e.currency_original)    AS currency,
                MAX(rec.created_at)         AS last_receipt_date
              FROM classification_rules cr
              JOIN categories c   ON c.id = cr.category_id
              LEFT JOIN shop_chains sc ON sc.id = cr.chain_id
              JOIN receipt_items ri ON ri.name_normalized = cr.item_name_normalized
              JOIN receipts rec   ON rec.id = ri.receipt_id
              LEFT JOIN stores rec_s ON rec_s.id = rec.store_id
              LEFT JOIN expenses e ON e.id = ri.expense_id
             WHERE (cr.chain_id IS NULL OR rec_s.chain_id = cr.chain_id)
             GROUP BY cr.id
        )
        SELECT *
          FROM rule_stats
        """
    order_clause = """
         ORDER BY
             (confidence_level < 4) DESC,
             CASE WHEN confidence_level < 4 THEN amount_at_stake ELSE 0 END DESC,
             CASE WHEN confidence_level >= 4 THEN last_receipt_date ELSE '' END DESC
         LIMIT ? OFFSET ?
        """
    if doubtful_only:
        sql = base_query + " WHERE confidence_level < 4 " + order_clause
    else:
        sql = base_query + order_clause
    rows = con.execute(sql, [limit, offset]).fetchall()  # noqa: S608
    return [
        {
            "is_doubtful": bool(r["confidence_level"] < 4),
            "id": int(r["id"]),
            "name": str(r["item_name_normalized"]) if r["item_name_normalized"] else None,
            "store": str(r["store_chain"]) if r["store_chain"] else None,
            "total": float(r["amount_at_stake"] or 0),
            "count": int(r["occurrence_count"]),
            "currency": str(r["currency"]) if r["currency"] else None,
            "confidence_level": int(r["confidence_level"]),
            "category_id": int(r["category_id"]),
            "category_name": str(r["category_name"]),
            "expense_id": int(r["expense_id"]) if r["expense_id"] is not None else None,
            "datetime": str(r["last_receipt_date"]) if r["last_receipt_date"] else None,
            "alternative_categories": _resolve_ids_to_names(
                con,
                "categories",
                r["alternative_category_ids"],
            ),
            "tags": _resolve_ids_to_names(con, "tags", r["tag_ids"]),
        }
        for r in rows
    ]


def confirm_rules_bulk(con: sqlite3.Connection, rule_ids: list[int]) -> int:
    if not rule_ids:
        return 0
    placeholders = ",".join("?" * len(rule_ids))
    with transaction(con):
        con.execute(
            f"UPDATE classification_rules SET confidence_level=4, source='user_correction'"  # noqa: S608
            f" WHERE id IN ({placeholders})",
            rule_ids,
        )
        con.execute(
            f"UPDATE expenses SET confidence_level=4 WHERE rule_id IN ({placeholders})",  # noqa: S608
            rule_ids,
        )
    return len(rule_ids)


def approve_rule_category(
    rule_id: int,
    category_id: int,
    con: sqlite3.Connection,
    pending_ratings: list[tuple[str, float]] | None = None,
) -> dict:
    if (
        con.execute(
            "SELECT id FROM classification_rules WHERE id = ?",
            [rule_id],
        ).fetchone()
        is None
    ):
        raise HTTPException(status_code=404, detail="Rule not found")
    if (
        con.execute(
            f"SELECT c.id FROM categories c WHERE c.id = ? AND {VISIBLE_CATEGORY_PREDICATE}",  # noqa: S608
            [category_id],
        ).fetchone()
        is None
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or inactive category_id: {category_id}",
        )
    with transaction(con):
        # Read the model verdict before the update flips source to
        # 'user_correction'; that flip is what dedups a second correction.
        rating = pending_rating_for_rule(con, rule_id, category_id)
        if rating is not None and pending_ratings is not None:
            pending_ratings.append(rating)
        con.execute(
            "UPDATE classification_rules"
            " SET category_id=?, confidence_level=4, source='user_correction'"
            " WHERE id=?",
            [category_id, rule_id],
        )
        con.execute(
            "UPDATE expenses SET category_id=?, confidence_level=4 WHERE rule_id=?",
            [category_id, rule_id],
        )
        count = con.execute("SELECT changes()").fetchone()[0]
    return {"updated_expenses_count": int(count)}


def build_rules_feed(
    con: sqlite3.Connection,
    page: int,
    page_size: int,
    *,
    doubtful_only: bool = True,
) -> dict[str, Any]:
    offset = (page - 1) * page_size
    d_total = count_doubtful(con)
    effective_total = d_total
    rows = (
        query_rules(con, page_size, offset, doubtful_only=doubtful_only)
        if effective_total > 0
        else []
    )
    return {
        "doubtful_count": d_total,
        "items": rows,
        "has_more": offset + page_size < effective_total,
        "receipts_queue": classification_job_counts(con),
    }
