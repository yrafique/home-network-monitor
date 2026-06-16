"""Background thread that builds hour-of-week latency baselines from Prometheus history."""
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from prometheus_client import Gauge

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
WINDOW_DAYS = int(os.environ.get("SEASONAL_WINDOW_DAYS", "14"))
REFRESH_SECONDS = int(os.environ.get("SEASONAL_REFRESH_SECONDS", "300"))
MIN_SAMPLES = int(os.environ.get("SEASONAL_MIN_SAMPLES", "12"))
STEP_SECONDS = int(os.environ.get("SEASONAL_STEP_SECONDS", "300"))

SEASONAL_EXPECTED = Gauge(
    "internet_latency_seasonal_expected_ms",
    "Median latency for the current hour-of-week from historical baseline",
    ["target"],
)
SEASONAL_SAMPLES = Gauge(
    "internet_latency_seasonal_samples",
    "Historical samples backing the current hour-of-week expectation",
    ["target"],
)


def _bucket(ts: float) -> tuple[int, int]:
    """Return (weekday, hour) UTC bucket for a unix timestamp."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.weekday(), dt.hour


def _fetch_history(now: float) -> dict[str, dict[tuple[int, int], list[float]]]:
    """Query latency range data and group samples by target -> bucket -> values."""
    resp = requests.get(
        f"{PROM_URL}/api/v1/query_range",
        params={
            "query": "internet_ping_rtt_avg_ms",
            "start": now - WINDOW_DAYS * 86400,
            "end": now,
            "step": STEP_SECONDS,
        },
        timeout=30,
    )
    resp.raise_for_status()
    series = resp.json()["data"]["result"]

    grouped: dict[str, dict[tuple[int, int], list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for s in series:
        target = s["metric"].get("target", "unknown")
        for ts, val in s["values"]:
            try:
                grouped[target][_bucket(float(ts))].append(float(val))
            except (TypeError, ValueError):
                continue
    return grouped


def _update_once() -> None:
    now = time.time()
    grouped = _fetch_history(now)
    current_bucket = _bucket(now)

    for target, buckets in grouped.items():
        samples = buckets.get(current_bucket, [])
        if len(samples) >= MIN_SAMPLES:
            SEASONAL_EXPECTED.labels(target).set(statistics.median(samples))
            SEASONAL_SAMPLES.labels(target).set(len(samples))
        else:
            # Not enough history yet for this bucket: don't publish a guess.
            SEASONAL_SAMPLES.labels(target).set(len(samples))


def run() -> None:
    """Analyzer loop. Never raises out — a bad cycle just retries next time."""
    print(
        f"[analyzer] seasonal baseline | window={WINDOW_DAYS}d "
        f"refresh={REFRESH_SECONDS}s min_samples={MIN_SAMPLES}",
        flush=True,
    )
    while True:
        try:
            _update_once()
            delay = REFRESH_SECONDS
        except Exception as exc:  # noqa: BLE001 - keep the thread alive
            delay = min(REFRESH_SECONDS, 30)
            print(f"[analyzer] cycle failed, retry in {delay}s: {exc}", flush=True)
        time.sleep(delay)
