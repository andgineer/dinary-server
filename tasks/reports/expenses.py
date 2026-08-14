"""Aggregated expense viewer grouped by the 3D classification coord
(category, event, tags). Strictly read-only; safe against live production data —
SQLite's WAL mode lets readers and the single writer coexist without blocking."""

import argparse
import csv
import dataclasses
import json
import sqlite3
import sys
from collections.abc import Iterable
from decimal import Decimal
from typing import TextIO

from rich.console import Console
from rich.table import Table

from dinary.config import settings
from dinary.db import storage


@dataclasses.dataclass(frozen=True, slots=True)
class ExpenseSummaryRow:
    """One row of the aggregation: a unique 3D coord + its totals."""

    category: str
    event: str
    tags: str
    rows: int
    total: Decimal


#: Column order used by both the CSV writer and the rich-table builder.
#: Kept module-level so the two renderers cannot drift.
COLUMNS: tuple[str, ...] = ("category", "event", "tags", "rows", "total")

#: Invariants of the ``YYYY-MM`` CLI flag format. Extracted as
#: constants so ``parse_month`` reads declaratively (and to keep
#: ruff's PLR2004 "magic value" check from flagging the literals).
_MONTH_PARTS = 2
_MONTH_MIN = 1
_MONTH_MAX = 12

#: Static SQL body for ``aggregate_expenses``. Only the ``{where}``
#: placeholder is filled with one of three hardcoded fragments from
#: ``_build_filter`` — never with caller-supplied data — and all
#: year/month values travel through real positional bind parameters.
_EXPENSES_AGGREGATION_SQL = """
    WITH tagged AS (
        SELECT
            e.category_id,
            e.event_id,
            e.amount,
            COALESCE(
                (SELECT group_concat(t.name, ', ')
                 FROM (
                     SELECT t.name
                     FROM expense_tags et
                     JOIN tags t ON t.id = et.tag_id
                     WHERE et.expense_id = e.id
                     ORDER BY t.name
                 ) t),
                ''
            ) AS tags_joined
        FROM expenses e
        {where}
    )
    SELECT
        c.name AS category,
        COALESCE(ev.name, '') AS event,
        t.tags_joined AS tags,
        COUNT(*) AS rows,
        SUM(t.amount) AS total
    FROM tagged t
    JOIN categories c ON c.id = t.category_id
    LEFT JOIN events ev ON ev.id = t.event_id
    GROUP BY c.name, COALESCE(ev.name, ''), t.tags_joined
    ORDER BY total DESC, category ASC, event ASC, tags ASC
"""


def parse_month(value: str) -> tuple[int, int]:
    """Parse ``YYYY-MM`` into ``(year, month)``.

    Rejected inputs raise ``argparse.ArgumentTypeError`` so the CLI
    prints a clean usage message rather than a stack trace.
    """
    parts = value.split("-")
    if len(parts) != _MONTH_PARTS:
        msg = f"--month expects YYYY-MM, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError as exc:
        msg = f"--month expects numeric YYYY-MM, got {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if not _MONTH_MIN <= month <= _MONTH_MAX:
        msg = f"--month month component must be in {_MONTH_MIN}..{_MONTH_MAX}, got {month}"
        raise argparse.ArgumentTypeError(msg)
    return year, month


def _build_filter(
    year: int | None,
    month: tuple[int, int] | None,
) -> tuple[str, list[object]]:
    """Both sides bind as integers (matching ``get_month_expenses.sql``'s cast
    ``strftime`` output) so the query planner avoids a per-row string coercion."""
    if month is not None:
        return (
            "WHERE CAST(strftime('%Y', e.datetime) AS INTEGER) = ?"
            " AND CAST(strftime('%m', e.datetime) AS INTEGER) = ?",
            [month[0], month[1]],
        )
    if year is not None:
        return "WHERE CAST(strftime('%Y', e.datetime) AS INTEGER) = ?", [year]
    return "", []


def aggregate_expenses(
    con: sqlite3.Connection,
    *,
    year: int | None = None,
    month: tuple[int, int] | None = None,
) -> list[ExpenseSummaryRow]:
    """Tags are joined via a pre-sorted ``group_concat`` so the same tag set in a
    different insert order still collapses into one summary row. Sorted by
    ``total DESC`` then ``(category, event, tags)`` ASC for deterministic ties."""
    where, params = _build_filter(year, month)
    # S608: `where` is one of three hardcoded fragments, never user data.
    sql = _EXPENSES_AGGREGATION_SQL.format(where=where)
    rows = con.execute(sql, params).fetchall()
    return [
        ExpenseSummaryRow(
            category=str(row[0]),
            event=str(row[1]),
            tags=str(row[2]),
            rows=int(row[3]),
            total=Decimal(str(row[4])),
        )
        for row in rows
    ]


def _format_amount(value: Decimal) -> str:
    """Render an amount with thousands separators and two decimal places."""
    return f"{value:,.2f}"


def render_rich(
    rows: list[ExpenseSummaryRow],
    *,
    currency: str,
    title_suffix: str,
    stream: TextIO | None = None,
) -> None:
    """Depending on ``rich`` at module level is safe: this dev-only tool is never
    imported by the FastAPI runtime."""
    console = Console(file=stream)
    title = f"Expenses by 3D coord — {title_suffix} ({currency})"
    table = Table(title=title, show_lines=False)
    table.add_column("Category", style="cyan")
    table.add_column("Event", style="magenta")
    table.add_column("Tags")
    table.add_column("Rows", justify="right", style="green")
    table.add_column(f"Total ({currency})", justify="right", style="bold")

    total_sum = Decimal(0)
    total_count = 0
    for r in rows:
        table.add_row(r.category, r.event, r.tags, str(r.rows), _format_amount(r.total))
        total_sum += r.total
        total_count += r.rows

    if rows:
        table.add_section()
        table.add_row(
            "TOTAL",
            "",
            "",
            str(total_count),
            _format_amount(total_sum),
            style="bold white",
        )

    console.print(table)
    if not rows:
        console.print("[dim](no matching expenses)[/dim]")


def render_csv(rows: list[ExpenseSummaryRow], *, stream: TextIO) -> None:
    """Emit the summary as CSV (header + one row per 3D coord)."""
    writer = csv.writer(stream)
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow((r.category, r.event, r.tags, r.rows, f"{r.total:.2f}"))


def render_json(rows: Iterable[ExpenseSummaryRow], *, stream: TextIO) -> None:
    """Wire format for ``inv report-expenses --prod`` (see
    :func:`dinary.reports.income.render_json` for the shared Decimal/UTF-8
    rationale). ``ensure_ascii=False`` avoids a ~6x payload blow-up from
    ``\\uXXXX``-escaping the routinely non-ASCII category/event/tag text."""
    payload = [
        {
            "category": r.category,
            "event": r.event,
            "tags": r.tags,
            "rows": r.rows,
            "total": format(r.total, "f"),
        }
        for r in rows
    ]
    json.dump(payload, stream, ensure_ascii=False)
    stream.write("\n")


def rows_from_json(payload: list[dict]) -> list[ExpenseSummaryRow]:
    """Inverse of :func:`render_json`; tolerant of both int/str and Decimal/str
    encodings so the payload survives round-tripping through ``jq -r``/``yq``."""
    return [
        ExpenseSummaryRow(
            category=str(entry["category"]),
            event=str(entry["event"]),
            tags=str(entry["tags"]),
            rows=int(entry["rows"]),
            total=Decimal(str(entry["total"])),
        )
        for entry in payload
    ]


def _title_suffix(year: int | None, month: tuple[int, int] | None) -> str:
    if month is not None:
        return f"{month[0]:04d}-{month[1]:02d}"
    if year is not None:
        return str(year)
    return "all time"


def render(
    rows: list[ExpenseSummaryRow],
    *,
    year: int | None = None,
    month: tuple[int, int] | None = None,
    as_csv: bool = False,
    as_json: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Shared by :func:`run` and the remote SSH/JSON transport. ``year``/``month``
    are cosmetic (rich table title only) — filtering already happened upstream."""
    if as_csv and as_json:
        msg = "--csv and --json are mutually exclusive"
        raise ValueError(msg)
    out = stream if stream is not None else sys.stdout
    if as_json:
        render_json(rows, stream=out)
    elif as_csv:
        render_csv(rows, stream=out)
    else:
        render_rich(
            rows,
            currency=settings.accounting_currency,
            title_suffix=_title_suffix(year, month),
            stream=out,
        )


def run(
    *,
    year: int | None,
    month: tuple[int, int] | None,
    as_csv: bool = False,
    as_json: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Headless entry point used by ``main()`` and tests. Returns ``0``
    unconditionally — "no matching expenses" is a valid outcome, not an error."""
    if as_csv and as_json:
        msg = "--csv and --json are mutually exclusive"
        raise ValueError(msg)
    if not storage.DB_PATH.exists():
        msg = (
            f"DB not found at {storage.DB_PATH}. Either point "
            "DINARY_DATA_PATH at an existing SQLite file, or use "
            "`inv report-expenses --prod` to query the server."
        )
        print(msg, file=sys.stderr)
        return 1

    con = storage.get_connection()
    try:
        rows = aggregate_expenses(con, year=year, month=month)
    finally:
        con.close()

    render(
        rows,
        year=year,
        month=month,
        as_csv=as_csv,
        as_json=as_json,
        stream=stream,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show expenses aggregated by unique (category, event, tags) coord.",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--year", type=int, help="restrict to a single year")
    window.add_argument(
        "--month",
        type=parse_month,
        help="restrict to a single month (YYYY-MM)",
    )
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--csv",
        action="store_true",
        help="emit CSV to stdout instead of a rich table",
    )
    fmt.add_argument(
        "--json",
        action="store_true",
        help=(
            "emit a JSON array of rows to stdout (wire format used by "
            "``inv report-expenses --prod``)"
        ),
    )
    args = parser.parse_args(argv)
    return run(
        year=args.year,
        month=args.month,
        as_csv=args.csv,
        as_json=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())
