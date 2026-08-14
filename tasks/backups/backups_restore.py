"""Yandex.Disk restore pipeline.

Pulls a compressed snapshot from the operator-local ``yandex:`` rclone remote,
decompresses and integrity-checks it, then hands it to the shared restore flow —
which writes it to the local database or, with ``--prod``, to production.
"""

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from invoke import task

from tasks.backups.backup_snapshots import (
    BACKUP_RCLONE_PATH,
    BACKUP_RCLONE_REMOTE,
    assert_local_binaries,
    parse_snapshot_lsjson,
    pick_snapshot,
    print_snapshot_list,
)

from .backups_yandex import ensure_local_yandex_rclone_configured
from .restore_utils import run_restore


def yadisk_list_snapshots():
    """Shape/sort contract inherited from
    :func:`dinary.tools.backup_snapshots.parse_snapshot_lsjson`."""
    raw = subprocess.check_output(
        ["rclone", "lsjson", f"{BACKUP_RCLONE_REMOTE}:{BACKUP_RCLONE_PATH}/", "--files-only"],
        text=True,
    )
    return parse_snapshot_lsjson(raw)


def _download_and_verify(c, picked, workpath: Path) -> Path:
    """Download snapshot from Yadisk, decompress, integrity-check. Returns path to restored DB."""
    archive = workpath / picked[0]
    restored = workpath / "restored.db"
    remote_path = f"{BACKUP_RCLONE_REMOTE}:{BACKUP_RCLONE_PATH}/{picked[0]}"
    c.run(f"rclone copyto {shlex.quote(remote_path)} {shlex.quote(str(archive))}")
    c.run(f"zstd -q -d {shlex.quote(str(archive))} -o {shlex.quote(str(restored))}")
    check = subprocess.run(
        ["sqlite3", str(restored), "PRAGMA integrity_check"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0 or check.stdout.strip() != "ok":
        sys.stderr.write(
            f"integrity_check FAILED on {picked[0]}; "
            f"data/dinary.db NOT touched.\n"
            f"  stdout: {check.stdout.strip() or '(empty)'}\n"
            f"  stderr: {check.stderr.strip() or '(empty)'}\n",
        )
        sys.exit(1)
    return restored


@task(name="restore-yadisk")
def restore_from_yadisk(c, snapshot="latest", list_only=False, yes=False, prod=False):
    """Restore a snapshot from Yandex.Disk.

    Writes data/dinary.db by default; --prod replaces the production database instead.
    Flags: --snapshot DATE (default latest), --list-only, --yes, --prod.
    See https://andgineer.github.io/dinary/operations for restore runbooks.
    """
    assert_local_binaries(["rclone", "sqlite3", "zstd"])
    ensure_local_yandex_rclone_configured()

    snapshots = yadisk_list_snapshots()
    if not snapshots:
        sys.stderr.write(
            f"No snapshots found at {BACKUP_RCLONE_REMOTE}:{BACKUP_RCLONE_PATH}/.\n",
        )
        sys.exit(1)

    if list_only:
        print_snapshot_list(snapshots)
        return

    picked = pick_snapshot(snapshots, snapshot)
    if picked is None:
        sys.stderr.write(f"No snapshot matches --snapshot={snapshot!r}.\n")
        print_snapshot_list(snapshots, stream=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory() as workdir:
        restored = _download_and_verify(c, picked, Path(workdir))
        run_restore(
            c,
            restored,
            incoming_label=f"Snapshot {picked[0]}",
            target=Path("data/dinary.db"),
            prod=prod,
            yes=yes,
        )
