"""Aggregated income viewer grouped by year. Strictly read-only — no DB writes."""

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
class IncomeSummaryRow:
    """One row of the aggregation: one calendar year."""

    year: int
    months: int
    total: Decimal
    avg_month: Decimal


#: Column order used by both renderers.
COLUMNS: tuple[str, ...] = ("year", "months", "total", "avg_month")


def aggregate_income(con: sqlite3.Connection) -> list[IncomeSummaryRow]:
    """``avg_month`` divides by months-with-data, not by 12 — answers "income
    per month when there was any", useful for spotting gaps in legacy sheets."""
    sql = """
        SELECT
            year,
            COUNT(*) AS months,
            SUM(amount) AS total,
            SUM(amount) / COUNT(*) AS avg_month
        FROM income
        GROUP BY year
        ORDER BY year DESC
    """
    rows = con.execute(sql).fetchall()
    return [
        IncomeSummaryRow(
            year=int(row[0]),
            months=int(row[1]),
            total=Decimal(str(row[2])),
            avg_month=Decimal(str(row[3])),
        )
        for row in rows
    ]


def _format_amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def render_rich(
    rows: list[IncomeSummaryRow],
    *,
    currency: str,
    stream: TextIO | None = None,
) -> None:
    """Depending on ``rich`` at module level is safe: dev-only tooling, outside
    the runtime import graph."""
    console = Console(file=stream)
    table = Table(title=f"Income by year ({currency})", show_lines=False)
    table.add_column("Year", justify="right", style="cyan")
    table.add_column("Months", justify="right")
    table.add_column(f"Total ({currency})", justify="right", style="bold")
    table.add_column(f"Avg / month ({currency})", justify="right")

    total_sum = Decimal(0)
    total_months = 0
    for r in rows:
        table.add_row(
            str(r.year),
            str(r.months),
            _format_amount(r.total),
            _format_amount(r.avg_month),
        )
        total_sum += r.total
        total_months += r.months

    if rows:
        table.add_section()
        overall_avg = total_sum / total_months if total_months else Decimal(0)
        table.add_row(
            "TOTAL",
            str(total_months),
            _format_amount(total_sum),
            _format_amount(overall_avg),
            style="bold white",
        )

    console.print(table)
    if not rows:
        console.print("[dim](no income rows)[/dim]")


def render_csv(rows: list[IncomeSummaryRow], *, stream: TextIO) -> None:
    """Emit the summary as CSV (header + one row per year)."""
    writer = csv.writer(stream)
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow(
            (
                r.year,
                r.months,
                f"{r.total:.2f}",
                f"{r.avg_month:.2f}",
            ),
        )


def render_json(rows: Iterable[IncomeSummaryRow], *, stream: TextIO) -> None:
    """Wire format for ``inv report-income --prod``. ``Decimal`` values serialize
    as canonical strings, not float, to avoid silent precision loss; use
    :func:`rows_from_json` to round-trip bit-exact. ``ensure_ascii=False`` keeps
    Cyrillic fields as UTF-8 on the wire."""
    payload = [
        {
            "year": r.year,
            "months": r.months,
            "total": format(r.total, "f"),
            "avg_month": format(r.avg_month, "f"),
        }
        for r in rows
    ]
    json.dump(payload, stream, ensure_ascii=False)
    stream.write("\n")


def rows_from_json(payload: list[dict]) -> list[IncomeSummaryRow]:
    """Inverse of :func:`render_json` — same object type as the DB path."""
    return [
        IncomeSummaryRow(
            year=int(entry["year"]),
            months=int(entry["months"]),
            total=Decimal(entry["total"]),
            avg_month=Decimal(entry["avg_month"]),
        )
        for entry in payload
    ]


def render(
    rows: list[IncomeSummaryRow],
    *,
    as_csv: bool = False,
    as_json: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Shared by :func:`run` (local SQLite path) and the remote SSH/JSON transport,
    so both paths produce bit-identical output."""
    if as_csv and as_json:
        msg = "--csv and --json are mutually exclusive"
        raise ValueError(msg)
    out = stream if stream is not None else sys.stdout
    if as_json:
        render_json(rows, stream=out)
    elif as_csv:
        render_csv(rows, stream=out)
    else:
        render_rich(rows, currency=settings.accounting_currency, stream=out)


def run(
    *,
    as_csv: bool = False,
    as_json: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Headless entry point used by ``main()`` and tests. Raises ``ValueError`` if
    both ``as_csv`` and ``as_json`` are set, so silently picking one doesn't hide a
    CLI usage mistake."""
    if as_csv and as_json:
        msg = "--csv and --json are mutually exclusive"
        raise ValueError(msg)
    if not storage.DB_PATH.exists():
        msg = (
            f"DB not found at {storage.DB_PATH}. Either point "
            "DINARY_DATA_PATH at an existing SQLite file, or use "
            "`inv report-income --prod` to query the server."
        )
        print(msg, file=sys.stderr)
        return 1

    con = storage.get_connection()
    try:
        rows = aggregate_income(con)
    finally:
        con.close()

    render(rows, as_csv=as_csv, as_json=as_json, stream=stream)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show income aggregated by year.",
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
            "emit a JSON array of rows to stdout (wire format used by ``inv report-income --prod``)"
        ),
    )
    args = parser.parse_args(argv)
    return run(as_csv=args.csv, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
