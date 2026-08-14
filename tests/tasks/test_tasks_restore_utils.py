"""Tests for restore_utils: database summaries, the restore plan, the confirmation
prompt, and the shared local/prod restore flow."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import allure
import pytest

import tasks.backups.restore_utils as ru


def _make_db(path: Path, rows: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE expenses (id INTEGER PRIMARY KEY, datetime TIMESTAMP)")
    con.executemany(
        "INSERT INTO expenses (datetime) VALUES (?)",
        [(value,) for value in rows],
    )
    con.commit()
    con.close()
    return path


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestDbSummary:
    def test_summarizes_rows_and_last_expense(self, tmp_path):
        db = _make_db(tmp_path / "a.db", ["2026-08-01 10:00", "2026-08-13 03:27"])
        summary = ru.summarize_db_file(db, "Snapshot")
        assert summary.rows == 2
        assert summary.last_expense == "2026-08-13 03:27"

    def test_counts_rows_newer_than_cutoff(self, tmp_path):
        """The cutoff is the incoming snapshot's newest expense: everything above it is
        what the restore drops."""
        db = _make_db(
            tmp_path / "a.db",
            ["2026-08-01 10:00", "2026-08-13 03:27", "2026-08-14 09:41"],
        )
        summary = ru.summarize_db_file(db, "Prod", cutoff="2026-08-13 03:27")
        assert summary.newer_than_cutoff == 1

    def test_unreadable_file_summarizes_as_empty(self, tmp_path):
        """The summary only feeds a prompt — a junk file must not crash the restore."""
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"not a database")
        assert ru.summarize_db_file(junk, "Snapshot").rows == 0

    def test_describe_marks_empty_database(self, tmp_path):
        db = _make_db(tmp_path / "a.db", [])
        assert ru.summarize_db_file(db, "Local").describe() == "Local: empty"

    def test_describe_names_side_rows_and_last_expense(self, tmp_path):
        db = _make_db(tmp_path / "a.db", ["2026-08-13 03:27"])
        assert ru.summarize_db_file(db, "Prod").describe() == (
            "Prod: 1 expenses, last 2026-08-13 03:27"
        )

    def test_rejects_a_cutoff_that_could_escape_the_remote_quoting(self):
        """The cutoff is interpolated into a double-quoted remote shell argument."""
        with pytest.raises(ValueError, match="unexpected timestamp"):
            ru._summary_sql('2026-08-13"; rm -rf /; "')

    def test_prod_summary_reads_a_backup_snapshot(self, monkeypatch):
        """Counting against the live WAL-backed file could straddle a transaction."""
        captured = {}

        def fake_capture(_c, cmd):
            captured["cmd"] = cmd
            return "4823|2026-08-14 09:41|37"

        monkeypatch.setattr(ru, "ssh_capture", fake_capture)
        summary = ru.summarize_prod_db(MagicMock(), cutoff="2026-08-13 03:27")
        assert ".backup" in captured["cmd"]
        assert (summary.rows, summary.last_expense, summary.newer_than_cutoff) == (
            4823,
            "2026-08-14 09:41",
            37,
        )


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestRestorePlan:
    def test_states_what_the_restore_drops(self):
        """A stale snapshot silently loses recent expenses — the count is the only
        signal the operator gets before committing."""
        current = ru.DbSummary(
            "Prod", rows=4823, last_expense="2026-08-14 09:41", newer_than_cutoff=37
        )
        incoming = ru.DbSummary("Snapshot", rows=4786, last_expense="2026-08-13 03:27")
        plan = ru.render_restore_plan(current, incoming)
        assert "Losing: 37 expenses recorded after 2026-08-13 03:27" in plan

    def test_no_losing_line_when_snapshot_is_current(self):
        current = ru.DbSummary("Prod", rows=4786, last_expense="2026-08-13 03:27")
        incoming = ru.DbSummary("Snapshot", rows=4786, last_expense="2026-08-13 03:27")
        assert "Losing" not in ru.render_restore_plan(current, incoming)

    def test_shows_both_sides(self):
        current = ru.DbSummary("Prod", rows=2, last_expense="2026-08-14 09:41")
        incoming = ru.DbSummary("Snapshot x.db", rows=1, last_expense="2026-08-13 03:27")
        plan = ru.render_restore_plan(current, incoming)
        assert "Prod: 2 expenses" in plan
        assert "Snapshot x.db: 1 expenses" in plan


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestConfirmRestore:
    def test_prod_confirmation_ignores_the_yes_flag(self, monkeypatch):
        """``--yes`` in shell history must not stand in for reading the diff."""
        asked = []
        monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "prod")
        ru.confirm_restore(ru.PROD_CONFIRM_WORD, yes=True)
        assert asked, "prod must prompt even with --yes"

    def test_local_confirmation_honours_the_yes_flag(self, monkeypatch):
        def refuse(_prompt):
            raise AssertionError("must not prompt with --yes on a local target")

        monkeypatch.setattr("builtins.input", refuse)
        ru.confirm_restore(ru.LOCAL_CONFIRM_WORD, yes=True)

    def test_wrong_word_aborts(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
        with pytest.raises(SystemExit) as exc:
            ru.confirm_restore(ru.PROD_CONFIRM_WORD)
        assert exc.value.code == 1

    def test_prompt_names_the_required_word(self, monkeypatch):
        prompts = []
        monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "prod")
        ru.confirm_restore(ru.PROD_CONFIRM_WORD)
        assert "'prod'" in prompts[0]


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestRunRestore:
    @pytest.fixture
    def _incoming(self, tmp_path):
        return _make_db(tmp_path / "incoming.db", ["2026-08-13 03:27"])

    def test_local_target_is_overwritten_and_preserved(self, tmp_path, _incoming, monkeypatch):
        target = _make_db(tmp_path / "data" / "dinary.db", ["2026-08-14 09:41"])
        monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
        ru.run_restore(
            MagicMock(),
            _incoming,
            incoming_label="Snapshot",
            target=target,
            prod=False,
            yes=False,
        )
        assert target.read_bytes() == _incoming.read_bytes()
        assert list(target.parent.glob("dinary.db.before-restore-*"))

    def test_prod_target_never_touches_the_local_file(self, tmp_path, _incoming, monkeypatch):
        """``--prod`` stages through a temp file; the operator's dev DB is not collateral."""
        target = _make_db(tmp_path / "data" / "dinary.db", ["2026-08-14 09:41"])
        before = target.read_bytes()
        monkeypatch.setattr(ru, "summarize_prod_db", lambda *a, **kw: ru.DbSummary("Prod", 5))
        monkeypatch.setattr(ru, "apply_restore_to_prod", lambda *a: None)
        monkeypatch.setattr("builtins.input", lambda _prompt: "prod")
        ru.run_restore(
            MagicMock(),
            _incoming,
            incoming_label="Snapshot",
            target=target,
            prod=True,
            yes=False,
        )
        assert target.read_bytes() == before

    def test_prod_path_hands_the_staged_file_to_the_prod_pipeline(
        self, tmp_path, _incoming, monkeypatch
    ):
        applied = []
        monkeypatch.setattr(ru, "summarize_prod_db", lambda *a, **kw: ru.DbSummary("Prod", 5))
        monkeypatch.setattr(ru, "apply_restore_to_prod", lambda _c, path: applied.append(path))
        monkeypatch.setattr("builtins.input", lambda _prompt: "prod")
        ru.run_restore(
            MagicMock(),
            _incoming,
            incoming_label="Snapshot",
            target=tmp_path / "unused.db",
            prod=True,
            yes=False,
        )
        assert applied == [_incoming]

    def test_refusal_leaves_the_target_untouched(self, tmp_path, _incoming, monkeypatch):
        target = _make_db(tmp_path / "data" / "dinary.db", ["2026-08-14 09:41"])
        before = target.read_bytes()
        monkeypatch.setattr("builtins.input", lambda _prompt: "no")
        with pytest.raises(SystemExit):
            ru.run_restore(
                MagicMock(),
                _incoming,
                incoming_label="Snapshot",
                target=target,
                prod=False,
                yes=False,
            )
        assert target.read_bytes() == before

    def test_plan_is_printed_before_the_prompt(self, tmp_path, _incoming, monkeypatch, capsys):
        target = _make_db(tmp_path / "data" / "dinary.db", ["2026-08-14 09:41"])
        monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
        ru.run_restore(
            MagicMock(),
            _incoming,
            incoming_label="Snapshot",
            target=target,
            prod=False,
            yes=False,
        )
        out = capsys.readouterr().out
        assert "Losing: 1 expenses recorded after 2026-08-13 03:27" in out


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestStagedDb:
    def test_materializes_bytes_and_cleans_up(self):
        with ru.staged_db(b"payload") as staged:
            assert staged.read_bytes() == b"payload"
            kept = staged
        assert not kept.exists()


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestTimestampNormalisation:
    """Stored timestamps carry a UTC offset that changes with DST."""

    def test_cutoff_compares_across_offsets(self, tmp_path):
        db = _make_db(
            tmp_path / "a.db",
            ["2026-10-25 02:30:00+02:00", "2026-10-25 02:30:00+01:00"],
        )
        # 02:30+01:00 is 03:30 in +02:00 terms, so exactly one row is newer.
        summary = ru.summarize_db_file(db, "Prod", cutoff="2026-10-25 02:30:00+02:00")
        assert summary.newer_than_cutoff == 1

    def test_newest_expense_is_reported_as_stored(self, tmp_path):
        """The prompt should echo what the app shows, not a UTC translation."""
        db = _make_db(tmp_path / "a.db", ["2026-08-14 13:17:31.543000+02:00"])
        assert ru.summarize_db_file(db, "Prod").last_expense == "2026-08-14 13:17:31.543000+02:00"


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore to prod")
class TestPostRestoreReport:
    def test_prod_state_is_read_back_after_the_swap(self, tmp_path, monkeypatch, capsys):
        """Reported from prod itself, not from what was uploaded — a swap that half
        worked must not print the numbers the operator expected to see."""
        incoming = _make_db(tmp_path / "incoming.db", ["2026-08-13 03:27"])
        monkeypatch.setattr(ru, "apply_restore_to_prod", lambda *a: None)
        monkeypatch.setattr("builtins.input", lambda _prompt: "prod")
        summaries = iter(
            [
                ru.DbSummary("Prod", rows=4823, last_expense="2026-08-14 09:41"),
                ru.DbSummary("Prod now holds", rows=4786, last_expense="2026-08-13 03:27"),
            ],
        )
        monkeypatch.setattr(ru, "summarize_prod_db", lambda *a, **kw: next(summaries))
        ru.run_restore(
            MagicMock(),
            incoming,
            incoming_label="Snapshot",
            target=tmp_path / "unused.db",
            prod=True,
            yes=False,
        )
        assert "Prod now holds: 4,786 expenses" in capsys.readouterr().out


class _TrackingConnection:
    """Proxy: ``sqlite3.Connection.close`` is read-only, so it cannot be spied on directly."""

    def __init__(self, con, closed: list[bool]) -> None:
        self._con = con
        self._closed = closed

    def execute(self, *args, **kwargs):
        return self._con.execute(*args, **kwargs)

    def close(self) -> None:
        self._closed.append(True)
        self._con.close()


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore utils")
class TestSummaryReleasesTheFile:
    """sqlite3's context manager ends the transaction but leaves the handle open, and on
    Windows that blocks renaming the very file the restore is about to replace."""

    @pytest.fixture
    def _closed(self, monkeypatch):
        closed: list[bool] = []
        real_connect = sqlite3.connect
        monkeypatch.setattr(
            ru.sqlite3,
            "connect",
            lambda *a, **kw: _TrackingConnection(real_connect(*a, **kw), closed),
        )
        return closed

    @pytest.fixture
    def _db(self, tmp_path):
        """Built before ``_closed`` patches the module-level ``sqlite3.connect``."""
        return _make_db(tmp_path / "a.db", ["2026-08-13 03:27"])

    @pytest.fixture
    def _junk(self, tmp_path):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"not a database")
        return junk

    def test_connection_is_closed_after_summarizing(self, _db, _closed):
        ru.summarize_db_file(_db, "Snapshot")
        assert _closed, "the summary must not leave an open handle on the database"

    def test_connection_is_closed_for_an_unreadable_file(self, _junk, _closed):
        """The junk-file path returns early — it must not skip the close."""
        ru.summarize_db_file(_junk, "Snapshot")
        assert _closed
