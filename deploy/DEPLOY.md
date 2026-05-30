# Deploying GammaLeak to Oracle Always Free

Single-instance deployment of the engine + browser dashboard on an Oracle
Cloud ARM Ampere "Always Free" instance, auto-started by systemd timers,
reachable from your own devices via Tailscale's private mesh network.
No public IP exposure, no paid domain required.

**Operational profile**: Engine starts 09:13 IST, stops 15:35 IST, Mon–Fri.
Zero daily manual touch as long as the Upstox token remains valid (in
practice: generate once, lasts until Upstox invalidates).

**Total recurring cost: ₹0.** Oracle Always Free + Tailscale free tier.

---

## Prerequisites

- Oracle Cloud account with **Always Free** tier enabled.
- Tailscale account (free for personal use, up to 100 devices / 3 users).
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
sudo cp /opt/gammaleak/deploy/gammaleak.service           /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-stop.service      /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-start.timer       /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-stop.timer        /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-calibrate.service /etc/systemd/system/
sudo cp /opt/gammaleak/deploy/gammaleak-calibrate.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now gammaleak-start.timer gammaleak-stop.timer \
                            gammaleak-calibrate.timer

# Verify all three timers are registered
systemctl list-timers gammaleak-*
```

Expected output: three timers with next-fire times in the IST 09:13 / 15:35 /
16:00 slots on the next weekday. The 16:00 one re-derives the regime gate from
the day's graded outcomes (rewrites `core/calibration.py`); tomorrow's 09:13
boot picks it up automatically via `core/config.py`'s merge — no restart needed.

Run it by hand any time to see the current table without writing the file:

```bash
cd /opt/gammaleak && .venv/bin/python calibrate.py --dry-run
```

---

## Step 6 — Tailscale (private mesh for dashboard access)

Tailscale puts the Oracle instance, your laptop, and your phone on
a private virtual network. You access the dashboard at a stable
hostname (e.g. `http://gammaleak:8080`) from any of your devices,
anywhere in the world. **No public exposure, no domain required.**

### On the Oracle instance (one-time)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=gammaleak
# Opens a one-time auth URL — paste it into a browser, sign in with
# Google / GitHub / email, approve the device. Done.
```

Verify:

```bash
tailscale status         # should show 'gammaleak' as one of your nodes
tailscale ip -4          # the 100.x.x.x address assigned to this node
```

### On your devices (each one-time, ~30s)

- **Phone**: install "Tailscale" from App Store / Play Store, sign in
  with the same account, toggle the VPN on.
- **Laptop**: install Tailscale from <https://tailscale.com/download>
  (Windows / macOS / Linux), sign in, toggle on.

That's it. From any of those devices, open one of:

- `http://gammaleak:8080`           (MagicDNS — works after a few seconds)
- `http://100.x.x.x:8080`           (the tailnet IP, always works)

…and you have the dashboard. From anywhere. No DNS to configure, no
ports exposed publicly, no domain to buy.

### Security note

Only devices signed in to YOUR Tailscale account can reach the
dashboard. Tailscale's free tier supports up to 100 devices and 3
users — more than enough for personal use. If you ever want to share
read-only access with another trader, invite them as a user and use
Tailscale ACLs to restrict what they can hit.

### Optional: a public URL too

If you ever need a *public* URL (to share with someone who isn't on
your tailnet), use Cloudflare's free `trycloudflare.com` random-URL
tunnel as a one-off:

```bash
sudo apt install -y cloudflared
cloudflared tunnel --url http://localhost:8080
# Prints a random https://*.trycloudflare.com URL valid until you kill it
```

No account, no domain, no commitment. URL dies when you Ctrl-C.

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
From a Tailscale-connected device, open `http://gammaleak:8080` —
full dashboard should load.

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
dashboard is always at `http://gammaleak:8080` from any of your
Tailscale-connected devices.

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
| `http://gammaleak:8080` doesn't resolve | MagicDNS not propagated yet | Wait 30s after first `tailscale up`, or use the `100.x.x.x` IP from `tailscale ip -4` |
| Tailscale shows node as offline | Oracle instance asleep or `tailscaled` not running | `sudo systemctl restart tailscaled` on Oracle |
| Dashboard times out via Tailscale | Engine not running | `systemctl list-timers gammaleak-*` to confirm next-fire time |
| journalctl shows BSE_FO subscribe errors | Upstox account doesn't have BSE F&O scope | Non-fatal warning — engine boots without SENSEX_FUT card |
| Disk full | Logs accumulated | Run the `find ... -mtime +90 -delete` from above |

---

## What this gives you

- **Zero daily touch** during normal weeks.
- **~30 second monthly touch** when the token dies (push a fresh `.env`).
- **Dashboard at a stable hostname** — `http://gammaleak:8080` from any
  of your Tailscale-connected devices (phone / laptop / anywhere).
- **No public port exposure** — Oracle's only inbound port is SSH 22;
  Tailscale uses outbound-only connections to its coordination server.
- **₹0 recurring cost** — Oracle Always Free + Tailscale free tier, no
  domain to purchase or renew.
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
