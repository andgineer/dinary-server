"""Prod-side restore steps, driven entirely from the operator machine over SSH:
service control, the database swap, and the replica resync that ``inv replica-resync``
reuses. Nothing here runs ``inv`` on the server, so a restore never depends on which
revision happens to be deployed there."""

from datetime import UTC, datetime
from pathlib import Path

from tasks.devtools.constants import (
    _REMOTE_DB_PATH,
    _REMOTE_LITESTREAM_SHADOW_PATH,
    REPLICA_DB_NAME,
    REPLICA_LITESTREAM_DIR,
)
from tasks.devtools.env import replica_host
from tasks.ssh_utils import (
    ssh_capture,
    ssh_replica,
    ssh_run,
    ssh_sudo,
    upload_remote_bytes,
)

_INCOMING_DB_PATH = "/tmp/dinary-restore-incoming.db"  # noqa: S108


def wipe_ltx_trees(c) -> None:
    """Both trees go together: the VM1 shadow tree carries txids of the database it was
    built against, so keeping it strands litestream against a freshly emptied replica."""
    print("=== Wiping stale LTX shadow tree on VM1 ===")
    ssh_run(c, f"rm -rf {_REMOTE_LITESTREAM_SHADOW_PATH}")
    print(f"=== Wiping stale LTX tree on {replica_host()} ===")
    ssh_replica(c, f"rm -rf {REPLICA_LITESTREAM_DIR}/{REPLICA_DB_NAME}")


def stop_litestream(c) -> None:
    print("=== Stopping litestream on VM1 ===")
    ssh_sudo(c, "systemctl stop litestream")


def start_litestream(c) -> None:
    print("=== Starting litestream on VM1 (will push a fresh snapshot) ===")
    ssh_sudo(c, "systemctl start litestream")
    ssh_run(
        c,
        "sleep 5 && systemctl is-active litestream && "
        "sudo journalctl -u litestream -n 20 --no-pager",
    )


def _preserved_path() -> str:
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%MZ")
    return f"{_REMOTE_DB_PATH}.before-restore-{ts}"


def _report_services(c) -> None:
    for service in ("dinary", "litestream"):
        state = ssh_capture(c, f"systemctl is-active {service} || true").strip()
        print(f"=== {service}: {state} ===")


def apply_restore_to_prod(c, staged_db: Path) -> None:
    """Swap ``staged_db`` in as the production database and resync the replica.

    Both services are brought back from a ``finally``: a failure mid-swap must leave
    prod running rather than silently down.
    """
    print("=== Stopping dinary and litestream on VM1 ===")
    ssh_sudo(c, "systemctl stop dinary")
    stop_litestream(c)
    try:
        preserved = _preserved_path()
        ssh_run(c, f"cp {_REMOTE_DB_PATH} {preserved}")
        print(f"=== Previous prod DB saved as {preserved} ===")
        upload_remote_bytes(staged_db, _INCOMING_DB_PATH)
        ssh_run(
            c,
            f"mv {_INCOMING_DB_PATH} {_REMOTE_DB_PATH}"
            f" && rm -f {_REMOTE_DB_PATH}-wal {_REMOTE_DB_PATH}-shm",
        )
        wipe_ltx_trees(c)
    finally:
        start_litestream(c)
        print("=== Starting dinary on VM1 ===")
        ssh_sudo(c, "systemctl start dinary")
    _report_services(c)
