"""Tests for the prod restore pipeline: ordering of the service/swap/resync steps and
the guarantee that prod comes back up even when the swap fails."""

from pathlib import Path
from unittest.mock import MagicMock

import allure
import pytest

import tasks.backups.restore_prod as rp


@pytest.fixture
def _spy(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(rp, "ssh_run", lambda _c, cmd: calls.append(f"vm1: {cmd}"))
    monkeypatch.setattr(rp, "ssh_sudo", lambda _c, cmd: calls.append(f"vm1-sudo: {cmd}"))
    monkeypatch.setattr(rp, "ssh_replica", lambda _c, cmd: calls.append(f"vm2: {cmd}"))
    monkeypatch.setattr(
        rp,
        "upload_remote_bytes",
        lambda local, remote: calls.append(f"upload: {local} -> {remote}"),
    )
    monkeypatch.setattr(rp, "ssh_capture", lambda _c, _cmd: "ok")
    monkeypatch.setattr(rp, "replica_host", lambda: "ubuntu@replica")
    return calls


def _index(calls: list[str], needle: str) -> int:
    return next(i for i, call in enumerate(calls) if needle in call)


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore to prod")
class TestApplyRestoreToProd:
    def test_step_order(self, _spy, tmp_path):
        """Both services must be down before the file moves, and the LTX trees must be
        wiped before litestream starts pushing again."""
        rp.apply_restore_to_prod(MagicMock(), tmp_path / "incoming.db")
        stop_app = _index(_spy, "stop dinary")
        stop_ltx = _index(_spy, "stop litestream")
        swap = _index(_spy, "mv /tmp/dinary-restore-incoming.db")
        wipe_vm2 = _index(_spy, "vm2: rm -rf")
        start_ltx = _index(_spy, "start litestream")
        start_app = _index(_spy, "start dinary")
        assert stop_app < stop_ltx < swap < wipe_vm2 < start_ltx < start_app

    def test_previous_db_is_preserved_with_a_timestamp(self, _spy, tmp_path):
        """A fixed name would let a second restore destroy the only pre-restore copy."""
        rp.apply_restore_to_prod(MagicMock(), tmp_path / "incoming.db")
        preserve = _spy[_index(_spy, "before-restore-")]
        assert "cp /home/ubuntu/dinary/data/dinary.db" in preserve
        assert preserve.rstrip().endswith("Z")

    def test_upload_lands_in_tmp_not_on_the_live_database(self, _spy, tmp_path):
        """A truncated upload must not be able to become the production database."""
        rp.apply_restore_to_prod(MagicMock(), tmp_path / "incoming.db")
        upload = _spy[_index(_spy, "upload:")]
        assert upload.endswith("-> /tmp/dinary-restore-incoming.db")

    def test_stale_wal_is_removed_with_the_swap(self, _spy, tmp_path):
        """A WAL left from the replaced database would corrupt the restored one."""
        rp.apply_restore_to_prod(MagicMock(), tmp_path / "incoming.db")
        swap = _spy[_index(_spy, "mv /tmp/dinary-restore-incoming.db")]
        assert "-wal" in swap
        assert "-shm" in swap

    def test_both_ltx_trees_are_wiped(self, _spy, tmp_path):
        """Wiping only VM2 leaves VM1's shadow tree at the replaced database's txids."""
        rp.apply_restore_to_prod(MagicMock(), tmp_path / "incoming.db")
        assert "vm1: rm -rf /home/ubuntu/dinary/data/.dinary.db-litestream" in _spy
        assert "vm2: rm -rf /var/lib/litestream/dinary" in _spy

    def test_services_come_back_when_the_swap_fails(self, monkeypatch, _spy, tmp_path):
        """A failure mid-swap must leave prod running, not silently down."""

        def explode(local, remote):
            raise RuntimeError("upload died")

        monkeypatch.setattr(rp, "upload_remote_bytes", explode)
        with pytest.raises(RuntimeError, match="upload died"):
            rp.apply_restore_to_prod(MagicMock(), tmp_path / "incoming.db")
        assert _index(_spy, "start litestream") > _index(_spy, "stop litestream")
        assert any("start dinary" in call for call in _spy)

    def test_service_state_is_reported(self, monkeypatch, tmp_path, capsys):
        """A restore that ends with a dead service must not read as success."""
        monkeypatch.setattr(rp, "ssh_run", lambda _c, _cmd: None)
        monkeypatch.setattr(rp, "ssh_sudo", lambda _c, _cmd: None)
        monkeypatch.setattr(rp, "ssh_replica", lambda _c, _cmd: None)
        monkeypatch.setattr(rp, "upload_remote_bytes", lambda _l, _r: None)
        monkeypatch.setattr(rp, "replica_host", lambda: "ubuntu@replica")
        monkeypatch.setattr(rp, "ssh_capture", lambda _c, _cmd: "active")
        rp.apply_restore_to_prod(MagicMock(), Path("incoming.db"))
        out = capsys.readouterr().out
        assert "dinary: active" in out
        assert "litestream: active" in out


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore to prod")
class TestReplicaPrimitives:
    def test_wipe_targets_the_canonical_paths(self, _spy):
        rp.wipe_ltx_trees(MagicMock())
        assert _spy == [
            "vm1: rm -rf /home/ubuntu/dinary/data/.dinary.db-litestream",
            "vm2: rm -rf /var/lib/litestream/dinary",
        ]

    def test_start_verifies_the_service_came_up(self, _spy):
        """Starting without checking would report success on a unit that died on boot."""
        rp.start_litestream(MagicMock())
        assert any("is-active litestream" in call for call in _spy)
