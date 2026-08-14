"""Database tasks: migrate, verify-db, restore-primary."""

import base64
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from invoke import task

from dinary.db.db_migrations import migration_ids
from tasks.backups.restore_utils import run_restore, staged_db
from tasks.devtools.constants import _REMOTE_DB_PATH
from tasks.devtools.env import host
from tasks.ssh_utils import sqlite_backup_prologue, ssh_capture, ssh_capture_bytes, ssh_run

_LOCAL_DB_PATH = Path("data/dinary.db")


@contextmanager
def open_local_db() -> Generator[sqlite3.Connection]:
    if not _LOCAL_DB_PATH.exists():
        print(f"No local DB at {_LOCAL_DB_PATH}", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(_LOCAL_DB_PATH)
    try:
        yield con
    finally:
        con.close()


@task(name="migrate")
def migrate(c):
    """Apply pending yoyo migrations to local data/dinary.db (local dev only).

    The server applies migrations automatically on start — no manual step needed there.
    """
    c.run(
        "uv run python -c 'from dinary.db import storage; "
        'storage.init_db(); print("Migrated data/dinary.db")\'',
    )


@task(name="seed-categories")
def seed_categories(c):
    """Seed/reconcile the category catalog from src/dinary/category_templates/ (local dev only).

    The server runs this automatically on every boot — manual entry point for
    ad-hoc reseed/reconcile without restarting the service.
    """
    c.run(
        "uv run python -c '"
        "from dinary.db import storage, category_seed; "
        "storage.init_db()\n"
        "with storage.connection() as con:\n"
        "    category_seed.bootstrap_categories(con)\n"
        'print("Seeded categories")\'',
    )


@task(name="verify-db")
def verify_db(c, prod=False):  # noqa: ARG001
    """Check DB structural integrity and foreign-key consistency.

    --prod runs against a prod snapshot over SSH (default: local data/dinary.db).
    Exits non-zero on any issue.
    """
    if prod:
        remote_cmd = (
            "set -e; "
            + sqlite_backup_prologue("dinary-verify-db")
            + 'sqlite3 "$SNAP" "PRAGMA integrity_check; PRAGMA foreign_key_check;"'
        )
        raw = ssh_capture_bytes(remote_cmd)
        output = raw.decode("utf-8", errors="replace")
    else:
        with open_local_db() as con:
            rows = con.execute("PRAGMA integrity_check").fetchall()
            rows.extend(con.execute("PRAGMA foreign_key_check").fetchall())
        output = "\n".join("|".join(str(col) for col in row) for row in rows)
    print(output, end="" if output.endswith("\n") else "\n")
    lines = [line for line in output.splitlines() if line.strip()]
    if lines != ["ok"]:
        print("=== verify-db FAILED ===", file=sys.stderr)
        sys.exit(1)
    print("=== verify-db OK ===")


@task(name="restore-primary")
def restore_primary(c, output=None, yes=False):  # noqa: ARG001
    """Download a live consistent snapshot from VM1 to data/dinary.db.

    Uses SQLite online-backup API — no service shutdown needed. Prod is the source
    here, never the target, so there is no --prod: to put a snapshot back on the
    server use restore-replica --prod or restore-yadisk --prod.
    Flags: -o PATH (default data/dinary.db), --yes to skip confirmation.
    """
    target = Path(output) if output else Path("data/dinary.db")
    remote_cmd = sqlite_backup_prologue("dinary-restore-primary") + 'cat "$SNAP"'
    b64 = base64.b64encode(remote_cmd.encode()).decode()
    db_bytes = subprocess.run(
        ["ssh", host(), f"echo {b64} | base64 -d | bash"],
        capture_output=True,
        check=True,
    ).stdout
    with staged_db(db_bytes) as staged:
        run_restore(
            c,
            staged,
            incoming_label="Live snapshot from VM1",
            target=target,
            prod=False,
            yes=yes,
        )


@task(name="restore-yoyo")
def restore_yoyo(c, to):
    """Roll back server migrations, keeping the one matching the given numeric prefix.

    Example: inv restore-yoyo --to=0002
    If 0003 and 0004 are applied, rolls back 0004 then 0003, leaving 0002 applied.
    Requires .rollback.sql files to exist for every migration being rolled back.
    """
    all_ids = migration_ids()
    target = next((m for m in all_ids if m.startswith(to)), None)
    if target is None:
        print(f"No migration file matches prefix {to!r}. Known: {all_ids}", file=sys.stderr)
        sys.exit(1)

    to_roll_back = [m for m in all_ids if m > target]
    if not to_roll_back:
        print(f"{target} is already the last migration — nothing to roll back.")
        return

    svc_status = ssh_capture(c, "systemctl is-active dinary").strip()
    if svc_status == "active":
        print(
            "dinary is running — stop it first: sudo systemctl stop dinary",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Will roll back (newest first): {', '.join(reversed(to_roll_back))}")
    print(f"Target state: {target} applied, everything after removed.")
    ssh_run(
        c,
        f"cd ~/dinary && source ~/.local/bin/env"
        f" && uv run yoyo rollback --batch -r {to_roll_back[0]}"
        f" --database 'sqlite:///{_REMOTE_DB_PATH}'"
        f" src/dinary/db/migrations",
    )
