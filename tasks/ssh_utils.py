"""SSH transport, systemd helpers, and script builders for task modules."""

import base64
import json
import re
import shlex
import subprocess
from pathlib import Path

from tasks.devtools.constants import (
    _REMOTE_DB_PATH,
    _UV,
    LITESTREAM_VERSION,
    LOCAL_ENV_PATH,
    REMOTE_DEPLOY_DIR,
    REMOTE_ENV_PATH,
)
from tasks.devtools.env import host, replica_host

# ---------------------------------------------------------------------------
# Private module-level constants
# ---------------------------------------------------------------------------

# Characters that are safe to emit bare (unquoted) in a systemd EnvironmentFile
# value. Everything else gets double-quoted with internal escaping.
_ENV_SAFE_RE = re.compile(r"^[A-Za-z0-9@/.:_\-,+=]+$")

# ---------------------------------------------------------------------------
# Core SSH helpers
# ---------------------------------------------------------------------------


def ssh_run(c, cmd):
    """Base64-encodes ``cmd`` so quotes/dollar signs/newlines need no caller escaping."""
    b64 = base64.b64encode(cmd.encode()).decode()
    c.run(f"ssh {host()} 'echo {b64} | base64 -d | bash'")


def ssh_replica(c, cmd):
    """Like :func:`ssh_run` but targets the replica host (VM2)."""
    b64 = base64.b64encode(cmd.encode()).decode()
    c.run(f"ssh {replica_host()} 'echo {b64} | base64 -d | bash'")


def ssh_sudo(c, cmd):
    """Run ``cmd`` on the main host with ``sudo``."""
    ssh_run(c, f"sudo {cmd}")


def ssh_capture_bytes(cmd):
    """Uses ``subprocess.run`` directly (not invoke) so bytes are captured before
    any decode — critical for UTF-8 payloads spanning chunk boundaries."""
    b64 = base64.b64encode(cmd.encode()).decode()
    result = subprocess.run(
        ["ssh", host(), f"echo {b64} | base64 -d | bash"],
        capture_output=True,
        check=True,
    )
    return result.stdout


def ssh_capture(c, cmd):  # noqa: ARG001
    """Run ``cmd`` on the main host and return stdout as a string."""
    return ssh_capture_bytes(cmd).decode("utf-8")


def ssh_json(c, cmd):
    """Run ``cmd`` on the main host and parse its stdout as JSON."""
    return json.loads(ssh_capture(c, cmd))


def ssh_replica_capture_bytes(cmd):
    """Like :func:`ssh_capture_bytes` but targets the replica host (VM2)."""
    b64 = base64.b64encode(cmd.encode()).decode()
    result = subprocess.run(
        ["ssh", replica_host(), f"echo {b64} | base64 -d | bash"],
        capture_output=True,
        check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Remote file writers
# ---------------------------------------------------------------------------


def write_remote_file(c, path, content):
    """Write ``content`` to ``path`` on the main host via ``sudo tee``."""
    b64 = base64.b64encode(content.encode()).decode()
    c.run(f"ssh {host()} 'echo {b64} | base64 -d | sudo tee {path} > /dev/null'")


def write_remote_replica_file(c, path, content):
    """Write ``content`` to ``path`` on the replica host via ``sudo tee``."""
    b64 = base64.b64encode(content.encode()).decode()
    c.run(
        f"ssh {replica_host()} 'echo {b64} | base64 -d | sudo tee {path} > /dev/null'",
    )


# ---------------------------------------------------------------------------
# systemd helpers
# ---------------------------------------------------------------------------


def systemd_quote(value):
    """Bare alphanumeric/URL-safe values pass through unquoted; everything else is
    double-quoted with ``"``/``$``/``\\`` escaped so systemd doesn't expand them."""
    if value is None or value == "":
        return ""
    if _ENV_SAFE_RE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def render_service(c, name, content):
    """Upload a systemd unit file to ``/etc/systemd/system/<name>.service``."""
    path = f"/etc/systemd/system/{name}.service"
    write_remote_file(c, path, content)
    ssh_run(c, "sudo systemctl daemon-reload")


def create_service(c, name, content):
    """Upload, enable, and start a systemd service."""
    render_service(c, name, content)
    ssh_sudo(c, f"systemctl enable --now {name}")


# ---------------------------------------------------------------------------
# Env / config sync helpers
# ---------------------------------------------------------------------------


def sync_remote_env(c):
    """``write_remote_file`` leaves the file ``root:root 0644`` via ``sudo tee``;
    re-owns to ``ubuntu`` and tightens to ``0600`` since it holds deploy secrets."""
    content = Path(LOCAL_ENV_PATH).read_text(encoding="utf-8")
    ssh_run(c, f"mkdir -p {REMOTE_DEPLOY_DIR}")
    write_remote_file(c, REMOTE_ENV_PATH, content)
    ssh_sudo(c, f"chown ubuntu:ubuntu {REMOTE_ENV_PATH} && chmod 600 {REMOTE_ENV_PATH}")


def sync_remote_file(c, local: str, remote: str) -> None:
    """Upload a single file to the server if it exists locally."""
    local_path = Path(local)
    if not local_path.exists():
        print(f"=== Skipped {local} (not found locally) ===")
        return
    print(f"=== Syncing {local} → {remote} ===")
    content = local_path.read_text(encoding="utf-8")
    ssh_run(c, f"mkdir -p {REMOTE_DEPLOY_DIR}")
    write_remote_file(c, remote, content)
    ssh_sudo(c, f"chown ubuntu:ubuntu {remote} && chmod 600 {remote}")


# ---------------------------------------------------------------------------
# Tunnel setup
# ---------------------------------------------------------------------------


def setup_tailscale(c):
    """Configure Tailscale serve for the dinary app on the main host."""
    ssh_run(
        c,
        "curl -fsSL https://tailscale.com/install.sh | sudo sh"
        " && sudo tailscale up --hostname=dinary --ssh=false",
    )
    # Allow the current user to manage serve without sudo, then enable the proxy.
    ssh_sudo(c, "tailscale set --operator=$USER")
    ssh_run(c, "tailscale serve --bg 8000")


def setup_cloudflare(c):
    """Enable and start the Cloudflare tunnel service on the main host."""
    ssh_sudo(c, "systemctl enable --now cloudflared")


# ---------------------------------------------------------------------------
# Script builders
# ---------------------------------------------------------------------------


def litestream_install_script(version=None):
    """Installs or upgrades to the pinned version; short-circuits when it already matches.

    A ``command -v`` guard instead would pin every already-provisioned VM to whatever
    version it was bootstrapped with, so a constant bump would never reach production.
    ``--force-confold`` keeps the generated ``/etc/litestream.yml``: the .deb ships its own
    copy as a conffile, and dpkg would otherwise stop on an interactive prompt.
    """
    ver = version if version is not None else LITESTREAM_VERSION
    return (
        f'if [ "$(litestream version 2>/dev/null)" != "{ver}" ]; then\n'
        f"  ARCH=$(uname -m)\n"
        f"  case $ARCH in\n"
        f"    x86_64|amd64) ASSET=litestream-{ver}-linux-x86_64.deb ;;\n"
        f"    aarch64|arm64) ASSET=litestream-{ver}-linux-arm64.deb ;;\n"
        f'    *) echo "Unsupported arch $ARCH for litestream {ver}" >&2; exit 1 ;;\n'
        f"  esac\n"
        f"  URL=https://github.com/benbjohnson/litestream/releases/download/v{ver}/$ASSET\n"
        f"  curl -fsSL -o /tmp/$ASSET $URL\n"
        f"  sudo dpkg -i --force-confold /tmp/$ASSET\n"
        f"  rm /tmp/$ASSET\n"
        f"fi\n"
    )


def build_setup_swap_script(*, size_gb):
    """Emit the shell script that provisions a persistent swapfile.

    Idempotent: short-circuits when ``/swapfile`` is already active.
    """
    if size_gb <= 0:
        msg = "size_gb must be a positive integer"
        raise ValueError(msg)
    return (
        "sudo bash <<'DINARY_SWAP_EOF'\n"
        "set -euo pipefail\n"
        f"if swapon --show=NAME --noheadings | grep -qx /swapfile; then\n"
        "  echo '/swapfile already active, skipping allocation'\n"
        "else\n"
        f"  fallocate -l {size_gb}G /swapfile\n"
        "  chmod 600 /swapfile\n"
        "  mkswap /swapfile\n"
        "  swapon /swapfile\n"
        "fi\n"
        'FSTAB_LINE="/swapfile none swap sw 0 0"\n'
        'grep -qxF "$FSTAB_LINE" /etc/fstab || echo "$FSTAB_LINE" >> /etc/fstab\n'
        "DINARY_SWAP_EOF"
    )


def build_harden_sshd_script():
    """Disables X11Forwarding, forces PermitRootLogin no (Oracle cloud-init leaves
    it at ``prohibit-password``, which still accepts a key), wipes any cloud-init
    seeded authorized_keys, locks the dormant ``opc`` user, and validates with
    ``sshd -t`` before reloading (rolling back the X11 drop-in on failure)."""
    return (
        "sudo bash <<'DINARY_SSH_HARDEN_EOF'\n"
        "set -euo pipefail\n"
        "DROPIN=/etc/ssh/sshd_config.d/no-x11.conf\n"
        'echo "X11Forwarding no" >"$DROPIN"\n'
        "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config\n"
        "if ! sshd -t 2>&1; then\n"
        '  rm -f "$DROPIN"\n'
        "  echo 'sshd -t rejected the hardening config; X11 drop-in removed' >&2\n"
        "  exit 1\n"
        "fi\n"
        "systemctl reload ssh\n"
        ": >/root/.ssh/authorized_keys 2>/dev/null || true\n"
        "if id opc >/dev/null 2>&1; then\n"
        "  : >/home/opc/.ssh/authorized_keys 2>/dev/null || true\n"
        "  usermod -L -s /usr/sbin/nologin opc 2>/dev/null || true\n"
        "fi\n"
        "DINARY_SSH_HARDEN_EOF"
    )


def build_install_fail2ban_script():
    """Ban policy: 3 failures/10min, 1-day initial ban, geometric increase capped
    at 30d. ``ignoreip`` includes the Tailscale CGNAT range so admins on a
    Tailscale IP never get banned."""
    return (
        "sudo bash <<'DINARY_F2B_EOF'\n"
        "set -euo pipefail\n"
        "DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban\n"
        "cat >/etc/fail2ban/jail.local <<'EOC'\n"
        "[DEFAULT]\n"
        "ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10\n"
        "bantime = 1d\n"
        "bantime.increment = true\n"
        "bantime.factor = 2\n"
        "bantime.maxtime = 30d\n"
        "findtime = 10m\n"
        "maxretry = 3\n"
        "\n"
        "[sshd]\n"
        "enabled = true\n"
        "backend = systemd\n"
        "EOC\n"
        "systemctl enable --now fail2ban\n"
        "DINARY_F2B_EOF"
    )


def build_data_dir_permissions_script():
    """Default umask on Oracle images leaves ``dinary.db*`` group/world-readable;
    tightens to 700/600 so no other account can read the financial data."""
    return (
        "chmod 700 ~/dinary/data && "
        "find ~/dinary/data -maxdepth 1 -name 'dinary.db*' -exec chmod 600 {} +"
    )


def build_ensure_vm1_replica_key_script():
    """Idempotent: generates the keypair only if missing. Last stdout line is
    always the pubkey, ready to feed into :func:`build_install_authorized_key_script`."""
    return (
        "set -euo pipefail\n"
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        "if [ ! -f ~/.ssh/id_ed25519 ]; then\n"
        "  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -q -C dinary-vm1-litestream\n"
        "fi\n"
        "cat ~/.ssh/id_ed25519.pub\n"
    )


def build_install_authorized_key_script(pubkey):
    """``grep -qxF`` full-line match prevents duplicate appends but treats any
    byte-level drift as a different key, forcing investigation. ``ssh-keygen -l -f -``
    validates the payload so shell-mangled input fails here, not silently."""
    quoted = shlex.quote(pubkey)
    return (
        "set -euo pipefail\n"
        f"PUBKEY={quoted}\n"
        'if ! printf "%s\\n" "$PUBKEY" | ssh-keygen -l -f - >/dev/null 2>&1; then\n'
        '  echo "refusing to install malformed public key" >&2; exit 1\n'
        "fi\n"
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\n"
        'if ! grep -qxF "$PUBKEY" ~/.ssh/authorized_keys; then\n'
        '  printf "%s\\n" "$PUBKEY" >> ~/.ssh/authorized_keys\n'
        "fi\n"
    )


def build_add_known_host_script(hostname):
    """No-op if an entry already exists — a re-provisioned VM2 with a different
    host key fails the SFTP handshake rather than being silently accepted; that's
    the trust-refresh boundary ``inv replica-reset-trust`` handles."""
    quoted = shlex.quote(hostname)
    return (
        "set -euo pipefail\n"
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        "touch ~/.ssh/known_hosts && chmod 644 ~/.ssh/known_hosts\n"
        f"if ! ssh-keygen -F {quoted} -f ~/.ssh/known_hosts >/dev/null 2>&1; then\n"
        f"  ssh-keyscan -T 10 -t ed25519 {quoted} 2>/dev/null >> ~/.ssh/known_hosts\n"
        "fi\n"
    )


def build_reset_known_host_script(hostname):
    """Removes any existing entry and re-scans — used after an intentional VM2
    re-provision, the operator's explicit statement the new host key is legitimate."""
    quoted = shlex.quote(hostname)
    return (
        "set -euo pipefail\n"
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        "touch ~/.ssh/known_hosts && chmod 644 ~/.ssh/known_hosts\n"
        f"ssh-keygen -R {quoted} -f ~/.ssh/known_hosts >/dev/null 2>&1 || true\n"
        f"ssh-keyscan -T 10 -t ed25519 {quoted} 2>/dev/null >> ~/.ssh/known_hosts\n"
    )


def build_ssh_tailscale_only_script():
    """Guards against Tailscale not being installed or logged out; validates with
    ``sshd -t`` before reloading, rolling back the drop-in on failure."""
    return (
        "sudo bash <<'DINARY_SSH_TS_EOF'\n"
        "set -euo pipefail\n"
        "if ! command -v tailscale >/dev/null 2>&1; then\n"
        "  echo 'tailscale is not installed' >&2; exit 1\n"
        "fi\n"
        'TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"\n'
        'if [ -z "$TS_IP" ]; then\n'
        "  echo 'tailscaled is not up or not logged in' >&2; exit 1\n"
        "fi\n"
        "DROPIN=/etc/ssh/sshd_config.d/10-tailscale-only.conf\n"
        'cat >"$DROPIN" <<EOC\n'
        "ListenAddress ${TS_IP}:22\n"
        "ListenAddress 127.0.0.1:22\n"
        "EOC\n"
        "if ! sshd -t 2>&1; then\n"
        '  rm -f "$DROPIN"\n'
        "  echo 'sshd -t rejected the new config; drop-in removed' >&2; exit 1\n"
        "fi\n"
        "systemctl reload ssh\n"
        "DINARY_SSH_TS_EOF"
    )


# ---------------------------------------------------------------------------
# SQLite backup prologue
# ---------------------------------------------------------------------------


def sqlite_backup_prologue(snap_prefix):
    """Sets ``SNAP`` to ``/tmp/<snap_prefix>-<PID>.db`` with a cleanup trap;
    callers append further commands operating on ``$SNAP``."""
    return (
        f"SNAP=/tmp/{snap_prefix}-$$.db; "
        f'trap "rm -f \\"$SNAP\\"" EXIT; '
        f'sqlite3 "{_REMOTE_DB_PATH}" ".backup \\"$SNAP\\""; '
    )


# ---------------------------------------------------------------------------
# Remote snapshot command (for report / sql --remote)
# ---------------------------------------------------------------------------


def remote_snapshot_cmd(module_path, flags):
    """Wraps ``module_path`` in a ``sqlite3 .backup`` prologue so it reads a
    transactionally-consistent ``/tmp`` snapshot, not the live WAL-backed DB."""
    snap_name = "dinary-report-snapshot"
    prologue = sqlite_backup_prologue(snap_name)
    flag_str = (" " + shlex.join(flags)) if flags else ""
    data_env = 'DINARY_DATA_PATH="$SNAP"'
    run_cmd = f"cd ~/dinary && {data_env} {_UV} run python -m {module_path}{flag_str}"
    return f"set -e; {prologue}{run_cmd}"
