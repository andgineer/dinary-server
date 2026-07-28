"""Expense creation and editing business logic."""

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field

from dinary.adapters.rates.service import convert_to_accounting_amount
from dinary.api.controllers.catalog import (
    FrequentCategory,
    frequent_categories_sync,
    most_used_category_per_group,
    most_used_group,
)
from dinary.api.controllers.correction_ratings import pending_rating_for_rule
from dinary.api.controllers.expense_corrections import (
    CategoryCorrectionRequest,
    CorrectionScope,
    correct_category_sync,
)
from dinary.api.http_errors import value_error_as_422
from dinary.config import settings
from dinary.db.catalog import activate_category, get_catalog_version
from dinary.db.expenses import (
    ExpensePayload,
    describe_expense_conflict,
    insert_expense,
    lookup_existing_expense,
)
from dinary.db.storage import transaction
from dinary.sheets import sheet_mapping

logger = logging.getLogger(__name__)


class ExpenseRequest(BaseModel):
    client_expense_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=Decimal(0))
    currency: str | None = None
    category_id: int
    event_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    comment: str | None = None
    expense_datetime: datetime


class ExpenseResponse(BaseModel):
    status: Literal["ok", "duplicate"]
    month: str
    category_id: int
    amount_original: Decimal
    currency_original: str
    catalog_version: int
    default_group_id: int | None = None
    default_category_ids: dict[str, int] = Field(default_factory=dict)
    frequent_categories: list[FrequentCategory] = Field(default_factory=list)


class ExpenseEditRequest(BaseModel):
    category_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    event_id: int | None = None
    clear_event: bool = False
    scope: CorrectionScope = CorrectionScope.single
    update_rule: bool = False
    amount_original: Decimal | None = None
    currency_original: str | None = None
    comment: str | None = None


class ExpenseListTag(BaseModel):
    id: int
    name: str


class ExpenseListItem(BaseModel):
    id: int
    datetime: str
    category_id: int
    category_name: str
    event_id: int | None
    event_name: str | None
    store_id: int | None
    store_name: str | None
    receipt_id: int | None
    confidence_level: int | None
    tags: list[ExpenseListTag]
    has_rule: bool
    rule_id: int | None
    item_name: str | None = None
    amount_original: float
    currency_original: str
    comment: str | None = None


def create_expense_sync(req: ExpenseRequest, con: sqlite3.Connection) -> ExpenseResponse:
    currency = req.currency or settings.app_currency

    _resolve_category_for_write(con, req)
    _validate_event(con, req)
    _validate_tags(con, req)

    expense_dt = req.expense_datetime.astimezone(ZoneInfo(settings.user_timezone))

    with value_error_as_422():
        amount_acc = convert_to_accounting_amount(con, req.amount, currency, expense_dt.date())

    effective_tag_ids = sheet_mapping.resolve_effective_tag_ids(con, req.tag_ids, req.event_id)

    amount_acc_f = float(amount_acc)
    amount_orig_f = float(req.amount)
    comment = req.comment or ""
    with value_error_as_422():
        result = insert_expense(
            con,
            ExpensePayload(
                client_expense_id=req.client_expense_id,
                expense_datetime=expense_dt,
                amount=amount_acc_f,
                amount_original=amount_orig_f,
                currency_original=currency,
                category_id=req.category_id,
                event_id=req.event_id,
                comment=comment,
                sheet_category=None,
                sheet_group=None,
                tag_ids=effective_tag_ids,
            ),
            enqueue_logging=settings.sheet_logging_enabled,
        )

    if result == "conflict":
        diff = describe_expense_conflict(
            con,
            ExpensePayload(
                client_expense_id=req.client_expense_id,
                expense_datetime=expense_dt,
                amount=amount_acc_f,
                amount_original=amount_orig_f,
                currency_original=currency,
                category_id=req.category_id,
                event_id=req.event_id,
                comment=comment,
                sheet_category=None,
                sheet_group=None,
                tag_ids=effective_tag_ids,
            ),
        )
        detail = f"client_expense_id {req.client_expense_id!r} already exists with different data"
        if diff:
            detail = f"{detail}: {diff}"
        raise HTTPException(status_code=409, detail=detail)

    catalog_version = get_catalog_version(con)
    default_group_id = most_used_group(con)
    category_defaults = most_used_category_per_group(con)
    freq_cats = frequent_categories_sync(con)

    return ExpenseResponse(
        status="ok" if result == "created" else "duplicate",
        month=expense_dt.strftime("%Y-%m"),
        category_id=req.category_id,
        amount_original=req.amount,
        currency_original=currency,
        catalog_version=catalog_version,
        default_group_id=default_group_id,
        default_category_ids={str(k): v for k, v in category_defaults.items()},
        frequent_categories=freq_cats,
    )


def list_expenses_sync(
    con: sqlite3.Connection,
    page: int,
    page_size: int,
) -> dict:
    offset = (page - 1) * page_size
    total = con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
    rows = con.execute(
        """
        SELECT
            e.id,
            e.datetime,
            e.category_id,
            c.name          AS category_name,
            e.event_id,
            ev.name         AS event_name,
            e.store_id,
            sc.name         AS store_name,
            e.receipt_id,
            e.confidence_level,
            e.rule_id,
            e.amount_original,
            e.currency_original,
            e.comment,
            COALESCE((
                SELECT json_group_array(json_object('id', t.id, 'name', t.name))
                  FROM expense_tags et JOIN tags t ON t.id = et.tag_id
                 WHERE et.expense_id = e.id
            ), '[]') AS tags_json,
            (SELECT ri.name_raw FROM receipt_items ri
              WHERE ri.expense_id = e.id LIMIT 1) AS item_name
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        LEFT JOIN events ev ON ev.id = e.event_id
        LEFT JOIN stores s ON s.id = e.store_id
        LEFT JOIN shop_chains sc ON sc.id = s.chain_id
        LEFT JOIN receipts rec ON rec.id = e.receipt_id
        ORDER BY e.datetime DESC, e.id DESC
        LIMIT ? OFFSET ?
        """,
        [page_size, offset],
    ).fetchall()

    items = []
    for r in rows:
        try:
            raw_tags = json.loads(r["tags_json"]) if r["tags_json"] else []
            tags = [ExpenseListTag(id=int(t["id"]), name=str(t["name"])) for t in raw_tags]
        except (json.JSONDecodeError, KeyError, TypeError):
            tags = []
        items.append(
            ExpenseListItem(
                id=int(r["id"]),
                datetime=str(r["datetime"]),
                category_id=int(r["category_id"]),
                category_name=str(r["category_name"]),
                event_id=int(r["event_id"]) if r["event_id"] is not None else None,
                event_name=str(r["event_name"]) if r["event_name"] is not None else None,
                store_id=int(r["store_id"]) if r["store_id"] is not None else None,
                store_name=str(r["store_name"]) if r["store_name"] is not None else None,
                receipt_id=int(r["receipt_id"]) if r["receipt_id"] is not None else None,
                confidence_level=int(r["confidence_level"])
                if r["confidence_level"] is not None
                else None,
                tags=tags,
                rule_id=int(r["rule_id"]) if r["rule_id"] is not None else None,
                has_rule=r["rule_id"] is not None,
                item_name=str(r["item_name"]) if r["item_name"] is not None else None,
                amount_original=float(r["amount_original"]),
                currency_original=str(r["currency_original"]),
                comment=str(r["comment"]) if r["comment"] else None,
            ),
        )
    return {
        "items": items,
        "has_more": offset + page_size < total,
    }


def _update_amount(
    con: sqlite3.Connection,
    expense_id: int,
    amount_original: Decimal,
    currency_original: str | None,
) -> None:
    currency = currency_original or settings.app_currency
    exp_row = con.execute(
        "SELECT datetime FROM expenses WHERE id = ?",
        [expense_id],
    ).fetchone()
    if exp_row is None:
        return
    expense_date = datetime.fromisoformat(str(exp_row[0])).date()
    with value_error_as_422():
        amount_acc = convert_to_accounting_amount(con, amount_original, currency, expense_date)
    con.execute(
        "UPDATE expenses SET amount_original = ?, currency_original = ?, amount = ? WHERE id = ?",
        [float(amount_original), currency, float(amount_acc), expense_id],
    )


def delete_expense_sync(expense_id: int, con: sqlite3.Connection) -> None:
    """Delete a manual expense. Raises 404 if not found, 409 if receipt-backed."""
    row = con.execute(
        "SELECT id, receipt_id FROM expenses WHERE id = ?",
        [expense_id],
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Expense not found")
    if row[1] is not None:
        raise HTTPException(
            status_code=409,
            detail="Receipt-backed expenses must be deleted via DELETE /api/receipts/:id",
        )
    con.execute("DELETE FROM expense_tags WHERE expense_id = ?", [expense_id])
    con.execute("DELETE FROM expenses WHERE id = ?", [expense_id])


def edit_expense_sync(
    expense_id: int,
    req: ExpenseEditRequest,
    con: sqlite3.Connection,
    pending_ratings: list[tuple[str, float]] | None = None,
) -> None:
    row = con.execute(
        "SELECT id, receipt_id FROM expenses WHERE id = ?",
        [expense_id],
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Expense not found")

    receipt_id = row[1]

    if req.category_id is not None:
        correct_category_sync(
            expense_id,
            CategoryCorrectionRequest(category_id=req.category_id, scope=req.scope),
            con,
            skip_rule=req.update_rule,
            pending_ratings=pending_ratings,
        )

    with transaction(con):
        if req.amount_original is not None and receipt_id is None:
            _update_amount(con, expense_id, req.amount_original, req.currency_original)
        if req.comment is not None:
            con.execute(
                "UPDATE expenses SET comment = ? WHERE id = ?",
                [req.comment, expense_id],
            )
        con.execute("DELETE FROM expense_tags WHERE expense_id = ?", [expense_id])
        for tag_id in req.tag_ids:
            con.execute(
                "INSERT OR IGNORE INTO expense_tags (expense_id, tag_id) VALUES (?, ?)",
                [expense_id, tag_id],
            )

        if req.clear_event:
            con.execute("UPDATE expenses SET event_id = NULL WHERE id = ?", [expense_id])
        elif req.event_id is not None:
            con.execute(
                "UPDATE expenses SET event_id = ? WHERE id = ?",
                [req.event_id, expense_id],
            )

        if req.update_rule:
            _apply_rule_update(con, expense_id, req.category_id, req.tag_ids, pending_ratings)


def _apply_rule_update(
    con: sqlite3.Connection,
    expense_id: int,
    category_id: int | None,
    tag_ids: list[int],
    pending_ratings: list[tuple[str, float]] | None = None,
) -> None:
    row = con.execute(
        "SELECT rule_id, category_id FROM expenses WHERE id = ?",
        [expense_id],
    ).fetchone()
    if row is None or row[0] is None:
        logger.error(
            "update_rule=True for expense_id=%s but rule_id is not set — skipping",
            expense_id,
        )
        return
    effective_category_id = category_id if category_id is not None else int(row[1])
    rule_id = int(row[0])
    # This write is what flips the rule away from its llm origin, so the model
    # verdict has to be read here — ``correct_category_sync`` ran in skip_rule
    # mode and left the rule untouched.
    rating = pending_rating_for_rule(con, rule_id, effective_category_id)
    if rating is not None and pending_ratings is not None:
        pending_ratings.append(rating)
    con.execute(
        "UPDATE classification_rules"
        " SET category_id=?, confidence_level=4, source='user_correction', tag_ids=?"
        " WHERE id=?",
        [effective_category_id, json.dumps(tag_ids), rule_id],
    )
    con.execute(
        "UPDATE expenses SET category_id=?, confidence_level=4 WHERE rule_id=?",
        [effective_category_id, rule_id],
    )


def _is_replay(con: sqlite3.Connection, client_expense_id: str) -> bool:
    return lookup_existing_expense(client_expense_id, con=con) is not None


def _resolve_category_for_write(con: sqlite3.Connection, req: ExpenseRequest) -> None:
    row = con.execute(
        "SELECT code, is_active, is_hidden, is_retired FROM categories WHERE id = ?",
        [req.category_id],
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail=f"Unknown category_id: {req.category_id}")
    if row["is_retired"]:
        if _is_replay(con, req.client_expense_id):
            return
        raise HTTPException(status_code=422, detail=f"Retired category_id: {req.category_id}")
    if row["is_hidden"]:
        if _is_replay(con, req.client_expense_id):
            return
        raise HTTPException(status_code=422, detail=f"Hidden category_id: {req.category_id}")
    if not row["is_active"]:
        activate_category(con, row["code"])


def _validate_event(con: sqlite3.Connection, req: ExpenseRequest) -> None:
    if req.event_id is None:
        return
    row = con.execute("SELECT is_active FROM events WHERE id = ?", [req.event_id]).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail=f"Unknown event_id: {req.event_id}")
    if bool(row[0]):
        return
    if _is_replay(con, req.client_expense_id):
        return
    raise HTTPException(status_code=422, detail=f"Inactive event_id: {req.event_id}")


def _validate_tags(con: sqlite3.Connection, req: ExpenseRequest) -> None:
    if not req.tag_ids:
        return
    unique_ids = list({int(t) for t in req.tag_ids})
    placeholders = ",".join(["?"] * len(unique_ids))
    rows = con.execute(
        f"SELECT id, is_active FROM tags WHERE id IN ({placeholders})",  # noqa: S608
        unique_ids,
    ).fetchall()
    found = {int(r[0]): bool(r[1]) for r in rows}
    missing = [t for t in unique_ids if t not in found]
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown tag_ids: {missing}")
    inactive = [t for t in unique_ids if not found[t]]
    if not inactive:
        return
    if _is_replay(con, req.client_expense_id):
        return
    raise HTTPException(status_code=422, detail=f"Inactive tag_ids: {inactive}")
