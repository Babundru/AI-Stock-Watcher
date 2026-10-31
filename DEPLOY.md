# Running the backend on a server, dashboard reachable from anywhere

This runs `server.py` (no GUI - see below) continuously on a remote machine,
and reaches its web dashboard from any device over a private
[Tailscale](https://tailscale.com) network instead of the public internet.
On top of that, the dashboard itself sits behind a username/password
(HTTP Basic Auth) as a second layer.

Do not open its port to the public internet - only Tailscale peers should
ever be able to reach it. Basic Auth alone is not enough to make that safe:
without Tailscale's encryption, the password travels in a trivially
decodable form on every request.

## 1. Provision the VM (Google Cloud, Always Free e2-micro)

Google Cloud's "Always Free" tier includes one `e2-micro` instance
indefinitely (not a 12-month trial like AWS) - it just has to be a specific
machine type, in one of three regions:

- Machine type: **e2-micro** (2 shared vCPU, 1GB RAM) - a different type
  drops out of the free tier and starts billing.
- Region: **us-west1**, **us-central1**, or **us-east1** only.
- Boot disk: a **Standard persistent disk**, up to 30GB (an SSD disk is
  billed). Debian or Ubuntu minimal image.
- Skip "Allow HTTP/HTTPS traffic" when creating it - the dashboard is only
  ever reached over Tailscale, never through an open firewall rule.

Via the Console: Compute Engine -> VM instances -> Create Instance, set the
three fields above. Via `gcloud`:

```bash
gcloud compute instances create stocks-ai \
  --machine-type=e2-micro \
  --zone=us-central1-a \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-type=pd-standard --boot-disk-size=30GB
```

1GB RAM is tight for `pip install` and for running anything besides the app
itself - see step 3 for a trimmed dependency list, and don't run local
Ollama here (use a Cloud AI provider or the offline keyword matcher
instead - both are lightweight; see `config.py` / Settings).

## 2. Install the app

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone <your-repo-url> stocks-ai
cd stocks-ai
python3 -m venv venv
source venv/bin/activate
```

## 3. Install dependencies (trimmed for a 1GB VM)

```bash
pip install -r requirements-server.txt
```

`requirements-server.txt` skips `customtkinter`/`matplotlib` - those exist
only for the desktop GUI (`gui.py`) and are unnecessary weight here. It does
include `yfinance`/`pandas`, which the server side now needs too (sell-signal
watches and the portfolio value/profit graph).

### Add zram swap first - not a swapfile

1GB with no swap at all is the single most dangerous part of this setup. When
memory runs out the kernel does *not* necessarily OOM-kill anything: it can
instead settle into permanent reclaim, where the box stays `RUNNING` while
journald logs `Under memory pressure, flushing caches` every minute, sshd
can't fork, `tailscaled` stops answering, and `systemctl` times out talking
to PID 1. The VM looks frozen and only a hard reset recovers it.

Use **zram** (compressed swap in RAM), not a swapfile. A 30GB
Standard persistent disk has a baseline of roughly 45 write / 22 read IOPS,
so swapping to `/swapfile` makes that livelock *worse* - the box ends up
waiting on a disk that can't keep up. zram costs no disk I/O at all:

```bash
sudo apt install -y zram-tools
sudo tee /etc/default/zramswap >/dev/null <<'EOF'
ALGO=zstd
PERCENT=50
PRIORITY=100
EOF
sudo systemctl restart zramswap
swapon --show          # expect a /dev/zram0 device of ~500M
```

With zstd compression a 500MB zram device typically holds well over a
gigabyte of real pages, so it buys back a few hundred MB of usable memory
for the cost of a little CPU.

Also silence the kernel's martian-source logging. On GCE this fires every
few seconds forever, and it is pure noise that costs journald writes on a
disk with no IOPS to spare - it can be a quarter of your entire serial log:

```bash
echo 'net.ipv4.conf.all.log_martians=0'     | sudo tee /etc/sysctl.d/99-quiet.conf
echo 'net.ipv4.conf.default.log_martians=0' | sudo tee -a /etc/sysctl.d/99-quiet.conf
sudo sysctl --system
```

And cap the journal. The default limit is 10% of the disk, ~3GB here, and the
hourly reads of it cost a lot on this disk:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/size.conf >/dev/null <<'EOF'
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=50M
EOF
sudo systemctl restart systemd-journald
```

## 4. Configure

Only the dashboard login has to be set by hand, before the service is
reachable by anyone else. Everything else - the ntfy topic, the AI engine
and its API key, the paper-trading cost - can be set afterwards from the
dashboard's **Settings** tab on any device, and applies to the running
service without a restart.

Set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` in `data/settings.json`.
Leaving `DASHBOARD_PASSWORD` empty disables the login prompt entirely, and
`server.py` prints a warning at startup if you forget it:

```json
{
  "DASHBOARD_USERNAME": "your-username",
  "DASHBOARD_PASSWORD": "a-strong-password"
}
```

(`data/settings.json` is a flat JSON object of whichever settings you've
customized; the Settings tab merges into it rather than replacing it. If
you copied a `settings.json` over from the desktop app, it already holds
your topic and engine choice.)

## 5. Install Tailscale and join your network

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed login link once, from any device already on your
Tailscale account. Note the VM's Tailscale IP (`tailscale ip -4`) or its
MagicDNS name (`tailscale status`) - that's the address you'll use from
your phone/laptop, e.g. `http://100.x.x.x:8000` or `http://your-vm-name:8000`.

**Do not open port 8000 in the VM's firewall / VPC firewall rules.**
Tailscale traffic arrives over its own encrypted interface (`tailscale0`),
so as long as no GCP firewall rule allows 8000 from `0.0.0.0/0`, only your
Tailscale devices can reach the dashboard even though the app itself binds
`0.0.0.0`.

## 6. Run it as a service (systemd)

`/etc/systemd/system/stocks-ai.service`:

```ini
[Unit]
Description=Stocks AI backend + dashboard
After=network-online.target tailscaled.service
# Don't restart-loop into a disk that has ~45 write IOPS.
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/stocks-ai
ExecStart=/home/<your-user>/stocks-ai/venv/bin/python server.py
Restart=on-failure
RestartSec=30
# Log lines reach journald as they are written. Block-buffered, they came
# out in bursts stamped with the time of the flush, not of the event.
Environment=PYTHONUNBUFFERED=1
# Planned restart every two days, a safety net against slow growth. Reaching
# the limit counts as a failure, so Restart=on-failure brings it back; server.py
# saves its state on the SIGTERM first.
RuntimeMaxSec=2d
# A frozen process is still `active` to systemd. server.py pings this watchdog
# for as long as its scan loop makes progress (LOOP_STALL_LIMIT, 30 minutes);
# without a ping for 5 minutes systemd restarts the service. SIGTERM rather
# than the default SIGABRT: the state gets a chance to be saved, and no core
# dump lands on this disk.
WatchdogSec=5min
WatchdogSignal=SIGTERM

# THE IMPORTANT PART on a 1GB VM. Without a cap, this process growing past
# what the box has does not kill *this* service - it takes down sshd,
# tailscaled and journald with it, and you lose all access to the machine.
# With the cap the kernel kills only this unit and systemd restarts it,
# while everything you need to get in stays alive.
#
# No MemoryHigh, on purpose: it throttles without ever killing. In October 2026
# it held the app at its limit in constant reclaim, frozen for up to ~29 hours,
# never reaching MemoryMax (handoff/incidents.md).
MemoryAccounting=yes
MemoryMax=500M
# MemoryMax caps RAM only, not swap: in October 2026 the process went on into
# zram (374M RAM + 406M swap) until the whole VM ran out and thrashed for ~19
# hours before the kernel killed it.
MemorySwapMax=100M
OOMPolicy=stop

# Let sshd and tailscaled win every contest for CPU and disk. Losing a scan
# cycle costs nothing; losing your way into the box costs a reset.
Nice=5
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
```

Confirm the limits took effect - a typo here silently gives you no protection
at all:

```bash
systemctl show stocks-ai -p MemoryHigh -p MemoryMax -p MemorySwapMax -p OOMPolicy -p RuntimeMaxUSec -p WatchdogUSec -p WatchdogTimestamp
systemctl status stocks-ai | grep -i memory      # shows live usage
journalctl -u stocks-ai | grep "Memory in use"   # the app's own reading, once per scan
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stocks-ai
sudo systemctl status stocks-ai      # confirm it's running
journalctl -u stocks-ai -f           # tail its output
```

Now it starts on boot, restarts on crash, and keeps scanning even with no
dashboard open anywhere.

### Contain the Google guest agent too

The stock `google-guest-agent-manager.service` ships with `Restart=always`,
a 100ms restart delay and `KillMode=process`. If the metadata server becomes
unreachable, it crash-loops, and every restart leaves its `core_plugin` /
`guest_telemetry` children orphaned in the cgroup. Thousands of restarts
later those orphans fill the RAM. A drop-in kills the whole cgroup on
restart, slows the loop down and caps its memory:

```bash
sudo mkdir -p /etc/systemd/system/google-guest-agent-manager.service.d
sudo tee /etc/systemd/system/google-guest-agent-manager.service.d/override.conf >/dev/null <<'EOF'
[Unit]
StartLimitIntervalSec=0

[Service]
KillMode=control-group
RestartSec=30s
MemoryAccounting=yes
MemoryHigh=150M
MemoryMax=200M
EOF
sudo systemctl daemon-reload
systemctl show google-guest-agent-manager -p KillMode -p RestartUSec -p MemoryMax
```

## 7. Use it

From your phone or any other device on your Tailscale network, open
`http://<tailscale-ip-or-name>:8000` in a browser. Your browser will prompt
for the username/password from step 4 the first time (and remember it for
the session) - it's the same dashboard regardless of device, and it covers
everything the desktop app does: Start/Stop, alerts with the open
positions, logs (with the AI traffic switch), the portfolio with live
prices and the paper-trading record, sources, keywords, and a **Settings**
tab for the ntfy topic, the AI engine and API key, the paper cost, the
login, plus a "reload files from disk" button for anything edited by hand
over SSH. Every change applies to the running service immediately.

Notifications still go out via ntfy exactly as before, independent of
whether the dashboard is open.

## Optional: mirror the paper trades on an IBKR paper account

`broker.py` can place every paper trade as a real order on an Interactive
Brokers **paper** (demo) account and record the fill IBKR gave next to the
one the app simulated (`data/broker_orders.json`, `slippage_pct` per order).
It changes no decision - the paper ledger still runs everything.

IBKR has no API keys for personal accounts. The app talks to **IB Gateway**,
which holds the login:

1. In IBKR Client Portal, open a paper trading account (Settings -> Paper
   Trading Account) and note its username and its account id (`DU...`).
2. Install IB Gateway (stable) and log in with the **paper** username. In
   Configure -> Settings -> API -> Settings: enable "ActiveX and Socket
   Clients", turn off "Read-Only API", keep port 4002, and allow
   connections from 127.0.0.1 only.
3. `pip install ib_async`, then turn it on in the dashboard (Settings ->
   IBKR paper account: switch on, port, paper account id), or in
   `data/settings.json`:

   ```json
   "IBKR_ENABLED": true,
   "IBKR_PORT": 4002,
   "IBKR_ACCOUNT": "DU1234567"
   ```

4. Stop and start the monitoring. The log shows `🏦 IBKR connected - paper
   account DU...`, and the Settings card shows the connection state.

The app refuses any account that isn't a paper one (`DU...`), so a Gateway
logged in to the live account by mistake is never sent an order.

**Not on the e2-micro.** IB Gateway is a Java program that wants ~600-800MB
on its own; next to this service on a 1GB box it brings back the memory
incidents above. Run it on a machine with room (your desktop, or a bigger
VM) and point `IBKR_HOST` at it over Tailscale - then also add that address
to the Gateway's trusted IPs. The Gateway logs itself out once a day; tools
such as IBC restart and log it back in unattended.

## Updating

```bash
cd stocks-ai
git pull
sudo systemctl restart stocks-ai
```
