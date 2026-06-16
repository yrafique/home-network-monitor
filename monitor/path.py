"""MTR-style per-hop path monitoring via icmplib traceroute; exposes RTT/loss/jitter per hop."""
import os
import time

import enrich
from icmplib import traceroute
from prometheus_client import Gauge

PATH_TARGETS = [t.strip() for t in os.environ.get("PATH_TARGETS", "8.8.8.8").split(",") if t.strip()]
PATH_INTERVAL = int(os.environ.get("PATH_INTERVAL_SECONDS", "60"))
PATH_COUNT = int(os.environ.get("PATH_COUNT", "3"))
PATH_MAX_HOPS = int(os.environ.get("PATH_MAX_HOPS", "30"))
PATH_TIMEOUT = float(os.environ.get("PATH_HOP_TIMEOUT_SECONDS", "1"))

HOP_RTT = Gauge("internet_path_hop_rtt_avg_ms", "Average RTT to this hop", ["target", "hop", "address", "host", "owner"])
HOP_LOSS = Gauge("internet_path_hop_loss_ratio", "Packet loss 0..1 at this hop", ["target", "hop", "address", "host", "owner"])
HOP_JITTER = Gauge("internet_path_hop_jitter_ms", "Jitter at this hop", ["target", "hop", "address", "host", "owner"])
PATH_HOPS = Gauge("internet_path_hops_total", "Number of hops to the target", ["target"])
PATH_COMPLETE = Gauge("internet_path_complete", "1 if the trace reached the target", ["target"])


def _probe(target: str) -> None:
    try:
        hops = traceroute(
            target,
            count=PATH_COUNT,
            interval=0.05,
            timeout=PATH_TIMEOUT,
            max_hops=PATH_MAX_HOPS,
        )
    except Exception as exc:  # noqa: BLE001 - keep the thread alive
        PATH_COMPLETE.labels(target).set(0)
        print(f"[path] traceroute to {target} failed: {exc}", flush=True)
        return

    for hop in hops:
        host, owner = enrich.meta(hop.address)
        labels = (target, str(hop.distance), hop.address, host, owner)
        HOP_RTT.labels(*labels).set(hop.avg_rtt)
        HOP_LOSS.labels(*labels).set(hop.packet_loss)
        HOP_JITTER.labels(*labels).set(hop.jitter)

    PATH_HOPS.labels(target).set(hops[-1].distance if hops else 0)
    reached = bool(hops) and hops[-1].address == target
    PATH_COMPLETE.labels(target).set(1 if reached else 0)


def run() -> None:
    print(
        f"[path] per-hop monitoring | targets={PATH_TARGETS} "
        f"interval={PATH_INTERVAL}s count={PATH_COUNT}",
        flush=True,
    )
    while True:
        for target in PATH_TARGETS:
            _probe(target)
        time.sleep(PATH_INTERVAL)
