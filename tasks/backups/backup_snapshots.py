import json
import re
import shutil
import sqlite3
import sys
from datetime import UTC, datetime

from tasks.backups.backup_retention import _make_pattern

# ---------------------------------------------------------------------------
# Naming / storage constants
# ---------------------------------------------------------------------------

BACKUP_FILENAME_PREFIX = "dinary-"
BACKUP_FILENAME_SUFFIX = ".db.zst"

BACKUP_RCLONE_REMOTE = "yandex"
# Nested under an existing "Backup/" folder to avoid colliding with the
# operator's other ad-hoc uploads there.
BACKUP_RCLONE_PATH = "Backup/dinary"

# The daily timer fires at 03:17 UTC with 30min jitter, so a healthy snapshot
# is always <=24h30m old; 26h leaves a buffer for one missed jitter window.
BACKUP_STALE_HOURS = 26

# ---------------------------------------------------------------------------
# Snapshot inventory helpers
# ---------------------------------------------------------------------------


def parse_snapshot_lsjson(raw):
    """Sorted oldest-first; entries not matching the backup filename pattern are
    silently dropped."""
    entries = json.loads(raw)
    pattern = _make_pattern(BACKUP_FILENAME_PREFIX, BACKUP_FILENAME_SUFFIX)
    result = []
    for entry in entries:
        name = entry.get("Name", "")
        if pattern.match(name):
            result.append((name, int(entry.get("Size", 0))))
    result.sort(key=lambda x: x[0])
    return result


def parse_snapshot_timestamp(name):
    """Uses the filename timestamp rather than rclone's ModTime, to stay
    independent of Yandex-side clock skew."""
    pattern = re.compile(
        re.escape(BACKUP_FILENAME_PREFIX)
        + r"(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})Z"
        + re.escape(BACKUP_FILENAME_SUFFIX)
        + "$",
    )
    match = pattern.match(name)
    if match is None:
        return None
    year, month, day, hour, minute = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def check_backup_freshness(snapshots, now, max_age_hours):
    if not snapshots:
        return {
            "status": "empty",
            "newest": None,
            "age_hours": None,
            "size_bytes": None,
            "threshold_hours": float(max_age_hours),
        }
    name, size = snapshots[-1]
    ts = parse_snapshot_timestamp(name)
    if ts is None:
        return {
            "status": "stale",
            "newest": name,
            "age_hours": None,
            "size_bytes": size,
            "threshold_hours": float(max_age_hours),
        }
    age_hours = (now - ts).total_seconds() / 3600.0
    status = "ok" if age_hours <= max_age_hours else "stale"
    return {
        "status": status,
        "newest": name,
        "age_hours": age_hours,
        "size_bytes": size,
        "threshold_hours": float(max_age_hours),
    }


def format_backup_status_line(verdict):
    """Kept separate from the task so the cron wrapper and tests can reuse the exact wording."""
    threshold = verdict["threshold_hours"]
    if verdict["status"] == "empty":
        return (
            f"STALE: no snapshots on "
            f"{BACKUP_RCLONE_REMOTE}:{BACKUP_RCLONE_PATH}/ "
            f"(threshold: {threshold:g}h)"
        )
    name = verdict["newest"]
    size = verdict["size_bytes"]
    size_kb = (size or 0) / 1024
    age = verdict["age_hours"]
    if age is None:
        return (
            f"STALE: newest {name} has un-parseable timestamp "
            f"({size_kb:,.1f} KB, threshold: {threshold:g}h)"
        )
    tag = "OK" if verdict["status"] == "ok" else "STALE"
    return (
        f"{tag}: newest {name}, age {age:.1f}h, size {size_kb:,.1f} KB (threshold: {threshold:g}h)"
    )


def check_identical_backup_sizes(snapshots):
    """Exchange rates are written daily, so two backups of identical size mean a frozen replica.

    Fewer than two backups is treated as failure, not skipped, so a silently
    broken pipeline is never reported as healthy.
    """
    if len(snapshots) < 2:
        name, size = snapshots[0] if snapshots else (None, None)
        return {"status": "single", "newest": name, "size_bytes": size}
    newest_name, newest_size = snapshots[-1]
    prev_name, prev_size = snapshots[-2]
    if newest_size == prev_size:
        return {
            "status": "frozen",
            "newest": newest_name,
            "prev": prev_name,
            "size_bytes": newest_size,
        }
    return {"status": "ok"}


def format_frozen_replica_line(result):
    size_kb = result["size_bytes"] / 1024
    return (
        f"FAIL: {result['newest']} and {result['prev']} are both {size_kb:.1f} KB"
        " — Litestream replica appears frozen (exchange rates change daily)."
        " Run `inv healthcheck --prod` to confirm, then `inv replica-resync` to fix."
    )


def format_single_backup_line(result):
    if result["newest"] is None:
        return "FAIL: no backups found — cannot verify replica is not frozen."
    size_kb = result["size_bytes"] / 1024
    return (
        f"FAIL: only one backup found ({result['newest']}, {size_kb:.1f} KB)"
        " — cannot verify replica is not frozen. Expected daily backups."
    )


def pick_snapshot(snapshots, key):
    """A non-``latest`` key is matched as a date prefix, so ``--snapshot 2026-03-15``
    matches without the time suffix."""
    if not snapshots:
        return None
    if key == "latest":
        return snapshots[-1]
    needle = f"{BACKUP_FILENAME_PREFIX}{key}"
    for name, size in snapshots:
        if name.startswith(needle):
            return (name, size)
    return None


def print_snapshot_list(snapshots, stream=None):
    stream = stream if stream is not None else sys.stdout
    for name, size in reversed(snapshots):
        kb = size / 1024
        stream.write(f"  {name}  ({kb:,.1f} KB)\n")


def sqlite_row_count(db_path):
    """Returns 0 on any SQLite error rather than raising — the count only feeds a
    confirmation prompt."""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM expenses")
            return cur.fetchone()[0]
    except sqlite3.Error:
        return 0


def assert_local_binaries(names):
    missing = [name for name in names if shutil.which(name) is None]
    if not missing:
        return
    sys.stderr.write(
        "Missing local tools: " + ", ".join(missing) + ".\n"
        "  Ubuntu/Debian: sudo apt install " + " ".join(missing) + "\n"
        "  macOS: brew install " + " ".join(missing) + "\n"
        "Also ensure `rclone config` has run and a remote named "
        f"{BACKUP_RCLONE_REMOTE!r} exists (one-time OAuth browser click).\n",
    )
    sys.exit(1)
