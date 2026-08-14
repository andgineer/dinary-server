"""Shared restore helpers: database summaries, the confirmation prompt, and the
local file swap. The prod-side pipeline lives in :mod:`tasks.backups.restore_prod`."""

import re
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tasks.backups.restore_prod import apply_restore_to_prod
from tasks.ssh_utils import sqlite_backup_prologue, ssh_capture

PROD_CONFIRM_WORD = "prod"
LOCAL_CONFIRM_WORD = "yes"

# Ordering goes through datetime(): stored values carry a UTC offset that shifts with DST,
# so a plain string comparison mis-sorts the hour around each switch. The newest expense is
# still displayed as stored.
_SUMMARY_SQL = (
    "SELECT COUNT(*) || '|' ||"
    " COALESCE((SELECT datetime FROM expenses ORDER BY datetime(datetime) DESC LIMIT 1), '')"
    " || '|' ||"
    " COALESCE(SUM(CASE WHEN datetime(datetime) > datetime('{cutoff}') THEN 1 ELSE 0 END), 0)"
    " FROM expenses"
)

_TIMESTAMP_RE = re.compile(r"^[0-9T :.+-]*$")


@dataclass(frozen=True)
class DbSummary:
    """Row count, newest expense, and how many expenses are newer than a cutoff."""

    label: str
    rows: int
    last_expense: str = ""
    newer_than_cutoff: int = 0

    def describe(self) -> str:
        if not self.rows:
            return f"{self.label}: empty"
        return f"{self.label}: {self.rows:,} expenses, last {self.last_expense or 'unknown'}"


def _summary_sql(cutoff: str) -> str:
    """The cutoff is read from a database, but it ends up inside a double-quoted
    argument of a remote shell command — reject anything that could break out."""
    if not _TIMESTAMP_RE.match(cutoff):
        msg = f"unexpected timestamp {cutoff!r}"
        raise ValueError(msg)
    return _SUMMARY_SQL.format(cutoff=cutoff)


def _parse_summary(raw: str, label: str) -> DbSummary:
    rows, last, newer = ([*raw.strip().split("|", 2), "", ""])[:3]
    return DbSummary(
        label=label,
        rows=int(rows or 0),
        last_expense=last,
        newer_than_cutoff=int(newer or 0),
    )


def summarize_db_file(path: Path, label: str, cutoff: str = "") -> DbSummary:
    """Summarize a local SQLite file. An unreadable file summarizes as empty — the
    result only feeds a confirmation prompt."""
    try:
        with sqlite3.connect(path) as con:
            raw = con.execute(_summary_sql(cutoff)).fetchone()[0]
    except sqlite3.Error:
        return DbSummary(label=label, rows=0)
    return _parse_summary(str(raw), label)


def summarize_prod_db(c, label: str = "Prod", cutoff: str = "") -> DbSummary:
    """Summarize the production database over SSH, reading a ``.backup`` snapshot so
    the counts are not taken mid-transaction."""
    raw = ssh_capture(
        c,
        sqlite_backup_prologue("dinary-restore-summary")
        + f'sqlite3 "$SNAP" "{_summary_sql(cutoff)}"',
    )
    return _parse_summary(raw, label)


def render_restore_plan(current: DbSummary, incoming: DbSummary) -> str:
    """Both sides plus what the restore drops, so a stale snapshot is visible as a
    number rather than as silently missing rows afterwards."""
    lines = [current.describe(), incoming.describe()]
    if current.newer_than_cutoff:
        lines.append(
            f"Losing: {current.newer_than_cutoff:,} expenses recorded after "
            f"{incoming.last_expense}",
        )
    return "\n".join(lines)


def confirm_restore(word: str, *, yes: bool = False) -> None:
    """``--yes`` is honoured for a local target only: on prod the prompt exists so the
    diff gets read, and a flag left in shell history cannot stand in for that."""
    if yes and word == LOCAL_CONFIRM_WORD:
        return
    answer = input(f"Type '{word}' to proceed: ").strip()
    if answer != word:
        sys.stderr.write("Aborted.\n")
        sys.exit(1)


@contextmanager
def staged_db(db_bytes: bytes) -> Generator[Path]:
    """Materialize restored bytes as a file so they can be summarized (and, for prod,
    uploaded) before anything is overwritten."""
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "incoming.db"
        path.write_bytes(db_bytes)
        yield path


def run_restore(c, staged: Path, *, incoming_label: str, target: Path, prod: bool, yes: bool):
    """Show both sides, confirm, then write the staged database to the chosen target."""
    incoming = summarize_db_file(staged, incoming_label)
    current = (
        summarize_prod_db(c, cutoff=incoming.last_expense)
        if prod
        else summarize_db_file(target, str(target), cutoff=incoming.last_expense)
    )
    print(render_restore_plan(current, incoming))
    confirm_restore(PROD_CONFIRM_WORD if prod else LOCAL_CONFIRM_WORD, yes=yes)
    if prod:
        apply_restore_to_prod(c, staged)
        print(f"=== {summarize_prod_db(c, label='Prod now holds').describe()} ===")
    else:
        apply_restore(staged.read_bytes(), target)
        print(f"=== Restored {target} ===")


def apply_restore(db_bytes: bytes, target: Path) -> None:
    """Write db_bytes to target, preserving the old file and removing stale WAL."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%MZ")
        preserved = target.with_name(f"{target.name}.before-restore-{ts}")
        target.rename(preserved)
        print(f"Previous {target.name} saved as {preserved.name}")
    for wal_file in (target.with_suffix(".db-wal"), target.with_suffix(".db-shm")):
        if wal_file.exists():
            wal_file.unlink()
            print(f"Removed stale WAL file {wal_file.name}")
    target.write_bytes(db_bytes)
