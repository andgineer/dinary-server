# Deploy on Oracle Cloud Free Tier

Oracle Cloud Always Free tier provides permanent VMs — enough to run dinary indefinitely at zero cost.

## Pricing

| Resource | Free Tier Allocation | Cost |
|----------|---------------------|------|
| AMD Micro VM | 2 instances, 1 OCPU + 1 GB RAM each | $0 forever |
| ARM Ampere A1 VM | Up to 4 OCPU, 24 GB RAM (shared pool — often unavailable) | $0 forever |
| Boot volume | 200 GB total | $0 |
| Outbound data | 10 TB/month | $0 |
| **Total** | | **$0/month** |

!!! tip "Which shape to choose"
    **AMD Micro** (`VM.Standard.E2.1.Micro`, 1 GB RAM) is recommended — it is almost always available because Oracle reserves a dedicated pool for free-tier accounts. 1 GB RAM is enough for FastAPI without Docker.

    **ARM Ampere A1** (`VM.Standard.A1.Flex`, up to 24 GB RAM) is more powerful but often unavailable ("Out of host capacity"). If you get one — great, otherwise use AMD Micro.

!!! warning
    Oracle may reclaim idle Always Free instances. Running a lightweight server like dinary keeps the instance active. If reclaimed, you can recreate the VM, but the runtime source of truth is `data/dinary.db` on disk, not Google Sheets. Back up `~/dinary/data/` before destructive work and do not treat the sheet-logging spreadsheet as a full restore source.

## Prerequisites

- A Google service account JSON key at `~/.config/gspread/service_account.json` — see [Google Sheets Setup](google-sheets-setup.md).
- An SSH key pair for connecting to the VM.

## 1. Create an account

1. Go to [cloud.oracle.com](https://cloud.oracle.com/) → **Sign Up**.
2. Select your home region (cannot be changed later).
3. Complete verification (credit card required but never charged for Always Free resources).

!!! tip "Region selection"
    Your home region is permanent. ARM instance availability varies by region. Community reports suggest **Ashburn**, **Phoenix**, **Frankfurt**, and **London** tend to have better ARM availability. However AMD Micro instances are available in all regions.

!!! note
    Account approval can take hours to days.

## 2. Set up networking (VCN)

Oracle VMs need a Virtual Cloud Network (VCN) with a public subnet and internet gateway. Create these **before** the VM:

1. Go to **Networking** → **Virtual Cloud Networks** → **Create VCN**.
      - **Name**: `dinary-vcn` (or any name)
      - **IPv4 CIDR Blocks**: `10.0.0.0/16`
      - Click **Create VCN**.

2. Inside the new VCN → **Subnets** → **Create Subnet**.
      - **Name**: `public-subnet`
      - **Subnet type**: Regional
      - **IPv4 CIDR Block**: `10.0.0.0/24`
      - **Subnet access**: **Public Subnet**
      - Click **Create Subnet**.

3. Inside the VCN → **Internet Gateways** → **Create Internet Gateway**.
      - **Name**: `internet-gw`
      - Click **Create Internet Gateway**.

4. Inside the VCN → **Route Tables** → click the default route table → **Add Route Rules**.
      - **Destination CIDR Block**: `0.0.0.0/0`
      - **Target Type**: Internet Gateway
      - **Target**: select `internet-gw`
      - Click **Add Route Rules**.

## 3. Create a VM

1. Go to **Compute** → **Instances** → **Create Instance**.
2. Configure:
      - **Image**: `Canonical Ubuntu 22.04 Minimal` (for AMD Micro — without `aarch64` in the name)
      - **Shape**: `VM.Standard.E2.1.Micro` — 1 OCPU, 1 GB RAM
      - **Capacity**: `On-demand capacity`
      - **Availability / Live migration**: `Let Oracle Cloud Infrastructure choose the best migration option`
      - **Networking**: select `dinary-vcn` → select `public-subnet` → check **Automatically assign public IPv4 address**
      - **SSH keys**: upload your public key
      - **Cloud-init script**: leave empty
3. Click **Create**.

!!! tip "ARM alternative"
    If ARM capacity is available, you can choose `Canonical Ubuntu 22.04 Minimal aarch64` + shape `VM.Standard.A1.Flex` (1 OCPU, 6 GB RAM). More RAM allows running Docker if desired. The rest of the setup is the same.

## 4. Configure .deploy/.env

All per-instance configuration (everything that is not committed to the repo) lives under the `.deploy/` directory at the repo root. After the VM is created, copy the public IP from the Oracle dashboard and create `.deploy/.env` on the laptop:

```bash
mkdir -p .deploy
cp .deploy.example/.env .deploy/.env
```

Edit `.deploy/.env`:

```
DINARY_DEPLOY_HOST=ubuntu@<PUBLIC_IP>
# DINARY_TUNNEL=tailscale  # tailscale (default) | cloudflare | none
# DINARY_SHEET_LOGGING_SPREADSHEET=https://docs.google.com/spreadsheets/d/YOUR_ID/edit
```

`inv setup-server` syncs the local `.deploy/.env` to the VM under
`/home/ubuntu/dinary/.deploy/`. The category catalog seeds itself
automatically on first boot.

Verify SSH access:

```bash
ssh ubuntu@<PUBLIC_IP>
```

## 5. Setup

From your laptop, in the dinary repo:

```bash
inv setup-server
```

This single command performs everything on the VM via SSH:

- Installs system packages (python3, git)
- Installs uv (Python package manager)
- Clones the repo and installs dependencies
- Syncs your local `.deploy/.env` to the VM
- Uploads `~/.config/gspread/service_account.json` to the VM
- Creates and starts a `dinary` systemd service
- Sets up the tunnel (Tailscale by default, or Cloudflare — depending on `DINARY_TUNNEL`)

### Tailscale (default)

During setup, `tailscale up` prints a URL — open it in your browser to log in (create a free account if needed).

Enable HTTPS in the [admin console](https://login.tailscale.com/admin/dns):

1. Enable **MagicDNS** (if not already enabled).
2. Enable **HTTPS** for your tailnet.

The app will be reachable at `https://<machine-name>.<tailnet>.ts.net` — tailnet only, Tailscale must be running on the client device.

!!! warning "First launch: wait up to 10 minutes"
    On first launch, Tailscale provisions a TLS certificate and propagates DNS. The URL may return `ERR_SSL_PROTOCOL_ERROR` for several minutes. Wait and retry.

### Cloudflare

Set `DINARY_TUNNEL=cloudflare` in `.deploy/.env` before running `inv setup-server`. During setup, `cloudflared tunnel login` will prompt you to authenticate in the browser. Requires a domain managed by Cloudflare DNS — see [Cloudflare Tunnel & Access](cloudflare-setup.md).

### No tunnel

Set `DINARY_TUNNEL=none` to skip tunnel setup. You'll need to open firewall ports manually:

**VCN Security List**: add ingress rule — Source `0.0.0.0/0`, Protocol TCP, Port `8000`.

**OS firewall**:

```bash
ssh ubuntu@<PUBLIC_IP> 'sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT && sudo netfilter-persistent save'
```

## Maintenance

| Command | What it does |
|---------|-------------|
| `inv deploy --ref=main` | Checkout ref, sync deps, apply migrations, restart service |
| `inv status --prod` | Show dinary and tunnel service status |
| `inv logs --prod` | Tail dinary server logs |
| `inv setup-server` | Full re-setup (safe to re-run) |
