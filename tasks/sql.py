"""Interactive SQL runner against ``data/dinary.db``. See specs/reference/sql-tool.md."""

import argparse
import csv as _csv
import json as _json
import sys
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

from rich.console import Console
from rich.table import Table

from dinary.config import settings
from dinary.db import storage as sqlite_types


def _execute(sql: str, *, write: bool = False) -> tuple[list[str], list[tuple]]:
    """``write=False`` (default) opens read-only so SQLite itself refuses
    mutations; ``write=True`` is only reachable via ``inv sql --write`` locally,
    never over ``--prod``. ``columns == []`` means no result set (DDL/pragma)."""
    con = sqlite_types.connect(str(Path(settings.data_path)), read_only=not write)
    try:
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = [tuple(r) for r in cursor.fetchall()] if columns else []
    finally:
        con.close()
    return columns, rows


def _coerce_for_json(value: Any) -> Any:
    """``Decimal``/``date``/``datetime``/``bytes`` (from storage's converters) don't
    survive ``json.dumps`` natively; primitives pass through verbatim so ``jq``
    filters see native JSON numbers rather than strings."""
    if value is None or isinstance(value, int | float | str | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def render_rich(columns: list[str], rows: list[tuple], *, stream: IO[str] | None = None) -> None:
    """``stream`` resolves to ``sys.stdout`` at call time, not as a default value —
    a bare default would bind stdout at import time, before pytest's ``capsys``
    swaps it (same rationale in ``render_csv``/``render_json``)."""
    out = stream if stream is not None else sys.stdout
    if not columns:
        out.write("OK (no result set)\n")
        return
    table = Table(show_header=True, header_style="bold cyan")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*("" if v is None else str(v) for v in row))
    console = Console(file=out)
    console.print(table)
    console.print(f"[dim]{len(rows)} row(s)[/dim]")


def render_csv(columns: list[str], rows: list[tuple], *, stream: IO[str] | None = None) -> None:
    """Write CSV with header. Empty cells for ``NULL`` per standard CSV habit."""
    out = stream if stream is not None else sys.stdout
    writer = _csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])


def render_json(columns: list[str], rows: list[tuple], *, stream: IO[str] | None = None) -> None:
    """Schema is intentionally stable: ``inv sql --prod --json`` forwards these
    bytes verbatim, so local and remote output can be piped through ``jq`` alike."""
    out = stream if stream is not None else sys.stdout
    payload = {
        "columns": columns,
        "rows": [[_coerce_for_json(v) for v in row] for row in rows],
        "row_count": len(rows),
    }
    out.write(_json.dumps(payload, ensure_ascii=False))
    out.write("\n")


def rows_from_json(payload: dict) -> tuple[list[str], list[tuple]]:
    """Reverse of ``render_json`` for the local-render path on ``--prod``."""
    return payload["columns"], [tuple(r) for r in payload["rows"]]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Parses args, runs the query, renders."""
    parser = argparse.ArgumentParser(
        description="Read-only SQL runner against data/dinary.db",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--query", "-q", help="SQL query string")
    src.add_argument("--file", "-f", type=Path, help="read SQL from file")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--csv", action="store_true", help="emit CSV to stdout")
    fmt.add_argument("--json", action="store_true", help="emit JSON envelope")
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "open the DB read-write so mutating statements "
            "(UPDATE/DELETE/INSERT) can run. Off by default."
        ),
    )
    args = parser.parse_args(argv)

    sql = args.query if args.query else args.file.read_text(encoding="utf-8")

    columns, rows = _execute(sql, write=args.write)
    if args.csv:
        render_csv(columns, rows)
    elif args.json:
        render_json(columns, rows)
    else:
        render_rich(columns, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
