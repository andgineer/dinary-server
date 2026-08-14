"""Tests for ``restore-yadisk``: inventory + destructive replace.

Covers the discovery helpers (``yadisk_list_snapshots``,
``pick_snapshot``) and the end-to-end ``restore_from_yadisk`` task,
including the preserve-and-replace and integrity-check branches.
"""

import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import allure
import pytest

import tasks
import tasks.backups.backup_snapshots
import tasks.backups.backups_restore
from tasks.backups.backup_retention import _make_pattern
from tasks.backups.backup_snapshots import (
    BACKUP_FILENAME_PREFIX,
    BACKUP_FILENAME_SUFFIX,
    BACKUP_RCLONE_PATH,
    BACKUP_RCLONE_REMOTE,
    pick_snapshot,
)


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Cloud restore")
class TestRestoreFromYadiskHelpers:
    """Covers the non-destructive helpers (list parsing, snapshot picking); the
    file-writing path is covered in ``TestRestoreFromYadiskTask`` below."""

    def test_regex_round_trips_between_retention_and_restore(self):
        """retention and restore use the same pattern via _make_pattern —
        a drift (one side tightens the time precision, the other doesn't)
        would leave keepers the restorer cannot see, or vice versa.
        """
        pattern = _make_pattern(BACKUP_FILENAME_PREFIX, BACKUP_FILENAME_SUFFIX)
        assert pattern.match("dinary-2026-04-22T0317Z.db.zst")
        assert not pattern.match("dinary-2026-04-22.db.zst")
        assert not pattern.match("random.txt")

    def test_list_snapshots_parses_rclone_lsjson(self, monkeypatch):
        """The inventory parser must survive rclone's JSON shape and
        ignore non-matching filenames so human-uploaded noise in the
        same Yandex folder does not break the daily timer.
        """
        fake_json = json.dumps(
            [
                {"Name": "dinary-2026-04-22T0317Z.db.zst", "Size": 324000},
                {"Name": "dinary-2026-04-21T0317Z.db.zst", "Size": 322000},
                {"Name": "README.md", "Size": 100},
                {"Name": "dinary-malformed", "Size": 42},
            ],
        )

        def fake_check_output(cmd, text=True):
            assert "rclone" in cmd[0]
            assert "lsjson" in cmd[1]
            return fake_json

        monkeypatch.setattr(
            tasks.backups.backups_restore.subprocess, "check_output", fake_check_output
        )
        result = tasks.backups.backups_restore.yadisk_list_snapshots()
        assert result == [
            ("dinary-2026-04-21T0317Z.db.zst", 322000),
            ("dinary-2026-04-22T0317Z.db.zst", 324000),
        ]

    def test_pick_snapshot_latest_returns_newest(self):
        """A regression that picks ``[0]`` instead of the tail would silently
        restore the oldest available snapshot and lose weeks of data."""
        snaps = [
            ("dinary-2026-04-20T0317Z.db.zst", 100),
            ("dinary-2026-04-21T0317Z.db.zst", 200),
            ("dinary-2026-04-22T0317Z.db.zst", 300),
        ]
        picked = pick_snapshot(snaps, "latest")
        assert picked == ("dinary-2026-04-22T0317Z.db.zst", 300)

    def test_pick_snapshot_by_date_prefix_matches_any_time_suffix(self):
        """Operators type ``--snapshot 2026-04-21`` rather than
        memorizing the time stamp. Partial-prefix match must be
        supported.
        """
        snaps = [
            ("dinary-2026-04-20T0317Z.db.zst", 100),
            ("dinary-2026-04-21T0317Z.db.zst", 200),
            ("dinary-2026-04-22T0317Z.db.zst", 300),
        ]
        picked = pick_snapshot(snaps, "2026-04-21")
        assert picked == ("dinary-2026-04-21T0317Z.db.zst", 200)

    def test_pick_snapshot_returns_none_on_miss(self):
        """A typo in ``--snapshot`` must return None so the task
        surfaces the full inventory in its error message rather than
        silently restoring the wrong date.
        """
        snaps = [("dinary-2026-04-20T0317Z.db.zst", 100)]
        assert pick_snapshot(snaps, "1999-01-01") is None

    def test_pick_snapshot_on_empty_returns_none(self):
        """Fresh bucket case: calls with an empty list return None
        rather than raising, so the caller can emit a "no snapshots
        found" message instead of an opaque IndexError.
        """
        assert pick_snapshot([], "latest") is None


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Cloud restore")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "restore-yadisk shells out to the zstd and sqlite3 CLI "
        "binaries, which are not on the Windows CI runner path. The "
        "task itself only targets Linux (VM 1) / macOS (operator "
        "laptop), so skipping Windows here matches the deploy matrix."
    ),
)
class TestRestoreFromYadiskTask:
    """End-to-end tests for the destructive path: download, decompress,
    validate, preserve-and-replace. Uses real SQLite + zstd on
    ``tmp_path`` so the PRAGMA integrity_check path and the backup-
    before-overwrite behavior are exercised against actual file ops.
    """

    @pytest.fixture
    def _cwd(self, tmp_path, monkeypatch):
        """``restore_from_yadisk`` writes to ``./data/dinary.db``."""
        (tmp_path / "data").mkdir()
        monkeypatch.chdir(tmp_path)
        return tmp_path

    @staticmethod
    def _make_sqlite(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE expense (id INTEGER PRIMARY KEY, amount REAL);"
            "INSERT INTO expense (amount) VALUES (1.0), (2.0);",
        )
        con.close()

    @pytest.fixture
    def _mock_binaries_present(self, monkeypatch):
        """Pre-flight passes: local binaries present and the rclone remote configured."""
        monkeypatch.setattr(
            tasks.backups.backup_snapshots.shutil, "which", lambda name: f"/fake/{name}"
        )
        monkeypatch.setattr(
            tasks.backups.backups_restore, "ensure_local_yandex_rclone_configured", lambda: None
        )

    @pytest.fixture
    def _fake_snapshot(self, tmp_path, monkeypatch, _mock_binaries_present):
        """Stand up a fake Yandex-like snapshot on ``tmp_path`` and
        stub ``_yadisk_list_snapshots`` plus ``c.run`` to make rclone
        a file copy and zstd a real decompression.
        """
        snapshot_name = "dinary-2026-04-22T0317Z.db.zst"
        remote_root = tmp_path / "fake-yadisk"
        remote_root.mkdir()
        plain = remote_root / "plain.db"
        self._make_sqlite(plain)
        archive = remote_root / snapshot_name
        subprocess.run(
            ["zstd", "-q", "-19", str(plain), "-o", str(archive)],
            check=True,
        )

        monkeypatch.setattr(
            tasks.backups.backups_restore,
            "yadisk_list_snapshots",
            lambda: [(snapshot_name, archive.stat().st_size)],
        )

        class FakeContext:
            def run(self_inner, cmd):
                tokens = shlex.split(cmd)
                if tokens[0] == "rclone":
                    src = f"{BACKUP_RCLONE_REMOTE}:{BACKUP_RCLONE_PATH}/{snapshot_name}"
                    assert tokens[:2] == ["rclone", "copyto"]
                    assert tokens[2] == src
                    shutil.copyfile(archive, tokens[3])
                    return None
                if tokens[0] == "zstd":
                    subprocess.run(tokens, check=True)
                    return None
                raise AssertionError(f"unexpected command: {cmd}")

        return FakeContext(), snapshot_name

    def test_restore_writes_data_dinary_db_from_snapshot(
        self,
        _cwd,
        _fake_snapshot,
        capsys,
    ):
        """Happy path: no existing ``data/dinary.db``, ``--yes``
        implicit (no prompt when target is absent). Restored file
        must contain the rows from the snapshot.
        """
        c, _name = _fake_snapshot
        tasks.restore_from_yadisk.body(c, yes=True)

        target = _cwd / "data" / "dinary.db"
        assert target.exists()
        con = sqlite3.connect(target)
        count = con.execute("SELECT COUNT(*) FROM expense").fetchone()[0]
        con.close()
        assert count == 2

    def test_preserves_existing_db_before_overwrite(
        self,
        _cwd,
        _fake_snapshot,
        capsys,
    ):
        """An existing DB must be preserved before the replacement lands, even
        with ``--yes`` skipping the confirmation prompt."""
        target = _cwd / "data" / "dinary.db"
        self._make_sqlite(target)
        original_bytes = target.read_bytes()
        c, _name = _fake_snapshot

        tasks.restore_from_yadisk.body(c, yes=True)

        preserved = sorted(
            p for p in (_cwd / "data").iterdir() if p.name.startswith("dinary.db.before-restore-")
        )
        assert len(preserved) == 1
        assert preserved[0].read_bytes() == original_bytes

    def test_refuses_to_restore_corrupt_snapshot(
        self,
        _cwd,
        monkeypatch,
        tmp_path,
        _mock_binaries_present,
        capsys,
    ):
        """A snapshot failing ``PRAGMA integrity_check`` must leave the existing
        DB untouched — a loud stderr, not a silent swap."""
        snapshot_name = "dinary-2026-04-22T0317Z.db.zst"
        remote_root = tmp_path / "fake-yadisk"
        remote_root.mkdir()
        corrupt = remote_root / "corrupt.db"
        corrupt.write_bytes(b"not a sqlite file")
        archive = remote_root / snapshot_name
        subprocess.run(
            ["zstd", "-q", "-19", str(corrupt), "-o", str(archive)],
            check=True,
        )

        monkeypatch.setattr(
            tasks.backups.backups_restore,
            "yadisk_list_snapshots",
            lambda: [(snapshot_name, archive.stat().st_size)],
        )

        existing = _cwd / "data" / "dinary.db"
        self._make_sqlite(existing)
        existing_bytes = existing.read_bytes()

        class FakeContext:
            def run(self_inner, cmd):
                tokens = shlex.split(cmd)
                if tokens[0] == "rclone":
                    shutil.copyfile(archive, tokens[3])
                elif tokens[0] == "zstd":
                    subprocess.run(tokens, check=True)
                else:
                    raise AssertionError(f"unexpected: {cmd}")

        with pytest.raises(SystemExit) as excinfo:
            tasks.restore_from_yadisk.body(FakeContext(), yes=True)

        assert excinfo.value.code == 1
        assert existing.read_bytes() == existing_bytes
        preserved = [
            p for p in (_cwd / "data").iterdir() if p.name.startswith("dinary.db.before-restore-")
        ]
        assert preserved == []

    def test_list_only_is_readonly(
        self,
        _cwd,
        _fake_snapshot,
        capsys,
    ):
        """``--list-only`` must never touch the local filesystem — no downloads,
        no preservation, no overwrite."""
        target = _cwd / "data" / "dinary.db"
        self._make_sqlite(target)
        before = target.read_bytes()
        c, _name = _fake_snapshot

        tasks.restore_from_yadisk.body(c, list_only=True)

        assert target.read_bytes() == before
        assert (_cwd / "data").name == "data"
        preserved = [
            p for p in (_cwd / "data").iterdir() if p.name.startswith("dinary.db.before-restore-")
        ]
        assert preserved == []
        out = capsys.readouterr().out
        assert "dinary-2026-04-22T0317Z.db.zst" in out

    def test_exits_when_no_snapshots_available(
        self,
        _cwd,
        _mock_binaries_present,
        monkeypatch,
    ):
        """Empty-bucket case (fresh setup or post-wipe) must exit 1
        with a message pointing at the Yandex path, not crash with
        an IndexError deep in ``_pick_snapshot``.
        """
        monkeypatch.setattr(tasks.backups.backups_restore, "yadisk_list_snapshots", lambda: [])
        with pytest.raises(SystemExit) as excinfo:
            tasks.restore_from_yadisk.body(MagicMock())
        assert excinfo.value.code == 1

    def test_exits_when_snapshot_arg_does_not_match(
        self,
        _cwd,
        _fake_snapshot,
        capsys,
    ):
        """Typo in ``--snapshot``: task must surface the available
        inventory in stderr and exit 1, so the operator sees valid
        keys to retry with.
        """
        c, _name = _fake_snapshot
        with pytest.raises(SystemExit) as excinfo:
            tasks.restore_from_yadisk.body(c, snapshot="1999-01-01")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "1999-01-01" in err
        assert "dinary-2026-04-22T0317Z.db.zst" in err

    def test_exits_when_local_tools_missing(
        self,
        _cwd,
        monkeypatch,
    ):
        """Pre-flight must catch missing rclone/sqlite3/zstd with a
        single consolidated error message, not fail mid-pipeline
        after the download has already started.
        """
        monkeypatch.setattr(tasks.backups.backup_snapshots.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as excinfo:
            tasks.restore_from_yadisk.body(MagicMock())
        assert excinfo.value.code == 1

    def test_ensure_local_yadisk_called_before_listing(
        self,
        _cwd,
        monkeypatch,
        _mock_binaries_present,
    ):
        """``ensure_local_yandex_rclone_configured`` must be called before
        ``yadisk_list_snapshots`` so a missing yandex: remote triggers the
        credential prompt rather than a cryptic rclone error.
        """
        calls: list[str] = []
        monkeypatch.setattr(
            tasks.backups.backups_restore,
            "ensure_local_yandex_rclone_configured",
            lambda: calls.append("ensure"),
        )
        monkeypatch.setattr(
            tasks.backups.backups_restore,
            "yadisk_list_snapshots",
            lambda: calls.append("list") or [],
        )
        with pytest.raises(SystemExit):
            tasks.restore_from_yadisk.body(MagicMock())
        assert calls.index("ensure") < calls.index("list")


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore to prod")
class TestRestoreFromYadiskTarget:
    """The snapshot is downloaded and verified before either target is touched, and
    ``--prod`` is what decides which one that is."""

    @pytest.fixture
    def _patched(self, monkeypatch, tmp_path):
        (tmp_path / "data").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            tasks.backups.backup_snapshots.shutil, "which", lambda name: f"/fake/{name}"
        )
        monkeypatch.setattr(
            tasks.backups.backups_restore, "ensure_local_yandex_rclone_configured", lambda: None
        )
        monkeypatch.setattr(
            tasks.backups.backups_restore,
            "yadisk_list_snapshots",
            lambda: [("dinary-2026-04-22T0317Z.db.zst", 1000)],
        )
        monkeypatch.setattr(
            tasks.backups.backups_restore,
            "_download_and_verify",
            lambda c, picked, workpath: workpath / "restored.db",
        )
        return monkeypatch

    def _capture(self, patched):
        calls = []
        patched.setattr(
            tasks.backups.backups_restore,
            "run_restore",
            lambda _c, _staged, **kw: calls.append(kw),
        )
        return calls

    def test_defaults_to_the_local_database(self, _patched):
        calls = self._capture(_patched)
        tasks.restore_from_yadisk.body(MagicMock(), yes=True)
        assert calls[0]["prod"] is False
        assert calls[0]["target"] == Path("data/dinary.db")

    def test_prod_flag_is_forwarded(self, _patched):
        calls = self._capture(_patched)
        tasks.restore_from_yadisk.body(MagicMock(), prod=True)
        assert calls[0]["prod"] is True

    def test_snapshot_name_reaches_the_confirmation_label(self, _patched):
        """The operator picks between snapshots by name; the plan must say which one."""
        calls = self._capture(_patched)
        tasks.restore_from_yadisk.body(MagicMock(), yes=True)
        assert "dinary-2026-04-22T0317Z.db.zst" in calls[0]["incoming_label"]

    def test_list_only_never_restores(self, _patched, capsys):
        calls = self._capture(_patched)
        tasks.restore_from_yadisk.body(MagicMock(), list_only=True)
        assert calls == []
