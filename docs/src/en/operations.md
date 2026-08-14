# Database, Migrations, Backups, and Replication

This page describes how `dinary` stores data, how schema migrations
run, the full backup story (local cold copies, hot Litestream
replication to VM 2, and daily off-site backups to Yandex.Disk), and
the restore procedures for each.

## Database file

The server stores everything in a single SQLite database:

- `data/dinary.db` — the live database (categories, groups, tags,
  stores, mappings, expenses, income, and sync-job metadata)
- `data/dinary.db-wal` and `data/dinary.db-shm` — SQLite WAL-mode
  sidecars that hold in-flight writes before they are checkpointed
  back into `dinary.db`

All three files belong together. Do not copy just `dinary.db`: the
WAL sidecar can legitimately contain committed transactions that
have not yet been checkpointed. Any backup flow that does not go
through SQLite's `.backup` API must stop the `dinary` service first.

## Migrations

Schema changes are managed with `yoyo` migrations living in
`src/dinary/migrations/`.

### When migrations run

Migrations run automatically on every server start — yoyo applies any
outstanding migration before the first request is served. No manual
step is needed: `inv deploy --ref=<version>` restarts the service, and
yoyo handles the rest.

For development, `inv dev` triggers the same path on startup.
Use `inv migrate` to apply migrations without restarting the server.

## Integrity check

SQLite ships two pragmas for post-migration / post-restore sanity
checks:

- `PRAGMA integrity_check` walks every btree page and reports
  structural damage (torn pages, index/table mismatches, orphan
  freelist entries).
- `PRAGMA foreign_key_check` lists every row that violates a
  declared FK.

Both are read-only and cheap. `dinary` wraps them in:

```bash
inv verify-db            # local data/dinary.db
inv verify-db --prod     # snapshot of prod DB, checked over SSH
```

`--prod` first takes a `sqlite3 .backup` snapshot into `/tmp` on
the server and checks that, so you never read a live file whose WAL
is mid-checkpoint.

## Backups

`dinary` uses three backup layers paired to their failure modes.

### Pre-deploy snapshot (automatic)

`inv deploy` always takes a `sqlite3 .backup` snapshot before touching
the server and writes it to `data/backups/pre-deploy-<timestamp>/dinary.db`
on your workstation. No manual step needed.

To pull a live snapshot manually for local debugging:

```bash
inv restore-primary          # download prod DB to data/dinary.db
```

### Hot replica: Litestream

`inv backup` is pull-based and fires on demand. For continuous
streaming replication — "my VM 1 got terminated; how much data did
I just lose" — `dinary` supports Litestream (v0.5.x) as a sidecar
that ships LTX segments to an SFTP target continuously.

#### Prerequisites

- A second VM you control (VM 2, the Litestream replica host) — the
  reference target is an Oracle Cloud Free Tier `VM.Standard.E2.1.
  Micro` with Ubuntu 22.04 Minimal, same shape as VM 1.
- VM 2 has `DINARY_REPLICA_HOST` set in `.deploy/.env` on the
  operator machine (e.g. `ubuntu@dinary-replica` via Tailscale
  MagicDNS).
- VM 1's `~/.ssh/id_ed25519.pub` is in VM 2's
  `~/.ssh/authorized_keys` — this is the trust that lets
  `litestream.service` on VM 1 push WAL segments over SFTP. Run
  `ssh-copy-id` manually once (cross-host trust is out of scope for
  automation from the operator machine).

#### Provisioning VM 2

Run once from the operator machine against VM 2:

```bash
inv setup-replica
```

This installs `unattended-upgrades`, allocates a 1 GB swap file,
creates `/var/lib/litestream/` with the right ownership so the SFTP
receiver can drop WAL segments into it, and locks public SSH. The task is idempotent — re-running it after a
`Persistent=true` reboot or a Tailscale IP rotation converges
cleanly.

`setup-replica` does NOT install the `dinary` app service, a public
tunnel, or any Python runtime — VM 2 is intentionally a minimal SFTP
sink. Everything the daily off-site backup needs (rclone, sqlite3,
zstd, the Litestream binary used only for local restore) is added
by `inv setup-replica` — see "Off-site backup: Yandex.Disk"
below.

#### One-time Litestream bootstrap on VM 1

1. Copy the example config locally and fill in the SFTP target:

   ```bash
   cp .deploy.example/litestream.yml .deploy/litestream.yml
   # edit .deploy/litestream.yml — set host, user, path, key-path
   ```

2. Install the Litestream sidecar on VM 1 (now part of `inv setup-replica`):

   ```bash
   inv setup-replica
   ```

   This installs the Litestream binary, uploads the config to
   `/etc/litestream.yml`, creates a `litestream.service` systemd
   unit, and starts it. The task is idempotent — re-running
   `inv setup-replica` upgrades the binary and reloads the
   config.

3. Confirm replication is healthy:

   ```bash
   inv status --prod
   ```

   A healthy sidecar shows an active systemd unit and the managed
   DB path listed by `litestream databases`. An empty output means
   the sidecar either never reached the SFTP host or is still
   producing its first snapshot (first one lands within seconds of
   the first DB write after the sidecar starts).

`inv setup-server` does not start Litestream automatically even when
`.deploy/litestream.yml` is present, because the sidecar needs an
already-reachable SFTP host with VM 1's public key in its
`authorized_keys` — a cross-host trust relationship we cannot set
up from the deploy workstation. Run `inv setup-replica`
manually once that prerequisite is in place.

#### What the sidecar does

Litestream v0.5 is a passive replicator: it opens the DB
read-only, tails LTX segments out of SQLite's WAL, compacts them
into level files, and ships them to the SFTP target. The app never
talks to it. If the sidecar crashes, the app keeps writing into the
WAL normally — you just stop accumulating replica state until the
sidecar restarts. There is no back-pressure; SQLite's checkpoint
loop is unaffected.

Default settings in the example config: a full snapshot every hour
and 7 days of LTX history (top-level `snapshot: { interval: 1h,
retention: 168h }`). That bounds "how far back can I rewind the DB"
to a week and bounds LTX replay on restore to a one-hour window.
These fields are global in v0.5 — they are NOT valid inside a
per-replica block and Litestream refuses to start if they are
placed there.

#### Restoring from a replica

On any host with Litestream installed and SSH access to the replica
target:

```bash
litestream restore -config /path/to/litestream.yml /path/to/output/dinary.db
```

Litestream reads the most recent snapshot in the replica, replays
LTX forward to the latest committed transaction, and writes a fresh
`dinary.db`. The restored DB is transactionally consistent — no
`PRAGMA integrity_check` is required, but running it does not hurt.

## Off-site backup: Yandex.Disk (daily, GFS retention)

The Litestream replica on VM 2 is hot (seconds-level RPO) but
co-located with VM 1 in the same cloud provider's region. For
"both VMs went away" scenarios there is a daily off-site backup
pushed from VM 2 to Yandex.Disk, orchestrated by
`inv setup-replica`.

### What it does

Every day at 03:17 UTC (+ 30 min jitter) a systemd oneshot on VM 2:

1. Materializes the local Litestream replica at
   `/var/lib/litestream/dinary` into a plain SQLite file via
   `litestream restore`.
2. Validates the restored file with `PRAGMA integrity_check`.
   A corruption failure aborts the run without uploading — we
   refuse to overwrite the Yandex history with a visibly broken
   snapshot.
3. Compresses with `zstd -19` (ratio is near-optimal on SQLite's
   repetitive page layout; CPU cost is negligible on the <1 MB
   input).
4. Uploads to
   `yandex:Backup/dinary/dinary-<UTC-ISO>.db.zst` via `rclone`.
5. Prunes Yandex.Disk per the GFS retention policy (see below).

The upload is a plain compressed SQLite file, not an opaque
repository format — any machine with `zstd` and `sqlite3` can open
a snapshot directly without the `dinary` tooling.

### GFS retention

- 7 most-recent **daily** snapshots.
- 4 most-recent **weekly** snapshots (newest per ISO week).
- 12 most-recent **monthly** snapshots (newest per calendar month).
- All **yearly** snapshots, kept indefinitely (closed years are
  immutable — any drift between two yearly snapshots of the same
  closed year signals corruption and is worth keeping forever).

Buckets overlap — a snapshot is pruned only if it belongs to no
keeper bucket. On a 10-year horizon this is roughly 29 files total
(~9 MB on disk).

### One-time bootstrap

Run once from the operator machine against VM 2 (this also covers the off-site backup bootstrap):

```bash
inv setup-replica
```

The task:

- Installs apt packages `rclone`, `sqlite3`, `zstd` and the pinned
  Litestream binary on VM 2.
- **Configures the `yandex:` rclone remote interactively on the
  first run.** If the remote is missing the task prints a pointer
  to <https://id.yandex.ru/security/app-passwords>, prompts for the
  Yandex login, and reads the app-password via `getpass` (no echo).

  > **Yandex WebDAV does NOT accept your regular Yandex account
  > password.** You must create a dedicated app-password under the
  > "Files" (Файлы / WebDAV) category on the page above — Mail,
  > Calendar, or generic tokens are rejected by the WebDAV
  > endpoint. The app-password is revocable from the same page
  > without affecting the main account password.

  The plaintext password travels only over the SSH channel to VM 2,
  is fed to `rclone obscure -` on stdin, and only the obscured form
  is written to `~ubuntu/.config/rclone/rclone.conf`. Plaintext
  never lands in argv (`ps` listing), shell history, or disk.

  After writing the config the task runs `rclone lsd yandex:` as a
  smoke test. Any failure (wrong password, wrong scope, network)
  aborts the task and rolls back the partial config so the next run
  re-prompts with a clean slate. On a green smoke test the prompt
  is skipped on subsequent runs.
- Writes `/usr/local/bin/dinary-backup` and the paired retention
  script; installs and enables `dinary-backup.timer`; triggers one
  immediate run so the first snapshot is visible on Yandex.Disk
  within a minute of bootstrap.

Why an app-password and not the full OAuth flow: VM 2 is headless,
and the interactive `rclone config` wizard expects a
laptop-authorize → copy-token dance across machines. An
app-password is equivalent for our access pattern (PUT/DELETE of
uploaded files), can be revoked from the Yandex account UI at any
time, and bootstrap stays end-to-end non-interactive beyond the
password prompt.

The task is idempotent: apt installs are no-op on re-apply,
rclone-remote bootstrap is a no-op once `yandex:` exists, scripts
and systemd units are overwritten, `enable --now` is harmless to
re-run.

### Watching the daily run

```bash
ssh ubuntu@dinary-replica sudo journalctl -u dinary-backup.service -n 50 --no-pager
ssh ubuntu@dinary-replica sudo systemctl list-timers dinary-backup.timer
```

### Freshness monitoring: `inv backup-cloud-status`

The daily timer failing silently is the worst-case mode — the
off-site snapshot stops refreshing while everything else looks fine.
`inv backup-cloud-status` is the off-VM2 probe:

```bash
inv backup-cloud-status                     # human one-liner; exit 0/1
inv backup-cloud-status --json-output       # machine-readable verdict
inv backup-cloud-status --max-age-hours 3   # tighten threshold during an incident
```

It SSHes into VM 2, runs `rclone lsjson` against the `yandex:`
remote, extracts the UTC timestamp encoded in the newest
`dinary-YYYY-MM-DDTHHMMZ.db.zst` filename, and compares it to
`--max-age-hours` (default **26 h** — 24 h + 1 h for the 30 min
timer jitter + 30 min headroom). Sample outputs:

```
OK: newest dinary-2026-04-22T0317Z.db.zst, age 7.1h, size 198.5 KB (threshold: 26h)
STALE: newest dinary-2026-04-20T0317Z.db.zst, age 49.3h, size 198.1 KB (threshold: 26h)
STALE: no snapshots on yandex:Backup/dinary/ (threshold: 26h)
```

Because it runs off-VM2, **both** a dead VM 2 (SSH fails) and a
silently-stopped timer (snapshot stale) surface as a non-zero exit
code, so one probe covers both failure modes.

Wire it into laptop cron via
`linux-conf/osx/dinary_backup_check.sh` (copied into `~/scripts/`
by `linux-conf/osx/copy_scripts.sh`). That wrapper `cd`s into
`~/projects/dinary`, runs `uv run inv backup-cloud-status`, and on
non-zero exit pipes the captured output through `send_fail_email`
(macOS-side msmtp → Yandex SMTP). Suggested crontab:

```
17 */6 * * * /Users/<you>/scripts/dinary_backup_check.sh
```

Four checks per day is cheap and catches a missed 03:17 UTC run
within hours rather than the next morning.

### Local Yandex.Disk access: `inv setup-yadisk`

`restore-yadisk` reads from `yandex:` on whichever machine it
runs. `inv setup-yadisk` configures that remote locally — on VM 1
during disaster recovery, or on the operator laptop for debug
bootstraps:

```bash
inv setup-yadisk
```

Uses the same WebDAV + app-password flow as `inv setup-replica` (no
browser OAuth needed). Idempotent: skips the prompt if `yandex:` is
already configured and healthy.

`restore-yadisk` calls this automatically when `yandex:` is
absent, so running it beforehand is optional.

## Point-in-time restore from Yandex.Disk

```bash
inv restore-yadisk --list-only                     # show inventory
inv restore-yadisk                                 # restore latest
inv restore-yadisk --snapshot 2026-03-15           # specific date
inv restore-yadisk --yes                           # skip confirm
inv restore-yadisk --prod                          # restore production
```

### Two intended use cases

- **Laptop debug bootstrap.** Materialize a recent prod snapshot
  into `./data/dinary.db` on your workstation to reproduce a bug
  against real data. Low risk: any overwrite of a local debug DB
  is recoverable from the auto-saved
  `data/dinary.db.before-restore-<ts>`.
- **Production disaster recovery.** Add `--prod` and the same task
  restores the server instead, driven entirely from the operator
  machine.

Every restore task writes the local database unless `--prod` is
given, so no wrong terminal or stray tab-completion can reach
production. Restores are never launched on the server itself: the
`--prod` path issues plain SSH commands, so a restore does not
depend on which revision happens to be deployed there.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--snapshot DATE` | `latest` | Filename date prefix, e.g. `2026-04-22` matches the full `dinary-2026-04-22T0317Z.db.zst` |
| `--list-only` | off | Read-only: enumerate available snapshots and exit |
| `--prod` | off | Replace the production database instead of the local one |
| `--yes` | off | Skip the "type yes to proceed" gate on a local restore. Never applies to `--prod` |

### Preconditions (one-time per machine)

- `rclone`, `sqlite3`, `zstd` installed. All three are already
  present on VM 1 via `inv setup-server`. On macOS: `brew install
  rclone sqlite zstd`.
- A `yandex:` rclone remote configured locally. If it is absent,
  **`restore-yadisk` prompts automatically** (same WebDAV +
  app-password flow as `inv setup-replica`). To pre-configure before
  the first restore run: `inv setup-yadisk`.

### Safety guarantees

- The snapshot is decompressed into a tmpdir and
  `PRAGMA integrity_check`'d **before** any existing database is
  touched. A corrupt snapshot aborts the run with the live DB
  untouched.
- Before the confirmation prompt both databases are summarized —
  expense count and newest expense on each side — and the prompt
  states how many expenses the restore drops. A stale snapshot
  shows up as a number instead of as silently missing rows later.
- The replaced database is preserved next to the original:
  `dinary.db.before-restore-<UTC-ISO>`, locally or on the server.
- `--prod` asks for the literal word `prod`, and `--yes` does not
  suppress it.

### Production restore runbook

Run from the operator machine; the source task depends on what
survived:

```bash
inv restore-replica --prod                       # newest: VM2 WAL archive
inv restore-replica --timestamp 2026-08-12T07:30:00Z --prod  # point in time
inv restore-yadisk --snapshot 2026-03-15 --prod  # daily off-site archive
```

Each of them, in one run: downloads and verifies the snapshot,
prints both sides plus what will be lost, asks for `prod`, stops
`dinary` and `litestream`, preserves the current database, swaps
the file, resyncs the replica, and brings both services back —
reporting what production ended up holding.

To inspect a snapshot before committing it, restore it locally
first (no `--prod`), look at `data/dinary.db`, then re-run with
`--prod`.

If the operator machine is unavailable, the same result without
this repository:

```bash
ssh ubuntu@dinary-replica \
  'litestream restore -config /tmp/ls.yml /tmp/dinary.db'  # from VM2's tree
scp ubuntu@dinary-replica:/tmp/dinary.db /tmp/dinary.db
ssh ubuntu@dinary 'sudo systemctl stop dinary litestream'
scp /tmp/dinary.db ubuntu@dinary:/home/ubuntu/dinary/data/dinary.db
ssh ubuntu@dinary 'rm -f /home/ubuntu/dinary/data/dinary.db-wal \
  /home/ubuntu/dinary/data/dinary.db-shm \
  && rm -rf /home/ubuntu/dinary/data/.dinary.db-litestream'
ssh ubuntu@dinary-replica 'rm -rf /var/lib/litestream/dinary'
ssh ubuntu@dinary 'sudo systemctl start litestream dinary'
```

## Restore from cold backup

1. Stop the running service: `ssh $HOST 'sudo systemctl stop dinary'`.
2. Replace `data/dinary.db` with the backed-up file.
3. Remove any stale WAL sidecars: `rm -f data/dinary.db-wal data/dinary.db-shm`.
4. Start the service: `inv restart-server`.
5. Optionally run `inv verify-db` to check integrity.

## Deploy + restore flow

To deploy a specific version with a DB restore (e.g. rollback after a bad deploy):

```bash
inv deploy --ref=v0.4.0 --no-start   # deploy code but skip service start
inv restore-yadisk --prod            # restore the server DB from Yandex.Disk
inv restart-server                   # start; yoyo applies forward migrations
```

## Coordinated import reset

To rebuild expenses and income from Google Sheets (e.g. after restoring a backup that predates recent sheet edits). The category catalog seeds itself automatically on service start:

```bash
inv import-budget-all --yes
inv import-income-all --yes
inv import-verify-bootstrap-all
inv import-verify-income-all
```

## VM2 host-key refresh

After VM2 is reprovisioned, refresh trust from the operator machine:

```bash
inv setup-reset-trust
```

This wipes the old VM2 entry from VM1's `known_hosts`, re-scans, and re-installs VM1's pubkey in VM2's `authorized_keys`. Do **not** use this to resolve an unexpected host-key mismatch without physically confirming VM2 was reprovisioned — an unexpected mismatch is a security signal.

## Tailscale sshd break-glass

> **Current production posture:** Tailscale-only SSH is **not** applied in production. It was tried and reverted. Reason: Oracle Cloud Serial Console authenticates by password — but our VMs are configured with key-only SSH and no `/etc/shadow` password. When both SSH access and the Tailscale Serve API endpoint depend on the same tailnet tunnel, a single `tailscaled` crash simultaneously kills operator access and the data plane with no independent break-glass path. Current posture: public-key SSH on `0.0.0.0:22` plus `fail2ban` (1-day initial ban, doubling to 30 days) to absorb brute-force noise. Key-only auth already defeats brute-force; fail2ban absorbs the log noise.

If `--tailscale` was passed to `inv setup-server` (rebinds sshd to Tailscale-only) and you are locked out:

1. Oracle Cloud console → VM Instance → **Console connection** → **Launch Cloud Shell**
2. `sudo rm /etc/ssh/sshd_config.d/10-tailscale-only.conf`
3. `sudo systemctl reload ssh`

## Monitoring

```bash
inv healthcheck            # local data/dinary.db
inv healthcheck --prod   # prod over SSH
```

Checks: systemd services active, replica page count matches primary, yesterday's exchange rate
cached, last expense logged to Google Sheets. Exits non-zero on first failure.

```bash
inv backup-cloud-status                    # newest Yandex.Disk snapshot age (exits non-zero if stale)
inv backup-cloud-status --max-age-hours 3  # tighten threshold
inv backup-cloud-status --json-output      # machine-readable
```

## Reporting and data access

```bash
inv report-expenses                   # expenses by (category, event, tags)
inv report-expenses --year 2025       # filter to a year
inv report-expenses --month 2025-03   # filter to a month
inv report-expenses --csv             # CSV to stdout
inv report-expenses --prod          # query prod DB over SSH

inv report-income --prod            # income by year

inv sql -q "SELECT * FROM app_metadata ORDER BY key"            # read-only by default
inv sql -q "DELETE FROM expenses WHERE id = 999" --write        # opt into mutations
inv sql -f my_query.sql --csv > out.csv
inv sql -q "SELECT COUNT(*) FROM expenses" --prod             # prod snapshot over SSH
```

## Practical guidance

- Three layers of redundancy, paired to their failure modes:
  the pre-deploy snapshot covers "oops I nuked the DB during a manual repair";
  Litestream to VM 2 covers "oops I lost VM 1"; Yandex.Disk
  (`inv setup-replica`) covers "oops I lost both VMs / lost the
  whole cloud provider".
- Treat `data/dinary.db` as the source of truth. Do not edit it
  while the service is running, and never hand-edit the
  `-wal`/`-shm` sidecars at all.
