"""Shared helpers for restore-primary, restore-replica, and restore-yadisk."""

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tasks.backups.backup_snapshots import sqlite_row_count
from tasks.devtools.constants import (
    _REMOTE_LITESTREAM_SHADOW_PATH,
    REPLICA_DB_NAME,
    REPLICA_LITESTREAM_DIR,
    VM1_LITESTREAM_KEY_PATH,
)

_LITESTREAM_CONFIG = Path("/etc/litestream.yml")


def confirm_overwrite(target: Path, incoming_desc: str, yes: bool) -> None:
    """Print existing DB stats and prompt for confirmation. Exits non-zero on refusal."""
    row_count = sqlite_row_count(target)
    size_kb = target.stat().st_size / 1024
    mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(
        f"About to overwrite {target} ({row_count:,} expense rows, "
        f"{size_kb:,.1f} KB, mtime {mtime})\n"
        f"with {incoming_desc}.\n"
        f"Previous file will be saved as {target.name}.before-restore-<UTC-ISO>.\n"
        f"WARNING: stop the server before proceeding to avoid WAL corruption.",
    )
    if not yes:
        answer = input("Type 'yes' to proceed: ").strip()
        if answer != "yes":
            sys.stderr.write("Aborted.\n")
            sys.exit(1)


def litestream_active() -> bool:
    """Return True when litestream.service is running on this machine (i.e. we are on VM1)."""
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "litestream"],
        check=False,
    )
    return result.returncode == 0


def _parse_vm2_ssh_target(config_path: Path | None = None) -> str:
    """Extract ``user@host`` for VM2 from the litestream config written by ``inv setup-replica``."""
    if config_path is None:
        config_path = _LITESTREAM_CONFIG
    text = config_path.read_text(encoding="utf-8")
    host_m = re.search(r"^\s+host:\s+(\S+):22\s*$", text, re.MULTILINE)
    user_m = re.search(r"^\s+user:\s+(\S+)\s*$", text, re.MULTILINE)
    if not host_m or not user_m:
        raise ValueError(f"Cannot parse VM2 SSH target from {config_path}")
    return f"{user_m.group(1)}@{host_m.group(1)}"


def local_replica_resync(c) -> None:
    """Resync VM2 from VM1 without routing through the developer machine.

    Equivalent to ``inv replica-resync`` but runs entirely on VM1:
    stops the local litestream service, wipes the stale LTX trees on
    both sides, then restarts.
    Called automatically by restore tasks when litestream is active.
    """
    vm2 = _parse_vm2_ssh_target()
    replica_dir = f"{REPLICA_LITESTREAM_DIR}/{REPLICA_DB_NAME}"
    print("=== Stopping litestream on VM1 ===")
    c.run("sudo systemctl stop litestream")
    print("=== Wiping stale LTX shadow tree on VM1 ===")
    c.run(f"rm -rf {_REMOTE_LITESTREAM_SHADOW_PATH}")
    print(f"=== Wiping stale LTX tree on {vm2} ===")
    c.run(f"ssh -i {VM1_LITESTREAM_KEY_PATH} {vm2} 'rm -rf {replica_dir}'")
    print("=== Starting litestream on VM1 (will push fresh snapshot) ===")
    c.run("sudo systemctl start litestream")
    c.run(
        "sleep 5 && systemctl is-active litestream && "
        "sudo journalctl -u litestream -n 20 --no-pager",
    )
    print("=== Replica resync done ===")


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
