"""Category correction business logic."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from fastapi import HTTPException
from pydantic import BaseModel

from dinary.api.controllers.correction_ratings import pending_rating_for_item
from dinary.db.catalog import activate_category
from dinary.db.classification_rules import RuleSpec, create_or_update_rule
from dinary.db.storage import transaction

logger = logging.getLogger(__name__)


class CorrectionScope(StrEnum):
    single = "single"
    month = "month"
    year = "year"
    all = "all"


class CategoryCorrectionRequest(BaseModel):
    category_id: int
    scope: CorrectionScope = CorrectionScope.all


class CategoryCorrectionResponse(BaseModel):
    corrected_expense_id: int
    batch_updated_count: int


def _query_other_items(
    con: sqlite3.Connection,
    name_norm: str,
    chain_id: int | None,
    expense_id: int,
    since: str | None,
) -> list:
    return con.execute(
        """
        SELECT DISTINCT ri.expense_id
          FROM receipt_items ri
          JOIN receipts rec ON rec.id = ri.receipt_id
          LEFT JOIN stores s ON s.id = rec.store_id
         WHERE ri.name_normalized = ?
           AND (
               (? IS NOT NULL AND s.chain_id = ?)
               OR (? IS NULL AND rec.store_id IS NULL)
           )
           AND ri.expense_id != ?
           AND ri.expense_id IS NOT NULL
           AND (? IS NULL OR rec.created_at >= ?)
        """,
        [name_norm, chain_id, chain_id, chain_id, expense_id, since, since],
    ).fetchall()


def _since_for_scope(scope: CorrectionScope) -> str | None:
    now = datetime.now(UTC)
    if scope == CorrectionScope.month:
        return (now - timedelta(days=30)).isoformat()
    if scope == CorrectionScope.year:
        return datetime(now.year, 1, 1, tzinfo=UTC).isoformat()
    return None


def _chain_id_for_store(con: sqlite3.Connection, store_id: int | None) -> int | None:
    if store_id is None:
        return None
    row = con.execute("SELECT chain_id FROM stores WHERE id = ?", [store_id]).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _upsert_rule_in_tx(
    con: sqlite3.Connection,
    chain_id: int | None,
    item_name_normalized: str,
    category_id: int,
) -> None:
    create_or_update_rule(
        con,
        chain_id,
        item_name_normalized,
        RuleSpec(category_id, 4, "user_correction"),
    )


def _validate_category_for_correction(con: sqlite3.Connection, category_id: int) -> None:
    cat_row = con.execute(
        "SELECT code, is_active, is_hidden, is_retired FROM categories WHERE id = ?",
        [category_id],
    ).fetchone()
    if cat_row is None:
        raise HTTPException(status_code=422, detail=f"Unknown category_id: {category_id}")
    if cat_row["is_retired"]:
        raise HTTPException(status_code=422, detail=f"Retired category_id: {category_id}")
    if cat_row["is_hidden"]:
        raise HTTPException(status_code=422, detail=f"Hidden category_id: {category_id}")
    if not cat_row["is_active"]:
        activate_category(con, cat_row["code"])


def correct_category_sync(
    expense_id: int,
    req: CategoryCorrectionRequest,
    con: sqlite3.Connection,
    skip_rule: bool = False,
    pending_ratings: list[tuple[str, float]] | None = None,
) -> CategoryCorrectionResponse:
    row = con.execute(
        "SELECT receipt_id, store_id FROM expenses WHERE id = ?",
        [expense_id],
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Expense not found")

    _validate_category_for_correction(con, req.category_id)

    receipt_id = row[0]
    store_id = row[1]
    chain_id = _chain_id_for_store(con, store_id)

    with transaction(con):
        con.execute(
            "UPDATE expenses"
            " SET category_id = ?,"
            # manual expenses have no receipt_id — leave their confidence_level untouched
            " confidence_level = CASE WHEN receipt_id IS NOT NULL"
            "                         THEN 4"
            "                         ELSE confidence_level END"
            " WHERE id = ?",
            [req.category_id, expense_id],
        )

        item_names: list[str] = []
        if receipt_id is not None:
            items = con.execute(
                "SELECT name_normalized FROM receipt_items WHERE expense_id = ?",
                [expense_id],
            ).fetchall()
            for (name_norm,) in items:
                if name_norm:
                    item_names.append(name_norm)

        since = _since_for_scope(req.scope)
        batch_count = 0
        for name_norm in set(item_names):
            if not skip_rule:
                # Read the model verdict before the upsert flips source to
                # 'user_correction'; that flip is what dedups a second correction.
                rating = pending_rating_for_item(
                    con,
                    chain_id,
                    name_norm,
                    req.category_id,
                )
                if rating is not None and pending_ratings is not None:
                    pending_ratings.append(rating)
                _upsert_rule_in_tx(con, chain_id, name_norm, req.category_id)

            if req.scope == CorrectionScope.single:
                continue

            other_expense_ids = _query_other_items(con, name_norm, chain_id, expense_id, since)
            for (old_exp_id,) in other_expense_ids:
                con.execute(
                    "UPDATE expenses SET category_id = ?, confidence_level = 4 WHERE id = ?",
                    [req.category_id, old_exp_id],
                )
                batch_count += 1

    return CategoryCorrectionResponse(
        corrected_expense_id=expense_id,
        batch_updated_count=batch_count,
    )
