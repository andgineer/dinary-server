"""Server-facing tasks: status, logs, restart, SSH sessions."""

from invoke import task

from tasks.devtools.constants import REMOTE_LITESTREAM_CONFIG_PATH
from tasks.devtools.env import host, replica_host, tunnel
from tasks.ssh_utils import (
    ssh_run,
    ssh_sudo,
)


@task(name="restart-server")
def restart_server(c):
    """Restart the dinary systemd service on the server."""
    ssh_sudo(c, "systemctl restart dinary")
    print("=== Restarted. Checking health... ===")
    ssh_run(c, "sleep 5 && curl -s http://localhost:8000/api/health")


@task
def logs(c, follow=False, lines=100, prod=False):
    """Show dinary service logs. --prod fetches over SSH. -f to follow. -l N lines."""
    if not prod:
        print("Local dev logs appear in the terminal when running `inv dev`.")
        return
    flag = "-f" if follow else f"-n {lines} --no-pager"
    c.run(f"ssh {host()} 'sudo journalctl -u dinary {flag}'")


@task
def status(c, prod=False):
    """Show service status and Litestream replicator state. --prod queries the server over SSH."""
    if not prod:
        c.run("curl -sf http://localhost:8000/api/health || echo 'Server not responding'")
        return
    tun = tunnel()
    ssh_sudo(c, "systemctl status dinary --no-pager")
    if tun == "tailscale":
        ssh_run(c, "tailscale serve status")
    elif tun == "cloudflare":
        ssh_sudo(c, "systemctl status cloudflared --no-pager")
    ssh_run(c, "systemctl status litestream --no-pager || true")
    ssh_run(c, "sudo journalctl -u litestream -n 30 --no-pager || true")
    ssh_run(c, f"litestream databases -config {REMOTE_LITESTREAM_CONFIG_PATH} || true")


@task
def ssh(c):
    """Open SSH session to the server."""
    c.run(f"ssh {host()}", pty=True)


@task(name="ssh-replica")
def ssh_replica(c):
    """Open SSH session to the replica (VM2)."""
    c.run(f"ssh {replica_host()}", pty=True)
