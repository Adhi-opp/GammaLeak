# Deploying GammaLeak to Oracle Always Free

Single-instance deployment of the engine + browser dashboard on an Oracle
Cloud ARM Ampere "Always Free" instance, auto-started by systemd timers,
reachable from anywhere via Cloudflare Tunnel. No public IP exposure.

**Operational profile**: Engine starts 09:13 IST, stops 15:35 IST, Mon–Fri.
Zero daily manual touch as long as the Upstox token remains valid (in
practice: generate once, lasts until Upstox invalidates).

---

## Prerequisites

- Oracle Cloud account with **Always Free** tier enabled.
- Cloudflare account + a domain (free tier; ~$8/yr for the cheapest TLD).
- Working Upstox API credentials + a valid access token in `.env`.

---

## Step 1 — Provision the instance

In the Oracle console:

1. **Compute → Instances → Create instance**.
2. **Shape**: VM.Standard.A1.Flex (ARM Ampere). Allocate 2 OCPU / 12 GB
   RAM (well within Always Free limits; you have 4 OCPU / 24 GB total
   to play with).
3. **Image**: Canonical Ubuntu 22.04 minimal.
4. **Networking**: default VCN, public IPv4 enabled.
5. **SSH key**: paste your public key.
6. **Security list**: leave default (only port 22 inbound). The dashboard
   will NOT be exposed on a public port — Cloudflare Tunnel handles that
   via outbound-only connections.

Once it's `RUNNING`, `ssh ubuntu@<public-ip>`.

---

## Step 2 — System setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv git rsync
sudo timedatectl set-timezone Asia/Kolkata
```

Verify the timezone took effect:

```bash
date  # should show IST
```

---

## Step 3 — Clone the repo and create the venv

```bash
sudo mkdir -p /opt/gammaleak
sudo chown ubuntu:ubuntu /opt/gammaleak
git clone https://github.com/Adhi-opp/GammaLeak.git /opt/gammaleak
cd /opt/gammaleak
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## Step 4 — Push your `.env` from your local machine

```bash
# Run on your LOCAL machine, not on the server
scp .env ubuntu@<oracle-public-ip>:/opt/gammaleak/.env
ssh ubuntu@<oracle-public-ip> 'chmod 600 /opt/gammaleak/.env'
```

The `.env` should contain `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`,
`UPSTOX_REDIRECT_URI`, and `UPSTOX_ACCESS_TOKEN`.

---

## Step 5 — Install systemd units

```bash
sudo cp /opt/gammaleak/deploy/gammaleak.service       /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-stop.service  /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-start.timer   /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-stop.timer    /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now gammaleak-start.timer gammaleak-stop.timer

# Verify both timers are registered
systemctl list-timers gammaleak-*
```

Expected output: two timers with next-fire times in the IST 09:13 / 15:35
slots on the next weekday.

---

## Step 6 — Cloudflare Tunnel (dashboard access from anywhere)

One-time setup, then runs as a service forever.

```bash
# Install cloudflared (ARM64 build)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o /tmp/cf.deb
sudo dpkg -i /tmp/cf.deb

# Authenticate with your Cloudflare account (opens a browser link — paste it locally)
cloudflared tunnel login

# Create the tunnel
cloudflared tunnel create gammaleak

# Route a hostname to it (requires the domain to be on Cloudflare DNS)
cloudflared tunnel route dns gammaleak dash.yourdomain.com

# Drop the config (replace <TUNNEL_UUID> with the value from `tunnel create`)
sudo mkdir -p /etc/cloudflared
sudo cp /opt/gammaleak/deploy/cloudflared-config.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml   # fill in TUNNEL_UUID + hostname

# Install + start as a system service
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

The dashboard is now reachable at `https://dash.yourdomain.com` from
anywhere — phone, laptop, public Wi-Fi — **with no inbound ports open
on the Oracle instance.**

Optional but recommended: enable **Cloudflare Access** (Zero Trust) on
the hostname in the Cloudflare dashboard so only your email can load the
page. Free for up to 50 users.

---

## Step 7 — Smoke test before market open

```bash
# Manual start (don't wait for the timer on first run)
sudo systemctl start gammaleak.service

# Watch the logs
journalctl -u gammaleak.service -f
```

You should see the bootloader output:

```
Bootloader: Resolving active contracts from local Master...
[OK] NIFTY Options Expiry: ...
[OK] SENSEX Futures (front-month): BSE_FO|...
[OK] Prior close NIFTY: ...
[OK] Prior close SENSEX: ...
...
Writer task started, logging to logs/YYYY-MM-DD/
```

Then `curl http://localhost:8080/` should return the dashboard HTML.
Open `https://dash.yourdomain.com` in a browser — full dashboard.

If everything is good: `sudo systemctl stop gammaleak.service`. The
timers will take over from tomorrow's 09:13 IST automatically.

---

## Step 8 — Idle-reaping protection

Oracle has been preempting truly idle Always Free instances. To keep
the instance "active" during weekends and holidays:

```bash
sudo cp /opt/gammaleak/deploy/heartbeat.cron /etc/cron.d/gammaleak-heartbeat
sudo chmod 644 /etc/cron.d/gammaleak-heartbeat
```

This appends `uptime` to `/var/log/gammaleak-heartbeat.log` every hour
24/7. Continuous I/O + CPU activity = no preemption.

---

## Daily ops

**Nothing.** The engine starts 09:13 IST every weekday, stops 15:35,
dashboard is always at `dash.yourdomain.com`.

## Weekly / monthly ops

**Rotate old logs** to keep the 200 GB free block storage from
filling up over years:

```bash
# Add to crontab manually or as another cron.d file
0 4 * * 0 find /opt/gammaleak/logs -name '*.csv' -mtime +90 -delete
```

## When the Upstox token does eventually die

Generate a fresh one locally, then push it:

```bash
# Local machine
python oauth_token_exchange.py    # opens browser flow, updates local .env
scp .env ubuntu@<oracle-public-ip>:/opt/gammaleak/.env
```

The next 09:13 start picks up the new token. No restart of anything
else needed (token is read on each engine boot).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Timer fires but service immediately exits | `.env` missing or unreadable | `ls -l /opt/gammaleak/.env` — should be `-rw------- ubuntu` |
| WebSocket connects but no ticks | Token invalidated server-side | Re-run Step 4 push |
| Dashboard loads but no data | Engine not running (timer didn't fire) | `systemctl list-timers gammaleak-*` to confirm next-fire time |
| Cloudflare tunnel returns 502 | Engine not running | Same as above |
| journalctl shows BSE_FO subscribe errors | Upstox account doesn't have BSE F&O scope | Non-fatal warning — engine boots without SENSEX_FUT card |
| Disk full | Logs accumulated | Run the `find ... -mtime +90 -delete` from above |

---

## What this gives you

- **Zero daily touch** during normal weeks.
- **~30 second monthly touch** when the token dies (push a fresh `.env`).
- **Dashboard always at one stable URL** — `https://dash.yourdomain.com`.
- **No public port exposure** — tunnel is outbound-only; Oracle's only
  inbound port is SSH on 22.
- **Full version-controlled deploy recipe** — this file + the units in
  `deploy/`. Next time you spin up an instance (or someone asks how
  you deployed it) the exact steps are in-repo.

---

## What this does **not** give you

- **Live order execution.** GammaLeak is an attention filter, not an
  order router. There's no Upstox-order code path in the engine. This
  deployment surfaces the dashboard for discretionary trading; you
  still place orders manually in Kite/Upstox.
- **Multi-user access.** The dashboard assumes single-trader use. Use
  Cloudflare Access if you need to share with another collaborator.
- **HA / failover.** Single instance. If Oracle reaps it or the data
  centre goes down, dashboard is offline until you re-provision.
  Acceptable for personal use; not acceptable for anything time-critical.
