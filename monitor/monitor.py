#!/usr/bin/env python3
"""
Internet health monitor.
Probes ICMP/DNS/HTTP targets and exposes Prometheus metrics on :8000/metrics.
"""
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from icmplib import ping as icmp_ping
from prometheus_client import Gauge, start_http_server

import analyzer
import enrich
import loadtest
import path
from outage_logger import OutageLog

# --------------------------------------------------------------------------- #
# Configuration (override via environment / .env)
# --------------------------------------------------------------------------- #
def _csv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


PING_TARGETS = _csv("PING_TARGETS", "8.8.8.8,1.1.1.1,google.com")
DNS_TARGETS = _csv("DNS_TARGETS", "google.com,cloudflare.com,github.com")
HTTP_TARGETS = _csv("HTTP_TARGETS", "https://www.google.com,https://1.1.1.1")

PROBE_INTERVAL = int(os.environ.get("PROBE_INTERVAL_SECONDS", "30"))
PING_COUNT = int(os.environ.get("PING_COUNT", "10"))
PING_PACKET_INTERVAL = float(os.environ.get("PING_PACKET_INTERVAL_SECONDS", "0.5"))
PING_PACKET_TIMEOUT = float(os.environ.get("PING_PACKET_TIMEOUT_SECONDS", "2"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "5"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "8000"))

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
_PING_LABELS = ["target", "owner"]
PING_RTT_MIN = Gauge("internet_ping_rtt_min_ms", "Minimum ICMP RTT in last probe", _PING_LABELS)
PING_RTT_AVG = Gauge("internet_ping_rtt_avg_ms", "Average ICMP RTT in last probe", _PING_LABELS)
PING_RTT_MAX = Gauge("internet_ping_rtt_max_ms", "Maximum ICMP RTT in last probe", _PING_LABELS)
PING_JITTER = Gauge("internet_ping_jitter_ms", "RTT mean deviation (jitter) in last probe", _PING_LABELS)
PING_LOSS = Gauge("internet_ping_packet_loss_ratio", "Packet loss ratio 0..1 in last probe", _PING_LABELS)
TARGET_UP = Gauge("internet_up", "1 if the target responded to ping, else 0", _PING_LABELS)

PROBE_OK = Gauge("internet_probe_success", "1 if last probe succeeded, 0 if failed", ["target", "owner"])

TARGET_INFO = Gauge(
    "internet_target_info",
    "Resolved hostname and owner for a ping target",
    ["target", "owner", "resolved_host"],
)

DNS_LOOKUP = Gauge("internet_dns_lookup_ms", "DNS resolution time in ms", ["host", "owner"])
DNS_OK = Gauge("internet_dns_ok", "1 if DNS resolved, else 0", ["host", "owner"])

HTTP_RESPONSE = Gauge("internet_http_response_ms", "HTTP total response time in ms", ["url", "owner"])
HTTP_OK = Gauge("internet_http_reachable", "1 if HTTP request succeeded, else 0", ["url", "owner"])

SCRAPE_DURATION = Gauge("internet_probe_cycle_seconds", "Wall-clock duration of the last probe cycle")

# Latest up/down reading per ping target, fed to the outage logger each cycle.
LATEST_UP: dict[str, int] = {}
OUTAGES = OutageLog()


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def probe_ping(target: str) -> None:
    """ICMP-ping the target with icmplib and update RTT/loss/jitter metrics."""
    rdns, owner = enrich.meta(target)
    lbl = (target, owner)
    TARGET_INFO.labels(target=target, owner=owner, resolved_host=rdns).set(1)
    try:
        host = icmp_ping(
            target,
            count=PING_COUNT,
            interval=PING_PACKET_INTERVAL,
            timeout=PING_PACKET_TIMEOUT,
            privileged=True,
        )
        PING_LOSS.labels(*lbl).set(host.packet_loss)
        if host.is_alive:
            PING_RTT_MIN.labels(*lbl).set(host.min_rtt)
            PING_RTT_AVG.labels(*lbl).set(host.avg_rtt)
            PING_RTT_MAX.labels(*lbl).set(host.max_rtt)
            PING_JITTER.labels(*lbl).set(host.jitter)
            TARGET_UP.labels(*lbl).set(1)
            PROBE_OK.labels(*lbl).set(1)
            LATEST_UP[target] = 1
        else:
            PROBE_OK.labels(*lbl).set(0)
            PING_RTT_MIN.labels(*lbl).set(float("nan"))
            PING_RTT_AVG.labels(*lbl).set(float("nan"))
            PING_RTT_MAX.labels(*lbl).set(float("nan"))
            PING_JITTER.labels(*lbl).set(float("nan"))
            TARGET_UP.labels(*lbl).set(0)
            LATEST_UP[target] = 0
    except Exception:  # noqa: BLE001 - any failure means the target is down
        PROBE_OK.labels(*lbl).set(0)
        PING_LOSS.labels(*lbl).set(1.0)
        PING_RTT_MIN.labels(*lbl).set(float("nan"))
        PING_RTT_AVG.labels(*lbl).set(float("nan"))
        PING_RTT_MAX.labels(*lbl).set(float("nan"))
        PING_JITTER.labels(*lbl).set(float("nan"))
        TARGET_UP.labels(*lbl).set(0)
        LATEST_UP[target] = 0


def probe_dns(host: str) -> None:
    """Time a DNS A-record lookup."""
    owner = enrich.owner(host)
    start = time.perf_counter()
    try:
        socket.getaddrinfo(host, None)
        DNS_LOOKUP.labels(host, owner).set((time.perf_counter() - start) * 1000.0)
        DNS_OK.labels(host, owner).set(1)
    except Exception:  # noqa: BLE001
        DNS_LOOKUP.labels(host, owner).set(float("nan"))
        DNS_OK.labels(host, owner).set(0)


def probe_http(url: str) -> None:
    """Time an HTTP(S) GET and record reachability."""
    owner = enrich.url_owner(url)
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        HTTP_RESPONSE.labels(url, owner).set(resp.elapsed.total_seconds() * 1000.0)
        HTTP_OK.labels(url, owner).set(1 if resp.ok else 0)
    except Exception:  # noqa: BLE001
        HTTP_RESPONSE.labels(url, owner).set(float("nan"))
        HTTP_OK.labels(url, owner).set(0)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run_cycle(pool: ThreadPoolExecutor) -> None:
    futures = []
    futures += [pool.submit(probe_ping, t) for t in PING_TARGETS]
    futures += [pool.submit(probe_dns, h) for h in DNS_TARGETS]
    futures += [pool.submit(probe_http, u) for u in HTTP_TARGETS]
    for f in futures:
        f.result()
    OUTAGES.record({t: LATEST_UP.get(t, 0) for t in PING_TARGETS})


def main() -> None:
    print(
        f"[monitor] starting exporter on :{EXPORTER_PORT} | "
        f"interval={PROBE_INTERVAL}s | ping_targets={PING_TARGETS}",
        flush=True,
    )
    start_http_server(EXPORTER_PORT)
    threading.Thread(target=analyzer.run, name="analyzer", daemon=True).start()
    threading.Thread(target=path.run, name="path", daemon=True).start()
    threading.Thread(target=loadtest.run, name="loadtest", daemon=True).start()
    with ThreadPoolExecutor(max_workers=16) as pool:
        while True:
            start = time.perf_counter()
            run_cycle(pool)
            elapsed = time.perf_counter() - start
            SCRAPE_DURATION.set(elapsed)
            time.sleep(max(0.0, PROBE_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
