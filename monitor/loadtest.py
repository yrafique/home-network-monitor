"""Download/upload throughput and bufferbloat measurements via the Ookla Speedtest CLI."""
import json
import math
import os
import subprocess
import time

from prometheus_client import Counter, Gauge

LOADTEST_ENABLED = os.environ.get("LOADTEST_ENABLED", "true").lower() in ("1", "true", "yes", "on")
LOADTEST_INTERVAL = int(os.environ.get("LOADTEST_INTERVAL_SECONDS", "3600"))
START_DELAY = int(os.environ.get("LOADTEST_START_DELAY_SECONDS", "30"))
SPEEDTEST_BIN = os.environ.get("SPEEDTEST_BIN", "/usr/local/bin/speedtest")
SERVER_ID = os.environ.get("SPEEDTEST_SERVER_ID", "")  # optional: pin a server

DOWNLOAD_MBPS = Gauge("internet_speedtest_download_mbps", "Download throughput (Mbps)")
UPLOAD_MBPS = Gauge("internet_speedtest_upload_mbps", "Upload throughput (Mbps)")
DOWNLOAD_BYTES = Counter("internet_speedtest_download_bytes_total", "Cumulative bytes downloaded across all speedtest runs")
UPLOAD_BYTES = Counter("internet_speedtest_upload_bytes_total", "Cumulative bytes uploaded across all speedtest runs")

_BYTES_STATE = os.path.join(os.path.dirname(os.environ.get("OUTAGE_LOG_PATH", "/data/outages.jsonl")), "speedtest_bytes.json")

def _load_bytes_state() -> tuple[float, float]:
    try:
        with open(_BYTES_STATE) as f:
            s = json.loads(f.read())
        return float(s.get("download", 0)), float(s.get("upload", 0))
    except Exception:
        return 0.0, 0.0

def _save_bytes_state(down: float, up: float) -> None:
    try:
        with open(_BYTES_STATE, "w") as f:
            json.dump({"download": down, "upload": up}, f)
    except Exception:
        pass

_persisted_down, _persisted_up = _load_bytes_state()
if _persisted_down > 0:
    DOWNLOAD_BYTES.inc(_persisted_down)
if _persisted_up > 0:
    UPLOAD_BYTES.inc(_persisted_up)
IDLE_LAT = Gauge("internet_bufferbloat_idle_latency_ms", "Idle latency before load")
DOWN_LAT = Gauge("internet_bufferbloat_down_latency_ms", "Latency during download saturation")
UP_LAT = Gauge("internet_bufferbloat_up_latency_ms", "Latency during upload saturation")
BB_INCREASE = Gauge("internet_bufferbloat_increase_ms", "Worst latency increase under load")
BB_GRADE = Gauge("internet_bufferbloat_grade", "Bufferbloat grade: 5=A(best) .. 1=F(worst)")


def _grade(increase_ms: float) -> int:
    """DSLReports-style grade from the latency increase under load."""
    if increase_ms < 5:
        return 5  # A
    if increase_ms < 30:
        return 4  # B
    if increase_ms < 60:
        return 3  # C
    if increase_ms < 200:
        return 2  # D
    return 1      # F


def _run_once() -> None:
    cmd = [SPEEDTEST_BIN, "--accept-license", "--accept-gdpr", "--format=json"]
    if SERVER_ID:
        cmd += ["--server-id", SERVER_ID]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(f"[loadtest] speedtest rc={proc.returncode}: {proc.stderr.strip()[:200]}", flush=True)
        return

    d = json.loads(proc.stdout)
    down_mbps = d["download"]["bandwidth"] * 8 / 1e6   # bandwidth is bytes/sec
    up_mbps = d["upload"]["bandwidth"] * 8 / 1e6
    idle = d["ping"]["latency"]
    down_lat = d.get("download", {}).get("latency", {}).get("iqm", math.nan)
    up_lat = d.get("upload", {}).get("latency", {}).get("iqm", math.nan)

    DOWNLOAD_MBPS.set(down_mbps)
    UPLOAD_MBPS.set(up_mbps)
    dl = d["download"]["bytes"]
    ul = d["upload"]["bytes"]
    DOWNLOAD_BYTES.inc(dl)
    UPLOAD_BYTES.inc(ul)
    _save_bytes_state(DOWNLOAD_BYTES._value.get(), UPLOAD_BYTES._value.get())
    IDLE_LAT.set(idle)
    if not math.isnan(down_lat):
        DOWN_LAT.set(down_lat)
    if not math.isnan(up_lat):
        UP_LAT.set(up_lat)

    loaded = [v for v in (down_lat, up_lat) if not math.isnan(v)]
    if loaded:
        increase = max(loaded) - idle
        BB_INCREASE.set(max(0.0, increase))
        BB_GRADE.set(_grade(increase))

    server = d.get("server", {})
    print(
        f"[loadtest] {down_mbps:.1f}down/{up_mbps:.1f}up Mbps | "
        f"idle {idle:.0f}ms loaded down{down_lat:.0f}/up{up_lat:.0f}ms | "
        f"{server.get('name', '?')} {server.get('location', '')}",
        flush=True,
    )


def run() -> None:
    if not LOADTEST_ENABLED:
        print("[loadtest] disabled (LOADTEST_ENABLED=false)", flush=True)
        return
    print(f"[loadtest] Ookla speedtest+bufferbloat | interval={LOADTEST_INTERVAL}s", flush=True)
    time.sleep(START_DELAY)  # don't compete with startup / first probes
    while True:
        try:
            _run_once()
        except Exception as exc:  # noqa: BLE001 - never crash the monitor
            print(f"[loadtest] cycle failed: {exc}", flush=True)
        time.sleep(LOADTEST_INTERVAL)
