"""Tests for healthcheck pure helpers."""

import allure

from tasks.devtools.constants import REPLICA_DB_NAME, REPLICA_LITESTREAM_DIR
from tasks.healthcheck import (
    _build_replica_sync_script,
    _litestream_error_check_command,
    _parse_litestream_errors,
    _parse_sync_output,
    _sync_divergence_messages,
)


@allure.epic("Infrastructure")
@allure.feature("Healthcheck")
@allure.story("Replica sync")
class TestBuildReplicaSyncScript:
    def test_contains_page_count_pragma(self):
        script = _build_replica_sync_script()
        assert "PRAGMA page_count" in script

    def test_contains_exchange_rates_query(self):
        script = _build_replica_sync_script()
        assert "exchange_rates" in script
        assert "MAX(date)" in script

    def test_contains_litestream_restore(self):
        script = _build_replica_sync_script()
        assert "litestream restore" in script

    def test_references_configured_replica_path(self):
        script = _build_replica_sync_script()
        assert REPLICA_LITESTREAM_DIR in script
        assert REPLICA_DB_NAME in script


@allure.epic("Infrastructure")
@allure.feature("Healthcheck")
@allure.story("Replica sync")
class TestParseSyncOutput:
    def test_parses_two_lines(self):
        raw = b"242\n2026-06-05\n"
        page_count, max_date = _parse_sync_output(raw)
        assert page_count == "242"
        assert max_date == "2026-06-05"

    def test_strips_trailing_whitespace(self):
        raw = b"242\n2026-06-05\n\n"
        page_count, max_date = _parse_sync_output(raw)
        assert page_count == "242"
        assert max_date == "2026-06-05"

    def test_fills_missing_second_line_with_question_mark(self):
        raw = b"242\n"
        page_count, max_date = _parse_sync_output(raw)
        assert page_count == "242"
        assert max_date == "?"

    def test_empty_output_returns_question_marks(self):
        page_count, max_date = _parse_sync_output(b"")
        assert page_count == "?"
        assert max_date == "?"


@allure.epic("Infrastructure")
@allure.feature("Healthcheck")
@allure.story("Replica sync")
class TestSyncDivergenceMessages:
    def test_no_messages_when_in_sync(self):
        assert _sync_divergence_messages(("242", "2026-06-05"), ("242", "2026-06-05")) == []

    def test_detects_page_count_mismatch(self):
        msgs = _sync_divergence_messages(("300", "2026-06-05"), ("242", "2026-06-05"))
        assert len(msgs) == 1
        assert "page_count" in msgs[0]
        assert "300" in msgs[0]
        assert "242" in msgs[0]

    def test_detects_stale_exchange_rates(self):
        msgs = _sync_divergence_messages(("242", "2026-06-05"), ("242", "2026-05-10"))
        assert len(msgs) == 1
        assert "exchange_rates" in msgs[0]
        assert "2026-06-05" in msgs[0]
        assert "2026-05-10" in msgs[0]

    def test_detects_both_mismatches(self):
        msgs = _sync_divergence_messages(("300", "2026-06-05"), ("242", "2026-05-10"))
        assert len(msgs) == 2

    def test_detects_never_replica(self):
        msgs = _sync_divergence_messages(("242", "2026-06-05"), ("242", "never"))
        assert len(msgs) == 1
        assert "never" in msgs[0]


@allure.epic("Infrastructure")
@allure.feature("Healthcheck")
@allure.story("Litestream errors")
class TestLitestreamErrorCheckCommand:
    def test_targets_litestream_service(self):
        assert "litestream" in _litestream_error_check_command()

    def test_matches_message_text_not_journald_priority(self):
        """Litestream logs to stdout, so journald tags every line PRIORITY=6 — `-p err` is blind."""
        cmd = _litestream_error_check_command()
        assert "level=ERROR" in cmd
        assert "-p err" not in cmd

    def test_scoped_to_current_invocation(self):
        assert "_SYSTEMD_INVOCATION_ID" in _litestream_error_check_command()

    def test_ignores_benign_startup_compaction_error(self):
        assert "page size not initialized yet" in _litestream_error_check_command()

    def test_covers_24h_window(self):
        assert "24 hours ago" in _litestream_error_check_command()


@allure.epic("Infrastructure")
@allure.feature("Healthcheck")
@allure.story("Litestream errors")
class TestParseLitestreamErrors:
    def test_empty_output_returns_zero(self):
        assert _parse_litestream_errors("") == (0, "")

    def test_whitespace_only_returns_zero(self):
        assert _parse_litestream_errors("  \n  ") == (0, "")

    def test_zero_count_returns_no_error(self):
        assert _parse_litestream_errors("0") == (0, "")

    def test_single_error_line(self):
        out = "1\nJun 06 10:00:00 vm1 litestream[1]: level=ERROR non-contiguous transaction files"
        count, last = _parse_litestream_errors(out)
        assert count == 1
        assert "non-contiguous" in last

    def test_large_count_with_last_line(self):
        out = "86415\nJul 28 14:21:06 vm1 litestream[1]: level=ERROR monitor error"
        assert _parse_litestream_errors(out) == (
            86415,
            "Jul 28 14:21:06 vm1 litestream[1]: level=ERROR monitor error",
        )

    def test_non_numeric_first_line_is_ignored(self):
        assert _parse_litestream_errors("bash: journalctl: not found") == (0, "")
