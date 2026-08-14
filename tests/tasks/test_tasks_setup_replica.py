"""Tests for ``inv setup-replica`` orchestration and its scripts, routed through
:mod:`tasks.backups.backups_replica`: sudo scope of the litestream.yml chown/chmod
step, the apt/litestream-dir shell builders, the task's step composition, and the
explicit trust-refresh task used after a VM re-provision."""

from pathlib import Path
from unittest.mock import MagicMock

import allure
import pytest

import tasks
import tasks.backups.backups_replica
import tasks.devtools.constants
import tasks.ssh_utils


_FAKE_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIABCDEFGHIJKLMNOPQRSTUVWXYZabcd dinary-vm1-litestream"
)


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestLitestreamSetupPermissions:
    """A naive ``sudo chown ... && chmod ...`` only escalates the ``chown``, leaving
    ``chmod`` to fail as ``ubuntu``; the fix wraps both in ``bash -c`` under one sudo."""

    @pytest.fixture
    def _spy(self, monkeypatch, tmp_path):
        calls: list[tuple[str, str]] = []

        deploy_dir = tmp_path / ".deploy"
        deploy_dir.mkdir()
        (deploy_dir / ".env").write_text(
            "# TestLitestreamSetupPermissions fixture\n"
            "DINARY_DEPLOY_HOST=ubuntu@test-primary\n"
            "DINARY_REPLICA_HOST=ubuntu@test-replica\n"
            "DINARY_TUNNEL=tailscale\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        def fake_ssh(_c, cmd: str) -> None:
            calls.append(("ssh", cmd))

        def fake_ssh_sudo(_c, cmd: str) -> None:
            calls.append(("sudo", cmd))

        def fake_ssh_replica(_c, cmd: str) -> None:
            calls.append(("ssh_replica", cmd))

        def fake_ssh_capture(_c, _cmd: str) -> str:
            calls.append(("capture", _cmd))
            return _FAKE_PUBKEY + "\n"

        def fake_write_remote_file(_c, _path: str, _content: str) -> None:
            calls.append(("write", _path))

        def fake_create_service(*_args, **_kwargs) -> None:
            calls.append(("service", "litestream"))

        def fake_write_remote_replica_file(_c, _path: str, _content: str) -> None:
            pass

        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_run", fake_ssh)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_sudo", fake_ssh_sudo)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_replica", fake_ssh_replica)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_capture", fake_ssh_capture)
        monkeypatch.setattr(
            tasks.backups.backups_replica, "write_remote_file", fake_write_remote_file
        )
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "write_remote_replica_file",
            fake_write_remote_replica_file,
        )
        monkeypatch.setattr(tasks.backups.backups_replica, "create_service", fake_create_service)
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "ensure_yandex_rclone_configured",
            lambda _c: calls.append(("yandex", "configured")),
        )
        monkeypatch.setattr(
            tasks.backups.backups_replica, "replica_host", lambda: "ubuntu@dinary-replica"
        )
        return calls

    def test_chown_and_chmod_run_inside_a_single_sudo_bash_c(self, _spy):
        """A bare ``&&`` chain would leave ``chmod`` running as the SSH user."""
        tasks.setup_replica.body(MagicMock(), no_swap=True)
        perm_call = next(
            (cmd for kind, cmd in _spy if kind == "sudo" and "chmod 644" in cmd),
            None,
        )
        assert perm_call is not None, "setup_replica must emit a chmod call for litestream config"
        assert perm_call.startswith("bash -c '"), (
            "permissions fix must be wrapped in bash -c so outer sudo "
            f"covers both chown and chmod; got: {perm_call!r}"
        )
        assert "chown root:root /etc/litestream.yml" in perm_call
        assert "chmod 644 /etc/litestream.yml" in perm_call
        assert perm_call.rstrip().endswith("'"), "bash -c payload must be fully quoted"

    def test_permissions_target_the_canonical_config_path(self, _spy):
        """A renamed-but-not-updated constant would leave the file at whatever
        ``sudo tee`` left on disk — group-readable on a multi-user VM."""
        tasks.setup_replica.body(MagicMock(), no_swap=True)
        perm_call = next(
            (cmd for kind, cmd in _spy if kind == "sudo" and "chmod" in cmd),
            None,
        )
        assert perm_call is not None
        assert tasks.devtools.constants.REMOTE_LITESTREAM_CONFIG_PATH in perm_call

    def test_restarts_litestream_after_the_service_is_created(self, _spy):
        """``enable --now`` does nothing to a running unit, so without an explicit
        restart a re-apply keeps the previous binary and config live."""
        tasks.setup_replica.body(MagicMock(), no_swap=True)
        kinds = [(kind, cmd) for kind, cmd in _spy if kind in {"service", "sudo"}]
        assert ("sudo", "systemctl restart litestream") in kinds
        service_idx = kinds.index(("service", "litestream"))
        restart_idx = kinds.index(("sudo", "systemctl restart litestream"))
        assert restart_idx > service_idx, "restart must follow the unit + config write"


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestLitestreamConfig:
    """Pins the generated ``/etc/litestream.yml`` — a wrong key name here is accepted
    silently by Litestream and only shows up as a replication surprise weeks later."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setattr(
            tasks.backups.backups_replica, "replica_host", lambda: "ubuntu@dinary-replica"
        )
        monkeypatch.setattr(tasks.backups.backups_replica, "litestream_retention", lambda: "168h")

    def test_disables_concurrent_writes(self):
        """Concurrent writes are on by default and make a failed upload restart from
        byte zero instead of resuming."""
        config = tasks.backups.backups_replica._build_litestream_config()
        assert "      concurrent-writes: false\n" in config

    def test_splits_replica_host_into_user_and_host_port(self):
        """The ``user@`` prefix must not leak into ``host:`` — Litestream would then
        resolve a hostname that does not exist."""
        config = tasks.backups.backups_replica._build_litestream_config()
        assert "      host: dinary-replica:22\n" in config
        assert "      user: ubuntu\n" in config

    def test_defaults_user_to_ubuntu_when_host_has_no_user_prefix(self, monkeypatch):
        """A bare host in ``.deploy/.env`` must still produce a usable SFTP login."""
        monkeypatch.setattr(tasks.backups.backups_replica, "replica_host", lambda: "dinary-replica")
        config = tasks.backups.backups_replica._build_litestream_config()
        assert "      host: dinary-replica:22\n" in config
        assert "      user: ubuntu\n" in config

    def test_replica_path_and_retention_come_from_constants_and_env(self):
        """Drift between the config path and ``REPLICA_LITESTREAM_DIR`` would restore
        from a tree nothing replicates into."""
        config = tasks.backups.backups_replica._build_litestream_config()
        replica_dir = tasks.devtools.constants.REPLICA_LITESTREAM_DIR
        replica_db = tasks.devtools.constants.REPLICA_DB_NAME
        assert f"      path: {replica_dir}/{replica_db}\n" in config
        assert "  retention: 168h\n" in config
        assert f"  interval: {tasks.devtools.constants.LITESTREAM_SNAPSHOT_INTERVAL}\n" in config


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestSetupReplicaScripts:
    """Pins the apt and litestream-dir shell builders (swap and ssh-hardening are
    pinned in their own classes) — a regression here hangs on a debconf prompt or
    leaves the dir unwritable by sftp."""

    def test_apt_runs_noninteractive_so_debconf_cannot_hang(self):
        """Without ``DEBIAN_FRONTEND=noninteractive``, a fresh Ubuntu image can
        block forever on a postfix/grub debconf dialog."""
        script = tasks.backups.backups_replica._build_setup_replica_packages_script()
        assert "export DEBIAN_FRONTEND=noninteractive" in script

    def test_apt_installs_unattended_upgrades(self):
        """The only automated CVE-patch channel a replica VM has — nobody runs
        ``inv deploy`` on it."""
        script = tasks.backups.backups_replica._build_setup_replica_packages_script()
        assert "apt-get install -y -qq unattended-upgrades" in script

    def test_apt_refreshes_package_index_before_install(self):
        """Without ``apt-get update`` first, a stale package index fails install
        with "Unable to locate package"."""
        script = tasks.backups.backups_replica._build_setup_replica_packages_script()
        update_idx = script.index("apt-get update -qq")
        install_idx = script.index("apt-get install -y -qq unattended-upgrades")
        assert update_idx < install_idx

    def test_apt_script_elevates_whole_block(self):
        """A semicolon-chain ``sudo apt-get update; apt-get install`` would only
        elevate the first command and the install would fail with EACCES."""
        script = tasks.backups.backups_replica._build_setup_replica_packages_script()
        assert script.startswith("sudo bash <<'DINARY_REPLICA_PKG_EOF'\n")
        assert script.rstrip().endswith("DINARY_REPLICA_PKG_EOF")

    def test_litestream_dir_path_is_the_canonical_constant(self):
        """Must match :data:`REPLICA_LITESTREAM_DIR` (also baked into VM1's
        Litestream replica URL) — drift here fails the first WAL push."""
        script = tasks.backups.backups_replica._build_setup_replica_litestream_dir_script()
        assert tasks.devtools.constants.REPLICA_LITESTREAM_DIR == "/var/lib/litestream"
        assert f"mkdir -p {tasks.devtools.constants.REPLICA_LITESTREAM_DIR}" in script

    def test_litestream_dir_mode_is_0750_not_world_readable(self):
        """The replica stream carries full pre-compaction row data (amounts,
        descriptions) — must not be world-readable on a shared VM."""
        script = tasks.backups.backups_replica._build_setup_replica_litestream_dir_script()
        assert f"chmod 750 {tasks.devtools.constants.REPLICA_LITESTREAM_DIR}" in script
        assert "chmod 755" not in script
        assert "chmod 777" not in script

    def test_litestream_dir_owned_by_ubuntu(self):
        """Litestream connects as ``ubuntu`` over SFTP; wrong ownership fails the
        first WAL segment write with EPERM."""
        script = tasks.backups.backups_replica._build_setup_replica_litestream_dir_script()
        assert f"chown ubuntu:ubuntu {tasks.devtools.constants.REPLICA_LITESTREAM_DIR}" in script

    def test_litestream_dir_script_elevates_whole_block(self):
        """A bare ``mkdir`` (no elevation) would fail with EACCES on ``/var/lib/``."""
        script = tasks.backups.backups_replica._build_setup_replica_litestream_dir_script()
        assert script.startswith("sudo bash <<'DINARY_REPLICA_DIR_EOF'\n")
        assert script.rstrip().endswith("DINARY_REPLICA_DIR_EOF")

    def test_litestream_dir_script_verifies_final_state(self):
        """A trailing ``ls -ld`` surfaces mode/owner so a silent umask drift is
        visible immediately, not discovered later when SFTP write fails."""
        script = tasks.backups.backups_replica._build_setup_replica_litestream_dir_script()
        assert f"ls -ld {tasks.devtools.constants.REPLICA_LITESTREAM_DIR}" in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestSetupReplicaTask:
    """Linear composition: VM2 bootstrap steps, trust step (VM1 pubkey to VM2's
    authorized_keys), then VM1-side Litestream setup. VM2's sshd is intentionally
    not restricted to Tailscale — the operator needs public-IP access. Trust must
    land strictly before ``create_service`` so the first Litestream start finds
    SSH auth ready; a VM re-provision goes through ``inv replica-reset-trust``."""

    @pytest.fixture
    def _spy(self, monkeypatch, tmp_path):
        class Spy:
            replica_calls: list[str]
            ssh_calls: list[str]
            sudo_calls: list[str]
            capture_calls: list[str]

            def __init__(self) -> None:
                self.replica_calls = []
                self.ssh_calls = []
                self.sudo_calls = []
                self.capture_calls = []

        spy = Spy()
        spy.yandex_calls = []  # type: ignore[attr-defined]

        def fake_ssh_replica(_c, cmd: str) -> None:
            spy.replica_calls.append(cmd)

        def fake_ssh_run(_c, cmd: str) -> None:
            spy.ssh_calls.append(cmd)

        def fake_ssh_sudo(_c, cmd: str) -> None:
            spy.sudo_calls.append(cmd)

        def fake_ssh_capture(_c, cmd: str) -> str:
            spy.capture_calls.append(cmd)
            return _FAKE_PUBKEY + "\n"

        def fake_write_remote_file(_c, _path: str, _content: str) -> None:
            pass

        def fake_create_service(*_args, **_kwargs) -> None:
            pass

        def fake_ensure_yandex(_c) -> None:
            spy.yandex_calls.append("ensure")  # type: ignore[attr-defined]

        deploy_dir = tmp_path / ".deploy"
        deploy_dir.mkdir()
        (deploy_dir / ".env").write_text(
            "# TestSetupReplicaTask fixture — not a real deploy env\n"
            "DINARY_DEPLOY_HOST=ubuntu@test-primary\n"
            "DINARY_REPLICA_HOST=ubuntu@test-replica\n"
            "DINARY_TUNNEL=tailscale\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        def fake_write_remote_replica_file(_c, _path: str, _content: str) -> None:
            pass

        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_replica", fake_ssh_replica)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_run", fake_ssh_run)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_sudo", fake_ssh_sudo)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_capture", fake_ssh_capture)
        monkeypatch.setattr(
            tasks.backups.backups_replica, "write_remote_file", fake_write_remote_file
        )
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "write_remote_replica_file",
            fake_write_remote_replica_file,
        )
        monkeypatch.setattr(tasks.backups.backups_replica, "create_service", fake_create_service)
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "ensure_yandex_rclone_configured",
            fake_ensure_yandex,
        )
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "replica_host",
            lambda: "ubuntu@dinary-replica",
        )
        return spy

    def test_runs_all_twelve_replica_steps(self, _spy):
        """Dropping any of the 12 VM2 steps leaves the replica half-configured."""
        tasks.setup_replica.body(MagicMock())
        assert len(_spy.replica_calls) == 12

    def test_bootstrap_order_is_stable(self, _spy):
        """Order is load-bearing: apt before fail2ban (needs apt, and the box
        shouldn't accept external traffic before it's active), daemon-reload before
        enable, and authorized_key install last so Litestream can SFTP in as soon
        as this function returns."""
        tasks.setup_replica.body(MagicMock())
        pkg_script = tasks.backups.backups_replica._build_setup_replica_packages_script()
        harden_script = tasks.ssh_utils.build_harden_sshd_script()
        f2b_script = tasks.ssh_utils.build_install_fail2ban_script()
        dir_script = tasks.backups.backups_replica._build_setup_replica_litestream_dir_script()
        swap_script = tasks.ssh_utils.build_setup_swap_script(size_gb=1)
        backup_pkg_script = (
            tasks.backups.backups_replica._build_setup_replica_backup_packages_script()
        )
        litestream_script = tasks.ssh_utils.litestream_install_script()
        trust_script = tasks.ssh_utils.build_install_authorized_key_script(_FAKE_PUBKEY)
        assert _spy.replica_calls == [
            pkg_script,
            harden_script,
            f2b_script,
            dir_script,
            swap_script,
            backup_pkg_script,
            litestream_script,
            f"sudo chmod 0755 {tasks.devtools.constants.BACKUP_SCRIPT_PATH}",
            f"sudo chmod 0755 {tasks.devtools.constants.BACKUP_RETENTION_SCRIPT_PATH}",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable --now dinary-backup.timer",
            trust_script,
        ]

    def test_does_not_rebind_replica_sshd_to_tailscale(self, _spy):
        """The operator reaches VM2 by its public IP; a tailscale-only drop-in
        here would silently lock them out."""
        tasks.setup_replica.body(MagicMock())
        ts_script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert ts_script not in _spy.replica_calls
        assert ts_script not in _spy.ssh_calls

    def test_swap_size_is_forwarded(self, _spy):
        """``--swap-size-gb`` must reach the builder unchanged, not get silently
        coerced back to 1."""
        tasks.setup_replica.body(MagicMock(), swap_size_gb=4)
        swap_script = next(
            (c for c in _spy.replica_calls if "fallocate" in c),
            None,
        )
        assert swap_script is not None
        assert "fallocate -l 4G /swapfile" in swap_script

    def test_swap_size_defaults_to_one_gigabyte(self, _spy):
        """The 956 MiB VM2 needs 1 GB swap minimum to survive ``apt-get upgrade``
        under concurrent Litestream SFTP sessions."""
        tasks.setup_replica.body(MagicMock())
        swap_script = next(
            (c for c in _spy.replica_calls if "fallocate" in c),
            None,
        )
        assert swap_script is not None
        assert "fallocate -l 1G /swapfile" in swap_script

    def test_trust_step_installs_vm1_pubkey_on_vm2(self, _spy):
        """A truncation or re-quoting bug here would produce a "Permission
        denied (publickey)" on the first SFTP connect with no obvious cause."""
        tasks.setup_replica.body(MagicMock())
        trust_call = next(
            (c for c in _spy.replica_calls if _FAKE_PUBKEY in c),
            None,
        )
        assert trust_call is not None, "pubkey must land verbatim in an ssh_replica payload"
        assert "authorized_keys" in trust_call

    def test_trust_step_fetches_vm1_pubkey_via_ssh_capture(self, _spy):
        """``ssh_run`` drops stdout — routing the keygen through it would leave
        authorized_keys operating on an empty string and silently succeed."""
        tasks.setup_replica.body(MagicMock())
        assert any("ssh-keygen -t ed25519" in c for c in _spy.capture_calls), (
            "VM1 pubkey fetch must use ssh_capture"
        )

    def test_calls_ensure_yandex_rclone_configured(self, _spy):
        """Dropping this call would silently leave the off-site backup path
        un-configured — Litestream replicates fine, but ``dinary-backup.service``
        fails nightly against a missing ``yandex:`` remote."""
        tasks.setup_replica.body(MagicMock())
        assert _spy.yandex_calls == ["ensure"], (
            "setup_replica must call ensure_yandex_rclone_configured exactly once"
        )

    def test_yandex_bootstrap_runs_before_litestream_service(self, _spy):
        """If run after ``create_service``, a typo-driven credential retry would
        leave the replicator pushing while credentials are still being typed."""
        call_log: list[str] = []

        def observe_yandex(_c) -> None:
            call_log.append("yandex")

        def observe_create(*_a, **_kw) -> None:
            call_log.append("create_service")

        import tasks.backups.backups_replica as br

        br_module = br
        orig_create = br_module.create_service
        orig_yandex = br_module.ensure_yandex_rclone_configured
        orig_write_replica = br_module.write_remote_replica_file
        br_module.create_service = observe_create
        br_module.ensure_yandex_rclone_configured = observe_yandex
        br_module.write_remote_replica_file = lambda *_a, **_kw: None
        try:
            tasks.setup_replica.body(MagicMock())
        finally:
            br_module.create_service = orig_create
            br_module.ensure_yandex_rclone_configured = orig_yandex
            br_module.write_remote_replica_file = orig_write_replica
        assert call_log == ["yandex", "create_service"]

    def test_trust_step_adds_vm2_host_key_to_vm1_known_hosts(self, _spy):
        """``known_hosts`` on VM1 must have an entry before the Litestream service
        starts, or every SFTP handshake fails under ``StrictHostKeyChecking yes``."""
        tasks.setup_replica.body(MagicMock())
        host_script = tasks.ssh_utils.build_add_known_host_script("dinary-replica")
        assert host_script in _spy.ssh_calls


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestReplicaResetTrustTask:
    """Pins the two destructive operations (known_hosts reset, pubkey re-install)
    so a refactor of this task can't silently diverge from ``inv setup-replica``'s
    trust step."""

    @pytest.fixture
    def _spy(self, monkeypatch, tmp_path):
        class Spy:
            replica_calls: list[str]
            ssh_calls: list[str]
            capture_calls: list[str]

            def __init__(self) -> None:
                self.replica_calls = []
                self.ssh_calls = []
                self.capture_calls = []

        spy = Spy()

        def fake_ssh_replica(_c, cmd: str) -> None:
            spy.replica_calls.append(cmd)

        def fake_ssh_run(_c, cmd: str) -> None:
            spy.ssh_calls.append(cmd)

        def fake_ssh_capture(_c, cmd: str) -> str:
            spy.capture_calls.append(cmd)
            return _FAKE_PUBKEY + "\n"

        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_replica", fake_ssh_replica)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_run", fake_ssh_run)
        monkeypatch.setattr(tasks.backups.backups_replica, "ssh_capture", fake_ssh_capture)
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "replica_host",
            lambda: "ubuntu@dinary-replica",
        )
        return spy

    def test_wipes_and_re_adds_vm2_host_key(self, _spy):
        """Without ``ssh-keygen -R`` first, OpenSSH keeps matching the old entry
        and refuses the new host key."""
        tasks.replica_reset_trust.body(MagicMock())
        reset_script = tasks.ssh_utils.build_reset_known_host_script("dinary-replica")
        assert reset_script in _spy.ssh_calls

    def test_reinstalls_pubkey_on_vm2(self, _spy):
        """Uses the same idempotent installer as ``setup-replica`` — a parallel
        implementation would drift under refactors."""
        tasks.replica_reset_trust.body(MagicMock())
        install_script = tasks.ssh_utils.build_install_authorized_key_script(_FAKE_PUBKEY)
        assert install_script in _spy.replica_calls

    def test_does_not_touch_the_database(self, _spy):
        """Wiping the DB / restarting litestream is ``inv setup-resync``'s job —
        mixing it in here would make a trust refresh destroy WAL state."""
        tasks.replica_reset_trust.body(MagicMock())
        all_calls = _spy.replica_calls + _spy.ssh_calls
        joined = "\n".join(all_calls)
        assert "rm -f /var/lib/litestream" not in joined
        assert "systemctl stop litestream" not in joined
        assert "systemctl start litestream" not in joined


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestSetupResyncTask:
    """The task is now composition only — the shell of each step is pinned in
    :file:`test_tasks_restore_prod.py`; what matters here is that the three steps
    run, and in an order where neither wipe races the running replicator."""

    @pytest.fixture
    def _spy(self, monkeypatch):
        calls: list[str] = []
        for name in ("stop_litestream", "wipe_ltx_trees", "start_litestream"):
            monkeypatch.setattr(
                tasks.backups.backups_replica,
                name,
                lambda _c, _name=name: calls.append(_name),
            )
        return calls

    def test_wipes_between_stop_and_start(self, _spy):
        tasks.replica_resync.body(MagicMock())
        assert _spy == ["stop_litestream", "wipe_ltx_trees", "start_litestream"]


@allure.epic("Infrastructure")
@allure.feature("Backup")
@allure.story("Restore to prod")
class TestRestoreReplicaTask:
    """``restore-replica`` writes the local database unless ``--prod`` is given."""

    @pytest.fixture
    def _patched(self, monkeypatch, tmp_path):
        (tmp_path / "data").mkdir()
        deploy_dir = tmp_path / ".deploy"
        deploy_dir.mkdir()
        (deploy_dir / ".env").write_text(
            "DINARY_DEPLOY_HOST=ubuntu@test-primary\n"
            "DINARY_REPLICA_HOST=ubuntu@test-replica\n"
            "DINARY_TUNNEL=tailscale\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            tasks.backups.backups_replica,
            "ssh_replica_capture_bytes",
            lambda script: b"SQLite format 3\x00" + b"\x00" * 96,
        )
        return monkeypatch

    def test_local_restore_writes_the_local_database(self, _patched):
        calls = []
        _patched.setattr(
            tasks.backups.backups_replica,
            "run_restore",
            lambda _c, staged, **kw: calls.append((staged.read_bytes()[:15], kw)),
        )
        tasks.restore_replica.body(MagicMock(), yes=True)
        payload, kwargs = calls[0]
        assert payload == b"SQLite format 3"
        assert kwargs["prod"] is False
        assert kwargs["target"] == Path("data/dinary.db")

    def test_prod_flag_is_forwarded(self, _patched):
        calls = []
        _patched.setattr(
            tasks.backups.backups_replica,
            "run_restore",
            lambda _c, _staged, **kw: calls.append(kw),
        )
        tasks.restore_replica.body(MagicMock(), prod=True)
        assert calls[0]["prod"] is True

    def test_timestamp_reaches_the_replica_script_and_the_label(self, _patched):
        scripts, kwargs = [], []
        _patched.setattr(
            tasks.backups.backups_replica,
            "ssh_replica_capture_bytes",
            lambda script: scripts.append(script) or (b"SQLite format 3\x00" + b"\x00" * 96),
        )
        _patched.setattr(
            tasks.backups.backups_replica,
            "run_restore",
            lambda _c, _staged, **kw: kwargs.append(kw),
        )
        tasks.restore_replica.body(MagicMock(), yes=True, timestamp="2026-08-12T07:30:00Z")
        assert '-timestamp "2026-08-12T07:30:00Z"' in scripts[0]
        assert "2026-08-12T07:30:00Z" in kwargs[0]["incoming_label"]

    def test_output_with_prod_is_refused(self, _patched):
        """``--output`` names a local file; silently ignoring it next to ``--prod``
        would let an operator think the restore went to that path."""
        _patched.setattr(
            tasks.backups.backups_replica,
            "run_restore",
            lambda *a, **kw: pytest.fail("must not restore anything"),
        )
        with pytest.raises(SystemExit) as exc:
            tasks.restore_replica.body(MagicMock(), prod=True, output="copy.db")
        assert exc.value.code == 1
