"""Receipt repository: DB operations for receipts, items, and drain jobs."""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from dinary.adapters.receipts.types import ParsedReceipt
from dinary.db.storage import transaction


@dataclass(slots=True)
class ReceiptJobRow:
    receipt_id: int
    url: str
    store_name_raw: str
    store_pib_raw: str
    invoice_number: str
    parsed_at: str | None
    used_journal_fallback: bool
    claim_token: str
    retry_count: int = 0


@dataclass(slots=True)
class ReceiptItemRow:
    id: int
    name_raw: str
    name_normalized: str | None
    unit_price: float
    quantity: float
    total_price: float
    tax_label: str
    expense_id: int | None


def insert_receipt(conn: sqlite3.Connection, client_receipt_id: str, url: str) -> int:
    """Insert a bare receipt row (URL only, not yet parsed). Returns receipt_id."""
    conn.execute(
        "INSERT INTO receipts (client_receipt_id, url) VALUES (?, ?)",
        [client_receipt_id, url],
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def get_receipt_by_client_id(
    conn: sqlite3.Connection,
    client_receipt_id: str,
) -> tuple[int, str] | None:
    """Return (receipt_id, url) if the client_receipt_id already exists."""
    row = conn.execute(
        "SELECT id, url FROM receipts WHERE client_receipt_id = ?",
        [client_receipt_id],
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


def insert_job(conn: sqlite3.Connection, receipt_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO receipt_classification_jobs (receipt_id) VALUES (?)",
        [receipt_id],
    )


def claim_next_job(conn: sqlite3.Connection, stale_minutes: int = 10) -> ReceiptJobRow | None:
    """Claim the oldest pending (or stale in_progress) job. Returns None if none available."""
    stale_cutoff = f"datetime('now', '-{stale_minutes} minutes')"
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"""
            SELECT r.id, r.url, r.store_name_raw, r.store_pib_raw,
                   r.invoice_number, r.parsed_at, r.used_journal_fallback, j.retry_count,
                   j.status
              FROM receipt_classification_jobs j
              JOIN receipts r ON r.id = j.receipt_id
             WHERE (
                     (j.status = 'pending'
                      AND (j.retry_after IS NULL OR j.retry_after <= datetime('now')))
                  OR (j.status = 'in_progress' AND j.claimed_at < {stale_cutoff})
                   )
             ORDER BY r.created_at
             LIMIT 1
            """,  # noqa: S608
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        receipt_id = int(row[0])
        token = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        # Stale reclaims count as a retry so crash-looping jobs back off like explicit failures.
        was_stale = str(row[8]) == "in_progress"
        retry_count = int(row[7]) + (1 if was_stale else 0)
        conn.execute(
            """
            UPDATE receipt_classification_jobs
               SET status = 'in_progress', claim_token = ?, claimed_at = ?, retry_count = ?
             WHERE receipt_id = ?
            """,
            [token, now, retry_count, receipt_id],
        )
        result = ReceiptJobRow(
            receipt_id=receipt_id,
            url=str(row[1]),
            store_name_raw=str(row[2]),
            store_pib_raw=str(row[3]),
            invoice_number=str(row[4]),
            parsed_at=str(row[5]) if row[5] else None,
            used_journal_fallback=bool(row[6]),
            claim_token=token,
            retry_count=retry_count,
        )
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def save_parsed_receipt(
    conn: sqlite3.Connection,
    receipt_id: int,
    parsed: ParsedReceipt,
) -> None:
    """Store parsed receipt metadata and items atomically."""
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute(
            """
            UPDATE receipts
               SET store_name_raw = ?, store_pib_raw = ?, total_amount = ?,
                   invoice_number = ?, purchase_datetime = ?,
                   parsed_at = ?, used_journal_fallback = ?
             WHERE id = ?
            """,
            [
                parsed.store_name,
                parsed.store_pib,
                parsed.total_amount,
                parsed.invoice_number,
                parsed.purchase_datetime,
                now,
                1 if parsed.used_journal_fallback else 0,
                receipt_id,
            ],
        )
        for item in parsed.items:
            conn.execute(
                """
                INSERT INTO receipt_items
                       (receipt_id, name_raw, unit_price, quantity, total_price, tax_label)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    receipt_id,
                    item.name_raw,
                    item.unit_price,
                    item.quantity,
                    item.total_price,
                    item.tax_label,
                ],
            )


def get_receipt_items(conn: sqlite3.Connection, receipt_id: int) -> list[ReceiptItemRow]:
    rows = conn.execute(
        """
        SELECT id, name_raw, name_normalized, unit_price, quantity, total_price,
               tax_label, expense_id
          FROM receipt_items
         WHERE receipt_id = ?
         ORDER BY id
        """,
        [receipt_id],
    ).fetchall()
    return [
        ReceiptItemRow(
            id=int(r[0]),
            name_raw=str(r[1]),
            name_normalized=str(r[2]) if r[2] else None,
            unit_price=float(r[3]),
            quantity=float(r[4]),
            total_price=float(r[5]),
            tax_label=str(r[6]),
            expense_id=int(r[7]) if r[7] is not None else None,
        )
        for r in rows
    ]


def update_receipt_item(
    conn: sqlite3.Connection,
    item_id: int,
    name_normalized: str,
    expense_id: int | None,
) -> None:
    conn.execute(
        """
        UPDATE receipt_items
           SET name_normalized = ?, expense_id = ?
         WHERE id = ?
        """,
        [name_normalized, expense_id, item_id],
    )


def complete_job(conn: sqlite3.Connection, receipt_id: int) -> None:
    conn.execute(
        "DELETE FROM receipt_classification_jobs WHERE receipt_id = ?",
        [receipt_id],
    )


def poison_job(conn: sqlite3.Connection, receipt_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE receipt_classification_jobs
           SET status = 'poisoned', last_error = ?
         WHERE receipt_id = ?
        """,
        [error[:2000], receipt_id],
    )


def release_job(
    conn: sqlite3.Connection,
    receipt_id: int,
    claim_token: str,
    retry_count: int,
    retry_after: str | None,
    last_error: str | None = None,
) -> None:
    """Release a claimed job back to 'pending' for retry.

    Matches on claim_token so a stale release from a previous attempt cannot
    accidentally reset a job that has already been re-claimed by a new worker.
    """
    conn.execute(
        """
        UPDATE receipt_classification_jobs
           SET status = 'pending', claim_token = NULL, claimed_at = NULL,
               retry_count = ?, retry_after = ?, last_error = ?
         WHERE receipt_id = ? AND claim_token = ?
        """,
        [retry_count, retry_after, last_error, receipt_id, claim_token],
    )


def requeue_receipts(
    conn: sqlite3.Connection,
    receipt_ids: list[int],
    clear_rules: bool = False,
) -> None:
    """Reset classification state and re-queue jobs for the given receipt IDs."""
    if not receipt_ids:
        return
    placeholders = ",".join("?" * len(receipt_ids))
    # Clear FK reference on items before deleting parent expenses.
    conn.execute(
        f"""
        UPDATE receipt_items
           SET expense_id = NULL
         WHERE receipt_id IN ({placeholders})
        """,  # noqa: S608
        receipt_ids,
    )
    conn.execute(
        f"DELETE FROM expenses WHERE receipt_id IN ({placeholders})",  # noqa: S608
        receipt_ids,
    )
    if clear_rules:
        # Delete rules scoped to the chains of the target receipts, plus
        # generic rules (chain_id IS NULL) that map names found in those receipts.
        scoped_items = conn.execute(
            f"SELECT DISTINCT ri.name_normalized, s.chain_id"  # noqa: S608
            f"  FROM receipt_items ri"
            f"  JOIN receipts rec ON rec.id = ri.receipt_id"
            f"  LEFT JOIN stores s ON s.id = rec.store_id"
            f" WHERE ri.receipt_id IN ({placeholders})"
            f"   AND ri.name_normalized IS NOT NULL",
            receipt_ids,
        ).fetchall()
        for name, item_chain_id in scoped_items:
            conn.execute(
                "DELETE FROM classification_rules"
                " WHERE item_name_normalized = ?"
                "   AND (chain_id IS NULL OR chain_id IS ?)",
                [name, item_chain_id],
            )
    conn.execute(
        f"""
        INSERT INTO receipt_classification_jobs (receipt_id)
             SELECT id FROM receipts WHERE id IN ({placeholders})
        ON CONFLICT(receipt_id) DO UPDATE
               SET status = 'pending', retry_count = 0, retry_after = NULL
        """,  # noqa: S608
        receipt_ids,
    )


def classification_job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return per-state counts for receipt_classification_jobs."""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'pending'
                          AND (retry_after IS NULL OR retry_after <= datetime('now'))
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'pending' AND retry_after > datetime('now')
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'poisoned' THEN 1 ELSE 0 END)
          FROM receipt_classification_jobs
        """,
    ).fetchone()
    return {
        "pending": int(row[0] or 0),
        "sleeping": int(row[1] or 0),
        "in_progress": int(row[2] or 0),
        "poisoned": int(row[3] or 0),
    }


def count_pending_classification_jobs(conn: sqlite3.Connection) -> int:
    """Return the number of jobs that are active or immediately claimable."""
    return conn.execute(
        "SELECT COUNT(*) FROM receipt_classification_jobs"
        " WHERE (status = 'pending' AND (retry_after IS NULL OR retry_after <= datetime('now')))"
        "    OR status = 'in_progress'",
    ).fetchone()[0]


def get_receipt_summary(conn: sqlite3.Connection, receipt_id: int) -> dict | None:
    """Return receipt metadata + associated expenses for the cascade-confirm UI.

    Returns None if the receipt does not exist.
    """
    row = conn.execute(
        """
        SELECT r.id, r.store_name_raw AS merchant, r.purchase_datetime AS captured_at,
               j.status, j.retry_count, j.last_error, j.retry_after, j.claimed_at
          FROM receipts r
          LEFT JOIN receipt_classification_jobs j ON j.receipt_id = r.id
         WHERE r.id = ?
        """,
        [receipt_id],
    ).fetchone()
    if row is None:
        return None

    job = None
    if row["status"] is not None:
        job = {
            "status": str(row["status"]),
            "retry_count": int(row["retry_count"]),
            "last_error": str(row["last_error"]) if row["last_error"] else None,
            "retry_after": (
                str(row["retry_after"])
                if row["status"] == "pending" and row["retry_after"]
                else None
            ),
            "last_attempted_at": str(row["claimed_at"]) if row["claimed_at"] else None,
        }

    expense_rows = conn.execute(
        """
        SELECT e.id, ri.name_raw AS item_name, e.amount_original AS amount,
               e.currency_original AS currency
          FROM expenses e
          LEFT JOIN receipt_items ri ON ri.expense_id = e.id
         WHERE e.receipt_id = ?
         ORDER BY e.id
        """,
        [receipt_id],
    ).fetchall()

    expenses = [
        {
            "id": int(r["id"]),
            "item_name": str(r["item_name"]) if r["item_name"] else "",
            "amount": float(r["amount"]),
            "currency": str(r["currency"]),
        }
        for r in expense_rows
    ]
    total = sum(float(e["amount"]) for e in expenses)
    currency = expenses[0]["currency"] if expenses else ""

    return {
        "id": int(row["id"]),
        "merchant": str(row["merchant"]) if row["merchant"] else "",
        "captured_at": str(row["captured_at"]) if row["captured_at"] else None,
        "expenses": expenses,
        "total": {"amount": total, "currency": currency},
        "job": job,
    }


def delete_receipt_cascade(conn: sqlite3.Connection, receipt_id: int) -> None:
    """Delete a receipt and all its expenses (cascade). Idempotent."""
    with transaction(conn):
        # expense_tags and sheet_logging_jobs (from migration 0001) have no ON DELETE action;
        # all 0004 child tables are handled automatically by CASCADE / SET NULL.
        conn.execute(
            "DELETE FROM expense_tags WHERE expense_id IN "
            "(SELECT id FROM expenses WHERE receipt_id = ?)",
            [receipt_id],
        )
        conn.execute(
            "DELETE FROM sheet_logging_jobs WHERE expense_id IN "
            "(SELECT id FROM expenses WHERE receipt_id = ?)",
            [receipt_id],
        )
        conn.execute("DELETE FROM expenses WHERE receipt_id = ?", [receipt_id])
        conn.execute("DELETE FROM receipts WHERE id = ?", [receipt_id])
