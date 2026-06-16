"""Tracks up/down transitions per target and appends structured outage events to a JSONL log."""
import json
import os
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import enrich
import notifier
from prometheus_client import Counter, Gauge

LOG_PATH = os.environ.get("OUTAGE_LOG_PATH", "/data/outages.jsonl")
ALERT_TARGETS = {
    t.strip() for t in os.environ.get("ALERT_TARGETS", "internet").split(",") if t.strip()
}
try:
    ALERT_TZ = ZoneInfo(os.environ.get("ALERT_TZ", "UTC"))
except Exception:  # noqa: BLE001 - bad/missing tz falls back to UTC
    ALERT_TZ = timezone.utc

OUTAGES_TOTAL = Counter(
    "internet_outages_total", "Completed outages observed", ["target", "owner"]
)
CURRENT_OUTAGE = Gauge(
    "internet_current_outage_seconds",
    "Seconds the target has been continuously down (0 if up)",
    ["target", "owner"],
)
LAST_OUTAGE = Gauge(
    "internet_last_outage_duration_seconds",
    "Duration of the most recent completed outage",
    ["target", "owner"],
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _clock(ts: float) -> str:
    """Human local time like '14:03:21' in the configured ALERT_TZ."""
    return datetime.fromtimestamp(ts, tz=ALERT_TZ).strftime("%H:%M:%S")


def _human(seconds: float) -> str:
    """Format a duration like '2m 14s' or '1h 03m'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _label(target: str) -> str:
    return "Internet (all providers)" if target == "internet" else target


def _notify(text: str) -> None:
    """Fire a WhatsApp alert without blocking the probe loop."""
    threading.Thread(target=notifier.send_whatsapp, args=(text,), daemon=True).start()


class OutageLog:
    """Stateful detector turning per-cycle up/down readings into outage events."""

    def __init__(self, path: str = LOG_PATH):
        self.path = path
        self._down_since: dict[str, float] = {}
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _write(self, event: dict) -> None:
        line = json.dumps(event)
        print(f"[outage] {line}", flush=True)
        try:
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"[outage] log write failed: {exc}", flush=True)

    def record(self, states: dict[str, int]) -> None:
        """Feed this cycle's {target: up(1/0)} readings; emit transition events."""
        if not states:
            return
        now = time.time()
        wan_states = {t: v for t, v in states.items() if enrich.owner(t) != "LAN"}
        wan_up = max(wan_states.values()) if wan_states else max(states.values())
        full = dict(states)
        full["internet"] = 1 if wan_up > 0 else 0

        with self._lock:
            for target, up in full.items():
                owner = "All" if target == "internet" else enrich.owner(target)
                if up == 0:
                    if target not in self._down_since:
                        self._down_since[target] = now
                        self._write(
                            {
                                "event": "outage_start",
                                "target": target,
                                "start": now,
                                "start_iso": _iso(now),
                            }
                        )
                        if target in ALERT_TARGETS:
                            _notify(f"🔴 {_label(target)} DOWN at {_clock(now)}")
                    CURRENT_OUTAGE.labels(target, owner).set(now - self._down_since[target])
                else:
                    start = self._down_since.pop(target, None)
                    if start is not None:
                        duration = now - start
                        OUTAGES_TOTAL.labels(target, owner).inc()
                        LAST_OUTAGE.labels(target, owner).set(duration)
                        self._write(
                            {
                                "event": "outage_end",
                                "target": target,
                                "start": start,
                                "end": now,
                                "duration_seconds": round(duration, 1),
                                "start_iso": _iso(start),
                                "end_iso": _iso(now),
                            }
                        )
                        if target in ALERT_TARGETS:
                            _notify(
                                f"🟢 {_label(target)} RESTORED at {_clock(now)} "
                                f"— was down {_human(duration)} "
                                f"(since {_clock(start)})"
                            )
                    CURRENT_OUTAGE.labels(target, owner).set(0)
