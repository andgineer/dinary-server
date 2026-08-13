"""Tests for the pure-shell builders in :mod:`tasks.ssh_utils`.

The three remote-bootstrap scripts the deploy task layer composes:

* ``litestream_install_script`` — arch-aware ``.deb`` download + install.
* ``build_setup_swap_script`` — persistent swapfile provisioner.
* ``build_ssh_tailscale_only_script`` — rebind sshd to Tailscale + loopback.

Sibling :file:`test_tasks_ssh_utils.py` covers the smaller helpers
(``systemd_quote``, ``remote_snapshot_cmd``, ``ssh_capture_bytes``).
"""

import allure
import pytest

import tasks.devtools.constants
import tasks.devtools.env
import tasks.ssh_utils


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Replica setup")
class TestLitestreamInstallScript:
    """Pins the ``uname -m`` -> asset-suffix mapping (Litestream's release assets
    use ``x86_64``/``arm64``, not the dpkg ``amd64``/``arm64`` spellings) — a drift
    here would only surface at the next VM bootstrap, weeks after the change lands."""

    def test_default_version_matches_pinned_constant(self):
        script = tasks.ssh_utils.litestream_install_script()
        assert (
            f"litestream-{tasks.devtools.constants.LITESTREAM_VERSION}-linux-x86_64.deb" in script
        )
        assert f"litestream-{tasks.devtools.constants.LITESTREAM_VERSION}-linux-arm64.deb" in script

    def test_x86_64_and_amd64_both_map_to_x86_64_asset(self):
        """Kernel reports ``x86_64``, dpkg spelling uses ``amd64`` — both must
        route to the same asset."""
        script = tasks.ssh_utils.litestream_install_script()
        assert (
            f"x86_64|amd64) ASSET=litestream-{tasks.devtools.constants.LITESTREAM_VERSION}-linux-x86_64.deb"
            in script
        )

    def test_aarch64_and_arm64_both_map_to_arm64_asset(self):
        """Kernel reports ``aarch64``, Debian userland prefers ``arm64`` — both
        must pick the arm64 asset."""
        script = tasks.ssh_utils.litestream_install_script()
        assert (
            f"aarch64|arm64) ASSET=litestream-{tasks.devtools.constants.LITESTREAM_VERSION}-linux-arm64.deb"
            in script
        )

    def test_unsupported_arch_exits_with_actionable_error(self):
        """Must error loudly with the offending arch, not silently ``curl 404``."""
        script = tasks.ssh_utils.litestream_install_script()
        assert (
            f"Unsupported arch $ARCH for litestream {tasks.devtools.constants.LITESTREAM_VERSION}"
            in script
        )
        assert "*) echo" in script
        assert "exit 1" in script

    def test_download_url_uses_github_release_path_for_pinned_version(self):
        """A typo in the ``v`` prefix or path layout is invisible until bootstrap day."""
        script = tasks.ssh_utils.litestream_install_script()
        assert (
            "https://github.com/benbjohnson/litestream/releases/download/"
            f"v{tasks.devtools.constants.LITESTREAM_VERSION}/$ASSET" in script
        )

    def test_script_is_idempotent_when_pinned_version_already_installed(self):
        """Re-running must be cheap: no download when the installed version matches."""
        script = tasks.ssh_utils.litestream_install_script()
        assert (
            f'if [ "$(litestream version 2>/dev/null)" != '
            f'"{tasks.devtools.constants.LITESTREAM_VERSION}" ]; then' in script
        )

    def test_version_mismatch_triggers_reinstall_not_a_path_check(self):
        """A ``command -v`` guard would freeze every provisioned VM on its bootstrap
        version, so a constant bump would never reach an existing deployment."""
        script = tasks.ssh_utils.litestream_install_script()
        assert "command -v litestream" not in script

    def test_dpkg_keeps_the_generated_config_without_prompting(self):
        """The .deb ships ``/etc/litestream.yml`` as a conffile: without
        ``--force-confold`` dpkg blocks on an interactive prompt over SSH and would
        otherwise replace the config ``inv setup-replica`` generated."""
        script = tasks.ssh_utils.litestream_install_script()
        assert "sudo dpkg -i --force-confold /tmp/$ASSET" in script

    def test_version_parameter_allows_future_upgrade(self):
        """A version bump should be a one-line constant change, not string surgery."""
        script = tasks.ssh_utils.litestream_install_script(version="0.6.0")
        assert "litestream-0.6.0-linux-x86_64.deb" in script
        assert "litestream-0.6.0-linux-arm64.deb" in script
        assert "/releases/download/v0.6.0/$ASSET" in script
        # Sanity: the pinned-default version is NOT leaking into a
        # caller-overridden script.
        assert f"litestream-{tasks.devtools.constants.LITESTREAM_VERSION}" not in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestSetupSwapScript:
    """A regression here (wrong size, missing fstab entry, broken idempotency)
    would surface weeks later as an OOM-killed service during a heavy import."""

    def test_default_allocates_one_gigabyte(self):
        """1 GB matches the Always Free VM profile — enough headroom for import
        spikes without eating meaningful disk on a 45 GB root fs."""
        script = tasks.ssh_utils.build_setup_swap_script(size_gb=1)
        assert "fallocate -l 1G /swapfile" in script

    def test_size_parameter_interpolates_into_fallocate(self):
        """The size must land verbatim in the ``fallocate`` line."""
        script = tasks.ssh_utils.build_setup_swap_script(size_gb=4)
        assert "fallocate -l 4G /swapfile" in script
        assert "fallocate -l 1G" not in script

    def test_rejects_nonpositive_size(self):
        """``fallocate -l 0G`` silently succeeds with a zero-byte file that
        ``mkswap`` then rejects with a cryptic error — fail fast instead."""
        with pytest.raises(ValueError, match="size_gb must be a positive integer"):
            tasks.ssh_utils.build_setup_swap_script(size_gb=0)
        with pytest.raises(ValueError, match="size_gb must be a positive integer"):
            tasks.ssh_utils.build_setup_swap_script(size_gb=-1)

    def test_idempotent_on_reapply(self):
        """Without the swapon-check short-circuit, a rerun would fallocate a fresh
        file over the live one and corrupt the swap signature."""
        script = tasks.ssh_utils.build_setup_swap_script(size_gb=1)
        assert "swapon --show=NAME --noheadings" in script
        assert "grep -qx /swapfile" in script
        assert "/swapfile already active, skipping allocation" in script

    def test_fstab_line_is_deduplicated(self):
        """``grep -qxF || echo >>`` prevents duplicate fstab entries piling up
        across reruns until the system refuses to mount."""
        script = tasks.ssh_utils.build_setup_swap_script(size_gb=1)
        assert "/swapfile none swap sw 0 0" in script
        assert 'grep -qxF "$FSTAB_LINE" /etc/fstab || echo "$FSTAB_LINE" >> /etc/fstab' in script

    def test_elevation_wraps_entire_block_not_just_first_command(self):
        """A semicolon chain prefixed with ``sudo`` would only elevate the first
        command; ``sudo bash <<HEREDOC`` elevates the whole block."""
        script = tasks.ssh_utils.build_setup_swap_script(size_gb=1)
        assert script.startswith("sudo bash <<'DINARY_SWAP_EOF'\n")
        assert script.rstrip().endswith("DINARY_SWAP_EOF")

    def test_quoted_heredoc_prevents_local_variable_expansion(self):
        """An unquoted heredoc would let the local shell expand ``$FSTAB_LINE`` to
        empty before the script even reaches the remote."""
        script = tasks.ssh_utils.build_setup_swap_script(size_gb=1)
        assert "<<'DINARY_SWAP_EOF'" in script
        assert "$FSTAB_LINE" in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestSshTailscaleOnlyScript:
    """A regression here is a lockout risk — the operator can only reach the VM
    via Oracle Cloud's Serial Console."""

    def test_refuses_when_tailscale_is_not_installed(self):
        """Binding sshd to a non-existent tailscaled IP would silently
        kill inbound SSH entirely. Gate the flip on ``command -v
        tailscale`` before touching any config file.
        """
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert "command -v tailscale" in script
        assert "tailscale is not installed" in script

    def test_refuses_when_tailscaled_has_no_ipv4(self):
        """``tailscale`` binary being present is not enough — the
        daemon may still be logged out or starting. Require a
        non-empty ``tailscale ip -4`` output before the flip.
        """
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert 'TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"' in script
        assert 'if [ -z "$TS_IP" ]; then' in script
        assert "tailscaled is not up" in script

    def test_keeps_loopback_listen_address(self):
        """Operators reaching the box via the Serial Console need loopback ssh
        to trigger a reload after rolling back a bad config."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert "ListenAddress 127.0.0.1:22" in script

    def test_binds_to_live_tailscale_ip_not_a_hardcoded_value(self):
        """Guards against a refactor replacing ``${TS_IP}`` with a literal, which
        would stop self-healing after an IP rotation."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert "ListenAddress ${TS_IP}:22" in script

    def test_inner_heredoc_is_unquoted_so_tsip_expands(self):
        """Unquoted on purpose: bash must expand ``${TS_IP}`` when writing the
        file, or the literal string lands in sshd_config.d and ``sshd -t`` rejects it."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert 'cat >"$DROPIN" <<EOC\n' in script
        assert "<<'EOC'" not in script

    def test_sshd_t_validates_before_reload(self):
        """Reloading on an invalid config, combined with the public IP already
        closed, would trap the operator outside the box."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        t_idx = script.index("sshd -t")
        reload_idx = script.index("systemctl reload ssh")
        assert t_idx < reload_idx

    def test_rejected_config_is_rolled_back(self):
        """Without rollback, a broken config survives reboot and kills sshd on
        next start — the only recovery path would be the Serial Console."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert 'rm -f "$DROPIN"' in script
        assert "sshd -t rejected the new config" in script

    def test_drop_in_path_and_idempotent_overwrite(self):
        """Rewritten (``cat >``) not appended on every run, so an IP rotation is
        absorbed by a simple replay."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert "DROPIN=/etc/ssh/sshd_config.d/10-tailscale-only.conf" in script
        assert 'cat >"$DROPIN" <<EOC' in script
        assert 'cat >>"$DROPIN"' not in script

    def test_elevation_wraps_the_whole_block(self):
        """One elevation boundary keeps the steps atomic — no partial apply if the
        operator's sudo timestamp expires mid-script."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert script.startswith("sudo bash <<'DINARY_SSH_TS_EOF'\n")
        assert script.rstrip().endswith("DINARY_SSH_TS_EOF")

    def test_outer_heredoc_is_quoted_so_remote_vars_dont_expand_locally(self):
        """Unquoted, the local shell would expand ``$TS_IP``/``$DROPIN`` to empty
        before the payload even ships, silently skipping the pre-flight checks."""
        script = tasks.ssh_utils.build_ssh_tailscale_only_script()
        assert "<<'DINARY_SSH_TS_EOF'" in script
        assert "$TS_IP" in script
        assert "$DROPIN" in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestHardenSshdScript:
    """A regression here would silently re-expose the dormant root/opc cloud-init
    seed key or leave X11Forwarding on."""

    def test_disables_x11_forwarding_via_dropin(self):
        script = tasks.ssh_utils.build_harden_sshd_script()
        assert "X11Forwarding no" in script
        assert "/etc/ssh/sshd_config.d/no-x11.conf" in script

    def test_forces_permit_root_login_no(self):
        """The pattern must match both commented and uncommented forms cloud-init
        might have left."""
        script = tasks.ssh_utils.build_harden_sshd_script()
        assert "PermitRootLogin no" in script
        assert "s/^#\\?PermitRootLogin" in script

    def test_wipes_root_and_opc_authorized_keys(self):
        """Cloud-init seeds the same key under ``/root`` and ``/home/opc``; a
        compromised laptop key would bypass the sudo audit trail otherwise."""
        script = tasks.ssh_utils.build_harden_sshd_script()
        assert ": >/root/.ssh/authorized_keys" in script
        assert ": >/home/opc/.ssh/authorized_keys" in script

    def test_locks_opc_user_when_present(self):
        script = tasks.ssh_utils.build_harden_sshd_script()
        assert "usermod -L -s /usr/sbin/nologin opc" in script
        # Guarded so the script is safe on hosts that never had opc.
        assert "id opc" in script

    def test_validates_sshd_before_reload_and_rolls_back_on_failure(self):
        """Must remove the X11 drop-in on rejection, or the next reload picks up
        a broken config."""
        script = tasks.ssh_utils.build_harden_sshd_script()
        assert "sshd -t" in script
        assert 'rm -f "$DROPIN"' in script
        assert "systemctl reload ssh" in script

    def test_uses_quoted_heredoc_so_vars_dont_expand_locally(self):
        script = tasks.ssh_utils.build_harden_sshd_script()
        assert "<<'DINARY_SSH_HARDEN_EOF'" in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestInstallFail2banScript:
    """Losing any of these knobs disables the jail, unbans too fast, or — most
    critically — drops the ``ignoreip`` exclusion and bans operators on the tailnet."""

    def test_installs_fail2ban_via_apt_noninteractive(self):
        script = tasks.ssh_utils.build_install_fail2ban_script()
        assert "DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban" in script

    def test_writes_jail_local_with_sshd_enabled(self):
        script = tasks.ssh_utils.build_install_fail2ban_script()
        assert "/etc/fail2ban/jail.local" in script
        assert "[sshd]" in script
        assert "enabled = true" in script
        assert "backend = systemd" in script

    def test_ignoreip_whitelists_tailscale_cgnat(self):
        """Without this, a mistyped password over the tailnet bans the operator
        on the only tunnel into the box."""
        script = tasks.ssh_utils.build_install_fail2ban_script()
        assert "100.64.0.0/10" in script
        assert "ignoreip" in script

    def test_ban_escalation_policy_matches_old_vm(self):
        """Geometric escalation with a 30-day cap is what the
        previous VM1 ran and what ``docs/operations.md`` references.
        If any of these drift, the op note goes stale.
        """
        script = tasks.ssh_utils.build_install_fail2ban_script()
        assert "bantime = 1d" in script
        assert "bantime.increment = true" in script
        assert "bantime.factor = 2" in script
        assert "bantime.maxtime = 30d" in script
        assert "findtime = 10m" in script
        assert "maxretry = 3" in script

    def test_enables_and_starts_service(self):
        script = tasks.ssh_utils.build_install_fail2ban_script()
        assert "systemctl enable --now fail2ban" in script

    def test_uses_quoted_heredoc_so_vars_dont_expand_locally(self):
        script = tasks.ssh_utils.build_install_fail2ban_script()
        assert "<<'DINARY_F2B_EOF'" in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestDataDirPermissionsScript:
    """SQLite recreates ``-wal``/``-shm`` with the umask default on restart, so a
    one-shot chmod is insufficient — must be idempotent and cover the glob."""

    def test_tightens_data_directory_to_700(self):
        script = tasks.ssh_utils.build_data_dir_permissions_script()
        assert "chmod 700 ~/dinary/data" in script

    def test_tightens_all_dinary_db_files_to_600(self):
        script = tasks.ssh_utils.build_data_dir_permissions_script()
        assert "dinary.db*" in script
        assert "chmod 600" in script
        assert "find ~/dinary/data" in script

    def test_find_scoped_to_top_level_not_recursive(self):
        """``-maxdepth 1`` keeps us from stat-ing backups/ subtrees
        on every restart. The live DB is always at the top level.
        """
        script = tasks.ssh_utils.build_data_dir_permissions_script()
        assert "-maxdepth 1" in script


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestReplicaTrustScripts:
    """Each step is idempotent, but the known_hosts script is deliberately NOT
    auto-refreshing — a host key change must travel through
    ``inv replica-reset-trust``."""

    def test_ensure_key_script_generates_ed25519_when_missing(self):
        """Must be ``ed25519``, not RSA — Oracle's Free Tier VMs ship old OpenSSH
        builds where RSA host keys default to SHA-1 signatures newer clients refuse."""
        script = tasks.ssh_utils.build_ensure_vm1_replica_key_script()
        assert "ssh-keygen -t ed25519" in script
        assert "-N ''" in script, "keygen must be non-interactive (no passphrase)"

    def test_ensure_key_script_is_idempotent(self):
        """Re-running ``inv setup-replica`` must not overwrite an
        existing keypair — doing so would invalidate every previously
        installed ``authorized_keys`` entry on VM2 in one stroke.
        """
        script = tasks.ssh_utils.build_ensure_vm1_replica_key_script()
        assert "[ ! -f ~/.ssh/id_ed25519 ]" in script

    def test_ensure_key_script_prints_pubkey_on_stdout(self):
        """Stdout feeds straight into ``build_install_authorized_key_script`` — any
        extra diagnostics on stdout would install garbage into authorized_keys."""
        script = tasks.ssh_utils.build_ensure_vm1_replica_key_script()
        assert script.rstrip().endswith("cat ~/.ssh/id_ed25519.pub")

    def test_install_authorized_key_is_idempotent(self):
        """``grep -qxF`` full-line match treats whitespace drift as a different
        key, forcing an operator check rather than a silent shadow."""
        pubkey = "ssh-ed25519 AAAAFAKE dinary-vm1-litestream"
        script = tasks.ssh_utils.build_install_authorized_key_script(pubkey)
        assert "grep -qxF" in script

    def test_install_authorized_key_validates_payload(self):
        """Without validation, a transport truncation installs a dead line that
        fills the file with junk."""
        pubkey = "ssh-ed25519 AAAAFAKE dinary-vm1-litestream"
        script = tasks.ssh_utils.build_install_authorized_key_script(pubkey)
        assert "ssh-keygen -l -f -" in script

    def test_install_authorized_key_sets_strict_permissions(self):
        """A mode wider than 0600 on ``authorized_keys`` makes
        strict-mode sshd reject the file entirely — failing all
        subsequent logins on VM2. Pin 0600 and 0700 on ``.ssh/``.
        """
        pubkey = "ssh-ed25519 AAAAFAKE dinary-vm1-litestream"
        script = tasks.ssh_utils.build_install_authorized_key_script(pubkey)
        assert "chmod 700 ~/.ssh" in script
        assert "chmod 600 ~/.ssh/authorized_keys" in script

    def test_install_authorized_key_escapes_shell_metachars(self):
        """Without ``shlex.quote``, a crafted pubkey comment could execute
        arbitrary code on VM2 as ``ubuntu``."""
        hostile = "ssh-ed25519 AAAAFAKE $(touch /tmp/owned)"
        script = tasks.ssh_utils.build_install_authorized_key_script(hostile)
        assert "$(touch /tmp/owned)" not in script.replace(
            "'ssh-ed25519 AAAAFAKE $(touch /tmp/owned)'", ""
        ), "shlex.quote must fully escape shell-substitution payloads"

    def test_add_known_host_is_idempotent(self):
        """Must not append a duplicate line, and critically must not overwrite a
        disagreeing entry — that's the ``replica-reset-trust`` boundary."""
        script = tasks.ssh_utils.build_add_known_host_script("replica.example")
        assert "ssh-keygen -F replica.example" in script
        assert "ssh-keyscan" in script

    def test_add_known_host_uses_ed25519_only(self):
        """Pins the scan type so a drift to ``-t rsa,ecdsa,ed25519`` doesn't
        silently accept weaker algorithms."""
        script = tasks.ssh_utils.build_add_known_host_script("replica.example")
        assert "ssh-keyscan -T 10 -t ed25519" in script

    def test_reset_known_host_wipes_before_scan(self):
        """Without removing the old entry first, OpenSSH refuses the new key with
        "REMOTE HOST IDENTIFICATION HAS CHANGED" no matter how many scans we append."""
        script = tasks.ssh_utils.build_reset_known_host_script("replica.example")
        keygen_idx = script.index("ssh-keygen -R replica.example")
        keyscan_idx = script.index("ssh-keyscan")
        assert keygen_idx < keyscan_idx

    def test_reset_known_host_tolerates_missing_old_entry(self):
        """``ssh-keygen -R`` exits non-zero if there's nothing to
        remove — without ``|| true`` the whole script fails the
        first time after a known_hosts wipe.
        """
        script = tasks.ssh_utils.build_reset_known_host_script("replica.example")
        assert "ssh-keygen -R replica.example -f ~/.ssh/known_hosts >/dev/null 2>&1 || true" in (
            script
        )

    def test_builders_escape_hostname_metachars(self):
        """The hostname flows into multiple shell invocations; a
        metachar-laden host derived from ``DINARY_REPLICA_HOST`` must
        not execute code on VM1 when ``inv setup-replica`` runs.
        """
        hostile = "replica.example;rm -rf /"
        add = tasks.ssh_utils.build_add_known_host_script(hostile)
        reset = tasks.ssh_utils.build_reset_known_host_script(hostile)
        assert "rm -rf /" not in add.replace("'replica.example;rm -rf /'", "")
        assert "rm -rf /" not in reset.replace("'replica.example;rm -rf /'", "")


@allure.epic("Infrastructure")
@allure.feature("Deploy")
@allure.story("Server setup")
class TestDinaryServiceBindHost:
    """Binding to the Tailscale IP instead of loopback breaks ``tailscale serve``
    and forces clients onto plain HTTP, where ``crypto.randomUUID()`` is
    unavailable (requires a secure context) — pins the contract against a
    refactor reintroducing ``$(tailscale ip -4)``."""

    def test_tailscale_binds_to_loopback(self):
        assert tasks.devtools.env.bind_host("tailscale") == "127.0.0.1"

    def test_cloudflare_binds_to_loopback(self):
        assert tasks.devtools.env.bind_host("cloudflare") == "127.0.0.1"

    def test_none_binds_to_all_interfaces(self):
        assert tasks.devtools.env.bind_host("none") == "0.0.0.0"

    def test_tailscale_service_unit_uses_loopback_not_tailscale_ip(self):
        """If ExecStart contains ``tailscale ip``, the service starts fine but
        ``tailscale serve`` gets connection-refused and HTTPS breaks silently."""
        unit = tasks.devtools.constants.DINARY_SERVICE.format(
            host=tasks.devtools.env.bind_host("tailscale")
        )
        exec_start = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        assert "--host 127.0.0.1" in exec_start
        assert "tailscale ip" not in exec_start

    def test_tailscale_service_unit_waits_for_tailscaled_before_start(self):
        """Without the ExecStartPre guard, the HTTPS proxy accepts connections
        before tailscaled (and thus the backend) is ready."""
        unit = tasks.devtools.constants.DINARY_SERVICE.format(
            host=tasks.devtools.env.bind_host("tailscale")
        )
        assert "ExecStartPre=" in unit
        assert "tailscale ip -4" in unit

    def test_none_service_unit_binds_to_all_interfaces(self):
        unit = tasks.devtools.constants.DINARY_SERVICE.format(
            host=tasks.devtools.env.bind_host("none")
        )
        exec_start = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        assert "--host 0.0.0.0" in exec_start
