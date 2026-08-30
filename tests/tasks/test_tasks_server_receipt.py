"""Tests for receipt-pipeline healthcheck functions in tasks/healthcheck.py."""

from datetime import UTC, datetime, timedelta

import allure

from tasks.healthcheck import (
    _healthcheck_receipt_fetch,
    _healthcheck_receipt_llm,
    _healthcheck_receipt_queue,
)


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


@allure.epic("Receipts")
@allure.feature("Admin")
class TestHealthcheckReceiptLLM:
    def test_ok_when_all_clear(self):
        results = {"llm_switch": "", "llm_exhausted": "", "llm_switch_count": "0"}
        assert _healthcheck_receipt_llm(results) is False

    def test_fails_on_switch(self):
        results = {
            "llm_switch": "2026-05-08T10:00:00Z | from: Groq | reason: 429 | to: OpenRouter",
            "llm_exhausted": "",
            "llm_switch_count": "1",
        }
        assert _healthcheck_receipt_llm(results) is True

    def test_fails_on_exhausted(self):
        results = {
            "llm_switch": "",
            "llm_exhausted": "2026-05-08T10:05:00Z | invoice: ABC-123",
            "llm_switch_count": "3",
        }
        assert _healthcheck_receipt_llm(results) is True

    def test_both_failures_reported(self, capsys):
        """When both switch and exhausted are set, both FAIL lines are printed."""
        results = {
            "llm_switch": "2026-05-08T10:00:00Z | from: Groq | reason: 429 | to: OpenRouter",
            "llm_exhausted": "2026-05-08T10:05:00Z | invoice: ABC-123",
            "llm_switch_count": "1",
        }
        failed = _healthcheck_receipt_llm(results)
        assert failed is True
        err = capsys.readouterr().err
        assert "LLM provider switched" in err
        assert "All LLM providers exhausted" in err

    def test_prints_switch_count_info(self, capsys):
        results = {"llm_switch": "", "llm_exhausted": "", "llm_switch_count": "5"}
        _healthcheck_receipt_llm(results)
        out = capsys.readouterr().out
        assert "5" in out


@allure.epic("Receipts")
@allure.feature("Admin")
class TestHealthcheckReceiptQueue:
    def test_ok_when_all_zero(self, capsys):
        assert _healthcheck_receipt_queue({"receipt_queue": "0|0|0|0"}) is False
        assert "empty" in capsys.readouterr().out

    def test_ok_when_key_missing(self, capsys):
        assert _healthcheck_receipt_queue({}) is False
        assert "empty" in capsys.readouterr().out

    def test_fails_on_pending(self, capsys):
        assert _healthcheck_receipt_queue({"receipt_queue": "3|0|0|0"}) is True
        assert "pending=3" in capsys.readouterr().err

    def test_fails_on_sleeping(self, capsys):
        assert _healthcheck_receipt_queue({"receipt_queue": "0|2|0|0"}) is True
        assert "sleeping=2" in capsys.readouterr().err

    def test_fails_on_in_progress(self, capsys):
        assert _healthcheck_receipt_queue({"receipt_queue": "0|0|1|0"}) is True
        assert "in_progress=1" in capsys.readouterr().err

    def test_fails_on_poisoned(self, capsys):
        assert _healthcheck_receipt_queue({"receipt_queue": "0|0|0|4"}) is True
        assert "poisoned=4" in capsys.readouterr().err

    def test_reports_all_non_zero_fields(self, capsys):
        assert _healthcheck_receipt_queue({"receipt_queue": "1|2|3|4"}) is True
        err = capsys.readouterr().err
        assert "pending=1" in err
        assert "sleeping=2" in err
        assert "in_progress=3" in err
        assert "poisoned=4" in err

    def test_does_not_call_sys_exit(self):
        # caller is responsible for exiting; helper only returns bool
        result = _healthcheck_receipt_queue({"receipt_queue": "1|0|0|0"})
        assert result is True


@allure.epic("Receipts")
@allure.feature("Admin")
class TestHealthcheckReceiptFetch:
    def test_ok_when_all_clear(self):
        results = {
            "receipt_fallback_count": "0",
            "receipt_journal_validation_failure": "",
            "receipt_journal_validation_failure_count": "0",
        }
        assert _healthcheck_receipt_fetch(results) is False

    def test_valid_journal_fallback_is_healthy(self):
        results = {
            "receipt_fallback_count": "3",
            "receipt_journal_validation_failure": "",
            "receipt_journal_validation_failure_count": "0",
        }
        assert _healthcheck_receipt_fetch(results) is False

    def test_fails_on_recent_journal_validation_failure(self):
        results = {
            "receipt_fallback_count": "3",
            "receipt_journal_validation_failure": (
                f"{_ago(hours=1)} | invoice: XYZ | reason: item total mismatch"
            ),
            "receipt_journal_validation_failure_count": "1",
        }
        assert _healthcheck_receipt_fetch(results) is True

    def test_ok_when_validation_failure_older_than_alert_window(self):
        results = {
            "receipt_journal_validation_failure": (
                f"{_ago(hours=49)} | invoice: XYZ | reason: item total mismatch"
            ),
            "receipt_journal_validation_failure_count": "1",
        }
        assert _healthcheck_receipt_fetch(results) is False

    def test_still_reports_stale_validation_failure_as_info(self, capsys):
        results = {
            "receipt_journal_validation_failure": (
                f"{_ago(hours=49)} | invoice: XYZ | reason: item total mismatch"
            ),
            "receipt_journal_validation_failure_count": "1",
        }
        _healthcheck_receipt_fetch(results)
        assert "XYZ" in capsys.readouterr().out

    def test_fails_on_unparseable_timestamp(self):
        results = {
            "receipt_journal_validation_failure": (
                "not-a-date | invoice: XYZ | reason: item total mismatch"
            ),
            "receipt_journal_validation_failure_count": "1",
        }
        assert _healthcheck_receipt_fetch(results) is True

    def test_naive_timestamp_treated_as_utc(self):
        stamp = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        results = {
            "receipt_journal_validation_failure": (
                f"{stamp} | invoice: XYZ | reason: item total mismatch"
            ),
            "receipt_journal_validation_failure_count": "1",
        }
        assert _healthcheck_receipt_fetch(results) is True

    def test_prints_fallback_count_info(self, capsys):
        results = {
            "receipt_fallback_count": "3",
            "receipt_journal_validation_failure": "",
            "receipt_journal_validation_failure_count": "0",
        }
        _healthcheck_receipt_fetch(results)
        out = capsys.readouterr().out
        assert "3" in out

    def test_prints_validation_failure_count_info(self, capsys):
        results = {
            "receipt_journal_validation_failure": "",
            "receipt_journal_validation_failure_count": "2",
        }
        _healthcheck_receipt_fetch(results)
        out = capsys.readouterr().out
        assert "validation failures, all time: 2" in out
