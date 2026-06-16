#!/usr/bin/env python3
"""
Compare internet-monitor stats between two vantage points (e.g. Mac vs Pi).

Queries two Prometheus instances over a time window and prints a side-by-side
table per ping target: latency min/avg/max, jitter, packet loss, uptime %, the
number of samples, and outage count. The "Δ" column is Pi minus Mac, so a
positive latency/loss Δ means the Pi saw worse numbers than the Mac.

Usage:
    python3 compare.py [window] [mac_url] [pi_url]
    python3 compare.py 24h
    python3 compare.py 24h http://localhost:9090 http://homebridge.local:9090

Defaults: window=24h, Mac=http://localhost:9090, Pi=http://homebridge.local:9090
No third-party deps — uses urllib.
"""
import json
import sys
import urllib.parse
import urllib.request

WINDOW = sys.argv[1] if len(sys.argv) > 1 else "24h"
MAC_URL = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:9090"
PI_URL = sys.argv[3] if len(sys.argv) > 3 else "http://homebridge.local:9090"


def q(base_url: str, expr: str):
    """Run an instant PromQL query; return float value or None."""
    url = f"{base_url}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            result = json.load(resp)["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:  # noqa: BLE001
        return None


def targets(base_url: str) -> set[str]:
    """Discover ping targets present at a vantage point."""
    url = f"{base_url}/api/v1/query?" + urllib.parse.urlencode({"query": "internet_up"})
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            result = json.load(resp)["data"]["result"]
        return {s["metric"].get("target", "?") for s in result}
    except Exception:  # noqa: BLE001
        return set()


# Each metric: label -> (promql template, formatter, lower_is_better)
METRICS = [
    ("latency avg ms", "avg_over_time(internet_ping_rtt_avg_ms{{target=\"{t}\"}}[{w}])", True),
    ("latency min ms", "min_over_time(internet_ping_rtt_min_ms{{target=\"{t}\"}}[{w}])", True),
    ("latency max ms", "max_over_time(internet_ping_rtt_max_ms{{target=\"{t}\"}}[{w}])", True),
    ("jitter avg ms", "avg_over_time(internet_ping_jitter_ms{{target=\"{t}\"}}[{w}])", True),
    ("packet loss %", "avg_over_time(internet_ping_packet_loss_ratio{{target=\"{t}\"}}[{w}]) * 100", True),
    ("uptime %", "avg_over_time(internet_up{{target=\"{t}\"}}[{w}]) * 100", False),
    ("samples", "count_over_time(internet_up{{target=\"{t}\"}}[{w}])", False),
    ("outages", "increase(internet_outages_total{{target=\"{t}\"}}[{w}])", True),
]


def fmt(v):
    return "   n/a" if v is None else f"{v:7.2f}"


def main() -> None:
    mac_t, pi_t = targets(MAC_URL), targets(PI_URL)
    all_targets = sorted(mac_t | pi_t)

    print(f"\nInternet monitor comparison — window={WINDOW}")
    print(f"  Mac = {MAC_URL}   ({'reachable' if mac_t else 'NO DATA / unreachable'})")
    print(f"  Pi  = {PI_URL}   ({'reachable' if pi_t else 'NO DATA / unreachable'})")
    if not all_targets:
        print("\nNo data from either side. Are both stacks running?")
        return

    for t in all_targets:
        print(f"\n══ target: {t} ══")
        print(f"  {'metric':<16}{'Mac':>9}{'Pi':>9}{'Δ(Pi-Mac)':>12}   verdict")
        print("  " + "-" * 58)
        for label, tmpl, lower_better in METRICS:
            expr = tmpl.format(t=t, w=WINDOW)
            mac_v = q(MAC_URL, expr)
            pi_v = q(PI_URL, expr)
            if mac_v is None and pi_v is None:
                continue
            delta = (pi_v - mac_v) if (mac_v is not None and pi_v is not None) else None
            verdict = ""
            if delta is not None and label not in ("samples",):
                if abs(delta) < 1e-6:
                    verdict = "same"
                elif lower_better:
                    verdict = "Pi better" if delta < 0 else "Mac better"
                else:
                    verdict = "Pi better" if delta > 0 else "Mac better"
            d_str = "   n/a" if delta is None else f"{delta:+8.2f}"
            print(f"  {label:<16}{fmt(mac_v):>9}{fmt(pi_v):>9}{d_str:>12}   {verdict}")

    # --- Connection quality: speed + bufferbloat (latest values) ---
    quality = [
        ("download Mbps", "internet_speedtest_download_mbps", False),
        ("upload Mbps", "internet_speedtest_upload_mbps", False),
        ("idle latency ms", "internet_bufferbloat_idle_latency_ms", True),
        ("loaded down ms", "internet_bufferbloat_down_latency_ms", True),
        ("loaded up ms", "internet_bufferbloat_up_latency_ms", True),
        ("bufferbloat +ms", "internet_bufferbloat_increase_ms", True),
        ("grade (5=A..1=F)", "internet_bufferbloat_grade", False),
    ]
    print("\n══ connection quality (latest speedtest/bufferbloat) ══")
    print(f"  {'metric':<18}{'Mac':>9}{'Pi':>9}{'Δ(Pi-Mac)':>12}   verdict")
    print("  " + "-" * 60)
    for label, metric, lower_better in quality:
        mac_v, pi_v = q(MAC_URL, metric), q(PI_URL, metric)
        if mac_v is None and pi_v is None:
            continue
        delta = (pi_v - mac_v) if (mac_v is not None and pi_v is not None) else None
        verdict = ""
        if delta is not None and abs(delta) > 1e-6:
            verdict = ("Pi better" if delta < 0 else "Mac better") if lower_better else ("Pi better" if delta > 0 else "Mac better")
        d_str = "   n/a" if delta is None else f"{delta:+8.2f}"
        print(f"  {label:<18}{fmt(mac_v):>9}{fmt(pi_v):>9}{d_str:>12}   {verdict}")

    print(
        "\nReading it: positive Δ on latency/loss/jitter/outages = Pi saw WORSE than Mac.\n"
        "Speed/grade: higher = better. The Pi is on weak Wi-Fi, so expect it to look\n"
        "worse until it's wired — then it becomes the trustworthy always-on reference.\n"
    )


if __name__ == "__main__":
    main()
