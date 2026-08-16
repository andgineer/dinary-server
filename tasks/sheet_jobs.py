"""Sheet-logging queue maintenance: return poisoned rows to the drain."""

import sys

from invoke import task

from tasks.db import open_local_db
from tasks.devtools.constants import _REMOTE_DB_PATH
from tasks.ssh_utils import ssh_capture

_COUNT_SQL = (
    "SELECT (SELECT COUNT(*) FROM sheet_logging_jobs WHERE status = 'poisoned')"
    " || '|' ||"
    " (SELECT COUNT(*) FROM income_logging_jobs WHERE status = 'poisoned')"
)

_REQUEUE_STATEMENTS = (
    "UPDATE sheet_logging_jobs SET status = 'pending', claim_token = NULL,"
    " claimed_at = NULL, last_error = NULL WHERE status = 'poisoned'",
    "UPDATE income_logging_jobs SET status = 'pending', claimed_at = NULL,"
    " last_error = NULL WHERE status = 'poisoned'",
)


def parse_poisoned_counts(raw: str) -> tuple[int, int]:
    """Parse the ``expenses|income`` counter pair; unreadable output counts as zero."""
    parts = (raw.strip().split("|") + ["0", "0"])[:2]
    try:
        return int(parts[0] or 0), int(parts[1] or 0)
    except ValueError:
        return 0, 0


def _count_local() -> tuple[int, int]:
    with open_local_db() as con:
        return parse_poisoned_counts(str(con.execute(_COUNT_SQL).fetchone()[0]))


def _count_prod(c) -> tuple[int, int]:
    return parse_poisoned_counts(ssh_capture(c, f'sqlite3 "{_REMOTE_DB_PATH}" "{_COUNT_SQL}"'))


def _requeue_local() -> None:
    with open_local_db() as con:
        for sql in _REQUEUE_STATEMENTS:
            con.execute(sql)
        con.commit()


def _requeue_prod(c) -> None:
    """Runs against the live DB rather than a snapshot — a status flip is a short
    write transaction that WAL serialises against the service's own writer."""
    statements = "".join(f"{sql}; " for sql in _REQUEUE_STATEMENTS)
    ssh_capture(c, f'sqlite3 "{_REMOTE_DB_PATH}" "{statements}"')


@task(name="requeue-sheet-jobs")
def requeue_sheet_jobs(c, prod=False, yes=False):
    """Return poisoned sheet-logging rows to 'pending' so the drain retries them.

    --prod targets the server over SSH (default: local data/dinary.db).
    See https://andgineer.github.io/dinary/operations#monitoring
    """
    expenses, income = _count_prod(c) if prod else _count_local()
    if not expenses and not income:
        print("Nothing to requeue: no poisoned sheet-logging jobs")
        return

    target = "the server" if prod else "data/dinary.db"
    print(f"Poisoned on {target}: {expenses} expense job(s), {income} income job(s)")
    if not yes:
        answer = input("Type 'yes' to requeue them: ").strip().lower()
        if answer != "yes":
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    if prod:
        _requeue_prod(c)
    else:
        _requeue_local()
    print(f"Requeued {expenses + income} job(s); the drain picks them up on its next sweep")
