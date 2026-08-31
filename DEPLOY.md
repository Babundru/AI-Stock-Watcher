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

`requirements-server.txt` skips `customtkinter`/`matplotlib`/`yfinance` -
those exist only for the desktop GUI (`gui.py`) and are unnecessary weight
here. If `pip install` gets OOM-killed on `e2-micro`, add a small swapfile
first:

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Configure

Copy over (or recreate) `data/settings.json` with your ntfy topic and a
Cloud AI provider + API key (see the Settings section in `gui.py`, or just
hand-edit the JSON) - `USE_CLOUD_AI: true` plus `CLOUD_AI_PROVIDER`,
`CLOUD_AI_MODEL`, `CLOUD_AI_API_KEY`. Leaving both `USE_CLOUD_AI` and
`USE_LOCAL_LLM` false runs the offline keyword matcher instead, which needs
no API key and no model at all.

Also set a dashboard login - `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`.
Leaving `DASHBOARD_PASSWORD` empty disables the login prompt entirely, and
`server.py` prints a warning at startup if you forget it. Put both in
`data/settings.json`:

```json
{
  "DASHBOARD_USERNAME": "your-username",
  "DASHBOARD_PASSWORD": "a-strong-password"
}
```

(Merge these into the file alongside `NTFY_TOPIC` etc. rather than
replacing it - `data/settings.json` is a flat JSON object of whichever
settings you've customized.)

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

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/stocks-ai
ExecStart=/home/<your-user>/stocks-ai/venv/bin/python server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stocks-ai
sudo systemctl status stocks-ai      # confirm it's running
journalctl -u stocks-ai -f           # tail its output
```

Now it starts on boot, restarts on crash, and keeps scanning even with no
dashboard open anywhere.

## 7. Use it

From your phone or any other device on your Tailscale network, open
`http://<tailscale-ip-or-name>:8000` in a browser. Your browser will prompt
for the username/password from step 4 the first time (and remember it for
the session) - it's the same dashboard regardless of device: status,
alerts, logs, portfolio, sources, and keywords, with Start/Stop control.

Notifications still go out via ntfy exactly as before, independent of
whether the dashboard is open.

## Updating

```bash
cd stocks-ai
git pull
sudo systemctl restart stocks-ai
```
