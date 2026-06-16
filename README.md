# 🏠 Home Network Monitor

> A self-hosted Docker stack that watches your internet connection 24/7 and visualises every drop, spike, and slowdown in Grafana.

![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-2.55-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-11.3-F46800?logo=grafana&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What it measures

Five containers, one purpose — answer **when, how often, and how badly is my connection misbehaving?**

| Signal | Method | Interval |
|---|---|---|
| Latency (min / avg / max / jitter) | ICMP ping, 10 packets per probe | 30 s |
| Packet loss | ICMP ping | 30 s |
| DNS resolution time | `getaddrinfo` | 30 s |
| HTTP reachability + response time | HTTP GET | 30 s |
| Per-hop path latency & loss | traceroute (raw ICMP) | 60 s |
| Download / upload speed | Ookla Speedtest CLI | 1 h |
| Bufferbloat grade (A – F) | Latency delta under full load | 1 h |
| LAN devices (IP, MAC, vendor, ports) | nmap + mDNS + SSDP + DHCP | 5 min |
| Outage events | Up/down transition log | real-time |

---

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │            Docker network             │
                    │                                       │
  .env ──► hnm-monitor ──/metrics──► hnm-prometheus ──► hnm-grafana
           (Python  :8000)              (:9090)              (:3000)
                                                               ▲
  host net ──► hnm-lanscan ──/metrics────────────────────────┘
               (Python  :8001)

              hnm-settings (:8080)  ←── edit .env, restart monitor
```

| Container | Role | Port |
|---|---|---|
| `hnm-monitor` | Probe engine — ping · DNS · HTTP · traceroute · speedtest | 8000 |
| `hnm-lanscan` | LAN scanner — nmap · mDNS · SSDP · DHCP | 8001 |
| `hnm-settings` | Web UI for editing config without SSH | 8080 |
| `hnm-prometheus` | Metrics store, 30-day retention | 9090 |
| `hnm-grafana` | Pre-provisioned dashboard, anonymous access | 3000 |

---

## Quick start

### Deploy to a Raspberry Pi (recommended)

```bash
git clone https://github.com/yrafique/home-network-monitor.git
cd home-network-monitor
cp .env.example .env        # edit targets, password, alerts
./deploy.sh                 # syncs files → builds → starts → verifies
```

| URL | What |
|---|---|
| `http://homebridge.local:3000` | Grafana dashboard |
| `http://homebridge.local:8080` | Settings UI |
| `http://homebridge.local:9090` | Prometheus |
| `http://homebridge.local:8000/metrics` | Raw metrics |

### Run locally

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build
# → http://localhost:3000
```

The override file disables the hourly speedtest on Mac so it doesn't saturate your link while the Pi continues its runs.

---

## Configuration

```bash
# Who to ping every 30 s — add your router IP to distinguish ISP vs local faults
PING_TARGETS=10.0.0.1,8.8.8.8,1.1.1.1,google.com

# Hourly speedtest (uses real bandwidth — disable on metered links)
LOADTEST_ENABLED=true
LOADTEST_INTERVAL_SECONDS=3600

# WhatsApp alerts on outage (CallMeBot is free)
ALERTS_ENABLED=false
CALLMEBOT_PHONE=9715XXXXXXXX
CALLMEBOT_APIKEY=123456

# Grafana
GRAFANA_PASSWORD=changeme
```

**Settings UI** (`http://...:8080`) — edit any option and restart the monitor from a browser, no SSH needed.

---

## The dashboard

### Status row
Live readings: UP/DOWN state, current latency, packet loss, range uptime %, latency min/avg/max. All stat panels respect the time picker — switch from 5 min to 30 days and every number updates.

### Latency & packet loss
Time series per target with mean/min/max in the legend table. The **Connection State Timeline** renders a green/red bar per target — red blocks mark exactly when you lost connectivity.

### Per-hop path
Every hop between you and the target with its own RTT and loss, auto-labelled by reverse-DNS and owner (LAN / Rogers / Google / …).

> Per-hop loss only matters if it **persists to the final hop** — intermediate ICMP drops are normal router behaviour, not a fault.

### Speed & bufferbloat

Hourly download/upload Mbps plus idle vs loaded latency. The **bufferbloat grade** captures why a connection *feels* sluggish even when speed looks fine — latency spikes under load ruin calls and games regardless of bandwidth.

| Grade | Latency increase under load |
|---|---|
| A | < 5 ms |
| B | 5 – 30 ms |
| C | 30 – 60 ms |
| D | 60 – 200 ms |
| F | > 200 ms |

### Advanced analytics

**Anomaly z-score** — latency compared to each target's own rolling 6-hour baseline, scaled by an IQR-based estimator (spike-resistant). `|z| > 3` is a strong anomaly. Computed entirely in Prometheus recording rules — interpretable, not a black box.

**Fault scope** — a timeline that classifies every problem as:
- **All providers down** → your router, modem, or ISP
- **One provider down** → that provider's issue, not yours

**Seasonal baseline** — the monitor builds a median latency profile per hour-of-week from ~14 days of history. The dashboard shows actual vs expected, so normal patterns (evening congestion, maintenance windows) aren't flagged — only deviations *from that norm*. Appears after a few days of data.

---

## LAN device scanner

`hnm-lanscan` discovers every device on your network every 5 minutes using four complementary techniques:

1. **nmap** — TCP/UDP port scan, OS fingerprint, service detection
2. **mDNS** — Apple devices, Sonos, printers, smart home by name
3. **SSDP / UPnP** — smart TVs, routers, media servers
4. **DHCP sniffer** — passively captures hostnames from DHCP requests (no traffic generated)

Results appear in the **LAN Devices** table in Grafana: IP, MAC, vendor (OUI lookup), mDNS name, open ports, last-seen.

---

## Outage logging

Every up → down → up transition is appended to `./data/outages.jsonl` (survives restarts):

```jsonl
{"event":"outage_start","target":"internet","start":1718123456.0,"start_iso":"2026-06-11T14:30:56+00:00"}
{"event":"outage_end","target":"internet","start":1718123456.0,"end":1718123634.0,"duration_seconds":178.0,...}
```

The synthetic `internet` target fires only when **all** WAN providers fail simultaneously — a genuine ISP outage, not a single-provider blip.

```bash
# List all completed outages
grep outage_end ./data/outages.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    print(e['start_iso'][:19], f\"{e['duration_seconds']:.0f}s\", e['target'])
"
```

**WhatsApp alerts** — set `ALERTS_ENABLED=true` with CallMeBot (free) or Twilio to receive a message on outage and another on recovery with the exact duration.

---

## Comparing two vantage points

Run the stack on both a laptop (Wi-Fi) and an always-on Pi (Ethernet), then diff them:

```bash
python3 compare.py 24h
python3 compare.py 6h http://laptop:9090 http://homebridge.local:9090
```

If the **router** (`10.0.0.1`) looks clean on the Pi but noisy on the Mac at the same moment → the fault is your Wi-Fi, not the ISP.

---

## Project layout

```
home-network-monitor/
├── monitor/
│   ├── monitor.py          # Probe loop — ping · DNS · HTTP
│   ├── analyzer.py         # Seasonal hour-of-week baseline
│   ├── loadtest.py         # Ookla speedtest + bufferbloat
│   ├── path.py             # Per-hop traceroute
│   ├── enrich.py           # rDNS + owner label enrichment
│   ├── outage_logger.py    # Outage events + WhatsApp alerts
│   ├── notifier.py         # CallMeBot / Twilio sender
│   └── Dockerfile
├── lanscan/
│   ├── lanscan.py          # nmap + mDNS + SSDP + DHCP
│   └── Dockerfile
├── settings/
│   ├── app.py              # Flask app — edit .env, restart containers
│   ├── templates/
│   └── Dockerfile
├── grafana/provisioning/   # Auto-provisioned datasource + dashboard JSON
├── prometheus/             # Scrape config + recording rules
├── data/                   # Runtime data — outages.jsonl, speedtest state
├── docker-compose.yml
├── docker-compose.override.yml   # Mac-local overrides (disables speedtest)
├── .env.example
└── deploy.sh               # One-command Pi deploy with health verification
```

---

## Common commands

```bash
# Deploy
./deploy.sh                          # sync + build + deploy to Pi
./deploy.sh --local                  # build + run on Mac
./deploy.sh --check                  # check Pi status, no deploy

# Logs
ssh pi@homebridge.local "docker logs hnm-monitor -f"
ssh pi@homebridge.local "docker logs hnm-lanscan  -f"

# Apply .env changes
ssh pi@homebridge.local "cd /home/pi/home-network-monitor && docker compose up -d monitor"

# Disk cleanup
ssh pi@homebridge.local "docker image prune -af && docker builder prune -af"

# Stop (keep data)
ssh pi@homebridge.local "cd /home/pi/home-network-monitor && docker compose down"

# Stop and wipe all stored metrics
ssh pi@homebridge.local "cd /home/pi/home-network-monitor && docker compose down -v"
```

---

## Requirements

- Docker + Docker Compose on the target host
- `rsync` and `ssh` on the machine running `deploy.sh`
- `NET_RAW` capability (granted automatically via the compose file) for ICMP and traceroute

---

## License

MIT
