"""Healthcheck task and helpers — moved from server.py."""

import sys
from datetime import UTC
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta

from invoke import task

from tasks.db import open_local_db
from tasks.devtools.constants import REPLICA_DB_NAME, REPLICA_LITESTREAM_DIR
from tasks.devtools.env import _env, tunnel
from tasks.ssh_utils import (
    sqlite_backup_prologue,
    ssh_capture,
    ssh_capture_bytes,
    ssh_replica_capture_bytes,
)

# A journal validation failure alerts for this long, then stops. The metadata
# row remains as an audit record without failing every later healthcheck.
JOURNAL_VALIDATION_ALERT_WINDOW = _timedelta(hours=24)


def _healthcheck_run_queries(c, prod: bool, **queries: str) -> dict[str, str]:  # noqa: ARG001
    names = list(queries)
    sqls = list(queries.values())
    if prod:
        combined = "; ".join(sqls)
        raw = ssh_capture_bytes(
            sqlite_backup_prologue("dinary-healthcheck") + f'sqlite3 "$SNAP" "{combined}"',
        )
        values = raw.decode("utf-8", errors="replace").strip().splitlines()
    else:
        with open_local_db() as con:
            values = [str(con.execute(sql).fetchone()[0]) for sql in sqls]
    return dict(zip(names, values, strict=False))


def _healthcheck_sheet_log(results: dict[str, str]) -> bool:
    """Print the last expense's sheet-logging line. Returns True on failure."""
    expense_line = results.get("sheet", "").strip()
    if not expense_line:
        print("OK: no expenses in DB, nothing to check")
        return False
    expense_id, job_status = (expense_line.split("|", 1) + [""])[:2]
    if job_status == "poisoned":
        print(
            f"FAIL: last expense (id={expense_id}) sheet logging failed after all retries",
            file=sys.stderr,
        )
        return True
    if job_status in ("pending", "in_progress"):
        print(f"OK: last expense (id={expense_id}) sheet logging in progress")
        return False
    if not job_status:
        print(f"OK: last expense (id={expense_id}) logged to sheet")
        return False
    print(
        f"FAIL: last expense (id={expense_id}) unexpected sheet logging status: {job_status!r}",
        file=sys.stderr,
    )
    return True


def _healthcheck_sheet_queue(results: dict[str, str]) -> bool:
    """Print the poisoned sheet-logging backlog. Returns True if any row is poisoned.

    Scoped to the whole queue, not just the newest expense: a poisoned row is terminal,
    so checking only the tail lets the alert clear itself as soon as a later expense
    logs successfully, leaving the stuck rows unreported forever.
    """
    raw = results.get("sheet_poisoned", "0|0|").strip()
    parts = (raw.split("|") + ["0", "0", ""])[:3]
    expenses, income, sample = int(parts[0] or 0), int(parts[1] or 0), parts[2]

    if not expenses and not income:
        print("OK: no poisoned sheet-logging jobs")
        return False
    detail = f"{expenses} expense job(s), {income} income job(s)"
    if sample:
        detail += f"; newest expense ids: {sample}"
    print(
        f"FAIL: poisoned sheet-logging jobs never reach the spreadsheet — {detail}."
        " Fix with `inv requeue-sheet-jobs --prod`",
        file=sys.stderr,
    )
    return True


def _fmt_amount(value: str) -> str:
    try:
        f = float(value)
        return str(int(f)) if f == int(f) else f"{f:.2f}"
    except (ValueError, OverflowError):
        return value


def _healthcheck_last_expense_info(results: dict[str, str]) -> None:
    detail = results.get("last_expense", "").strip()
    prev_total = results.get("prev_day_total", "").strip()
    if detail:
        amount_str, currency, category = (detail.split("|", 2) + ["", ""])[:3]
        print(f"OK: last expense {_fmt_amount(amount_str)} {currency} ({category})")
    if prev_total:
        totals = ", ".join(
            f"{_fmt_amount(total)} {cur}"
            for entry in prev_total.split(",")
            for cur, total in [entry.split(":", 1)]
        )
        print(f"OK: yesterday total {totals}")


_LITESTREAM_BENIGN_STARTUP_ERROR = "page size not initialized yet"


def _litestream_error_check_command() -> str:
    """Litestream logs to stdout, so journald stamps every line PRIORITY=6 regardless of the
    ``level=`` the message carries — a ``-p err`` filter can never match. Scoping to the current
    InvocationID keeps a fixed fault from being reported for 24h after the restart that fixed it.
    The L9 compaction monitor always loses a startup race with the first DB read, so its one
    ``page size not initialized yet`` error per boot is expected and must not fail the check.
    """
    return (
        "journalctl -u litestream --since '24 hours ago'"
        ' _SYSTEMD_INVOCATION_ID="$(systemctl show -p InvocationID --value litestream)"'
        " --no-pager -q 2>/dev/null"
        f" | awk '/level=ERROR/ && !/{_LITESTREAM_BENIGN_STARTUP_ERROR}/"
        " {n++; last=$0} END {print n+0; if (n) print last}' || true"
    )


def _parse_litestream_errors(output: str) -> tuple[int, str]:
    """Parse the awk summary into (error_count, last_error_line)."""
    lines = output.strip().splitlines()
    if not lines:
        return 0, ""
    try:
        count = int(lines[0].strip())
    except ValueError:
        return 0, ""
    return count, lines[1].strip() if len(lines) > 1 else ""


def _build_replica_sync_script() -> str:
    """Restore latest LTX snapshot on VM2; output page_count then max exchange_rate date."""
    replica_path = f"{REPLICA_LITESTREAM_DIR}/{REPLICA_DB_NAME}"
    return (
        "set -euo pipefail\n"  # noqa: S608
        "WORKDIR=$(mktemp -d)\n"
        "trap 'rm -rf \"$WORKDIR\"' EXIT\n"
        'SNAP="$WORKDIR/hc.db"\n'
        'CFG="$WORKDIR/ls.yml"\n'
        'cat > "$CFG" <<LSYAML\n'
        "dbs:\n"
        "  - path: $SNAP\n"
        "    replicas:\n"
        "      - type: file\n"
        f"        path: {replica_path}\n"
        "LSYAML\n"
        'litestream restore -config "$CFG" "$SNAP" >&2\n'
        'sqlite3 "$SNAP" "PRAGMA page_count;"\n'
        'sqlite3 "$SNAP" "SELECT COALESCE(MAX(date), \'never\') FROM exchange_rates;"\n'
    )


def _parse_sync_output(raw: bytes) -> tuple[str, str]:
    """Parse 2-line sync output into (page_count, max_rate_date)."""
    lines = raw.decode("utf-8", errors="replace").strip().splitlines()
    page_count = lines[0] if lines else "?"
    max_date = lines[1] if len(lines) > 1 else "?"
    return page_count, max_date


def _sync_divergence_messages(
    primary: tuple[str, str],
    replica: tuple[str, str],
) -> list[str]:
    """Return failure messages for each metric that diverges between primary and replica."""
    msgs = []
    p_pages, p_date = primary
    r_pages, r_date = replica
    if p_pages != r_pages:
        msgs.append(
            f"replica page_count ({r_pages}) != primary ({p_pages})",
        )
    if p_date != r_date:
        msgs.append(
            f"replica exchange_rates stale (primary: {p_date}, replica: {r_date})"
            " — run `inv replica-resync` to fix",
        )
    return msgs


def _healthcheck_replica_sync() -> None:
    """Compare VM1 and VM2 page_count + exchange_rate date; exit non-zero if replica is stale."""
    if not _env().get("DINARY_REPLICA_HOST"):
        return
    primary_raw = ssh_capture_bytes(
        sqlite_backup_prologue("dinary-hc-primary")  # noqa: S608
        + 'sqlite3 "$SNAP" "PRAGMA page_count;" && '
        + 'sqlite3 "$SNAP" "SELECT COALESCE(MAX(date), \'never\') FROM exchange_rates;"',
    )
    replica_raw = ssh_replica_capture_bytes(_build_replica_sync_script())
    primary = _parse_sync_output(primary_raw)
    replica = _parse_sync_output(replica_raw)
    failures = _sync_divergence_messages(primary, replica)
    if failures:
        for msg in failures:
            print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    p_pages, p_date = primary
    print(f"OK: replica in sync with primary (page_count={p_pages}, exchange_rates up to {p_date})")


def _healthcheck_receipt_llm(results: dict[str, str]) -> bool:
    """Print LLM provider health lines. Returns True if any failure was found."""
    switch = results.get("llm_switch", "").strip()
    exhausted = results.get("llm_exhausted", "").strip()
    count = results.get("llm_switch_count", "0").strip()

    if count != "0":
        print(f"OK: LLM provider switches since last start: {count}")

    failed = False
    if switch:
        print(f"FAIL: LLM provider switched — {switch}", file=sys.stderr)
        failed = True
    if exhausted:
        print(f"FAIL: All LLM providers exhausted — {exhausted}", file=sys.stderr)
        failed = True
    return failed


def _healthcheck_receipt_queue(results: dict[str, str]) -> bool:
    """Print receipt classification queue health lines. Returns True if any failure was found."""
    raw = results.get("receipt_queue", "0|0|0|0").strip()
    parts = (raw.split("|") + ["0", "0", "0", "0"])[:4]
    pending, sleeping, in_progress, poisoned = (int(p or 0) for p in parts)

    problems = []
    if pending:
        problems.append(f"pending={pending}")
    if sleeping:
        problems.append(f"sleeping={sleeping}")
    if in_progress:
        problems.append(f"in_progress={in_progress}")
    if poisoned:
        problems.append(f"poisoned={poisoned}")

    if problems:
        print(
            f"FAIL: receipt classification queue not empty: {', '.join(problems)}",
            file=sys.stderr,
        )
        return True
    print("OK: receipt classification queue is empty")
    return False


def _validation_failure_is_recent(failure: str, now: _datetime) -> bool:
    stamp = failure.split("|", 1)[0].strip()
    try:
        when = _datetime.fromisoformat(stamp)
    except ValueError:
        return True  # unreadable timestamp is itself worth an alert
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when <= JOURNAL_VALIDATION_ALERT_WINDOW


def _healthcheck_receipt_fetch(results: dict[str, str]) -> bool:
    """Print receipt-fetch health lines. Returns True if any failure was found."""
    fallback_count = results.get("receipt_fallback_count", "0").strip()
    validation_failure = results.get("receipt_journal_validation_failure", "").strip()
    validation_failure_count = results.get(
        "receipt_journal_validation_failure_count",
        "0",
    ).strip()

    if fallback_count != "0":
        print(f"OK: journal fallback uses, all time: {fallback_count}")
    if validation_failure_count != "0":
        print(f"OK: journal validation failures, all time: {validation_failure_count}")

    if not validation_failure:
        return False
    if _validation_failure_is_recent(validation_failure, _datetime.now(UTC)):
        print(
            f"FAIL: journal receipt validation failed — {validation_failure}",
            file=sys.stderr,
        )
        return True
    print(
        f"OK: last journal validation failure older than alert window — {validation_failure}",
    )
    return False


@task(name="healthcheck")
def healthcheck(c, prod=False):  # noqa: ARG001
    """Check systemd services, background tasks, and DB state.

    --prod checks the server over SSH (default: local data/dinary.db).
    Exits non-zero on first failed check.
    See https://andgineer.github.io/dinary/operations#monitoring
    """
    if prod:
        tun = tunnel()
        services = ["dinary", "litestream"]
        if tun == "cloudflare":
            services.append("cloudflared")
        for svc in services:
            state = ssh_capture(c, f"systemctl is-active {svc} || true").strip()
            if state != "active":
                print(f"FAIL: service {svc} is {state!r}", file=sys.stderr)
                sys.exit(1)
            print(f"OK: service {svc} active")
        ltx_error_count, ltx_last_error = _parse_litestream_errors(
            ssh_capture(c, _litestream_error_check_command()),
        )
        if ltx_error_count:
            print(
                f"FAIL: litestream logged {ltx_error_count} error(s) in last 24h: {ltx_last_error}",
                file=sys.stderr,
            )
            sys.exit(1)
        print("OK: no litestream errors in last 24h")
        _healthcheck_replica_sync()

    yesterday = (_date.today() - _timedelta(days=1)).isoformat()
    results = _healthcheck_run_queries(
        c,
        prod,
        rate=f"SELECT count(*) FROM exchange_rates WHERE date = '{yesterday}'",  # noqa: S608
        llm_switch=(
            "SELECT COALESCE((SELECT value FROM app_metadata"
            " WHERE key = 'llm_provider_switch_last'), '')"
        ),
        llm_exhausted=(
            "SELECT COALESCE((SELECT value FROM app_metadata"
            " WHERE key = 'llm_all_exhausted_last'), '')"
        ),
        llm_switch_count=(
            "SELECT COALESCE((SELECT value FROM app_metadata"
            " WHERE key = 'llm_provider_switch_count'), '0')"
        ),
        receipt_journal_validation_failure=(
            "SELECT COALESCE((SELECT value FROM app_metadata"
            " WHERE key = 'receipt_journal_validation_failure_last'), '')"
        ),
        receipt_fallback_count=(
            "SELECT COALESCE((SELECT value FROM app_metadata"
            " WHERE key = 'receipt_fetch_fallback_count'), '0')"
        ),
        receipt_journal_validation_failure_count=(
            "SELECT COALESCE((SELECT value FROM app_metadata"
            " WHERE key = 'receipt_journal_validation_failure_count'), '0')"
        ),
        receipt_queue=(
            "SELECT"
            " COALESCE(SUM(CASE WHEN status='pending'"
            "  AND (retry_after IS NULL OR retry_after<=datetime('now'))"
            "  THEN 1 ELSE 0 END),0)"
            " ||'|'||"
            " COALESCE(SUM(CASE WHEN status='pending'"
            "  AND retry_after>datetime('now') THEN 1 ELSE 0 END),0)"
            " ||'|'||"
            " COALESCE(SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END),0)"
            " ||'|'||"
            " COALESCE(SUM(CASE WHEN status='poisoned' THEN 1 ELSE 0 END),0)"
            " FROM receipt_classification_jobs"
        ),
        sheet_poisoned=(
            "SELECT (SELECT COUNT(*) FROM sheet_logging_jobs WHERE status = 'poisoned')"
            " || '|' ||"
            " (SELECT COUNT(*) FROM income_logging_jobs WHERE status = 'poisoned')"
            " || '|' ||"
            " COALESCE((SELECT GROUP_CONCAT(expense_id) FROM"
            " (SELECT expense_id FROM sheet_logging_jobs WHERE status = 'poisoned'"
            "  ORDER BY expense_id DESC LIMIT 5)), '')"
        ),
        sheet=(
            "SELECT COALESCE("
            "(SELECT e.id || '|' || COALESCE(slj.status, '')"
            " FROM expenses e"
            " LEFT JOIN sheet_logging_jobs slj ON slj.expense_id = e.id"
            " ORDER BY e.id DESC LIMIT 1),"
            " '')"
        ),
        last_expense=(
            "SELECT COALESCE("
            "(SELECT CAST(e.amount_original AS TEXT) || '|' || e.currency_original || '|' || c.name"
            " FROM expenses e"
            " JOIN categories c ON c.id = e.category_id"
            " ORDER BY e.id DESC LIMIT 1),"
            " '')"
        ),
        prev_day_total=(
            f"SELECT COALESCE(GROUP_CONCAT(currency_original || ':' || total, ','), '')"  # noqa: S608
            f" FROM (SELECT currency_original, SUM(amount_original) AS total"
            f" FROM expenses WHERE DATE(datetime) = '{yesterday}'"
            f" GROUP BY currency_original)"
        ),
    )

    rate_count = int(results.get("rate", "0") or "0")
    if rate_count == 0:
        print(f"FAIL: no exchange rate cached for {yesterday}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: exchange rate for {yesterday} cached")

    if not _env().get("DINARY_SHEET_LOGGING_SPREADSHEET"):
        print("OK: sheet logging not configured, skipping")
        sheet_failed = False
    else:
        sheet_failed = any(
            [_healthcheck_sheet_log(results), _healthcheck_sheet_queue(results)],
        )

    _healthcheck_last_expense_info(results)
    llm_failed = _healthcheck_receipt_llm(results)
    fetch_failed = _healthcheck_receipt_fetch(results)
    queue_failed = _healthcheck_receipt_queue(results)
    if sheet_failed or llm_failed or fetch_failed or queue_failed:
        sys.exit(1)
