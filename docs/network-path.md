# Network Path Reference — hops to 8.8.8.8

Identities of the IPs that appear in the **Per-Hop Latency / Per-Hop Packet Loss**
panels. Your ISP is **Rogers Communications (AS812)**; the trace target `8.8.8.8`
lives on **Google (AS15169)**. Use this to read which hop a problem belongs to.

> Path can change (load balancing / rerouting), so IPs may differ between runs.
> Reverse-DNS + ASN looked up June 2026.

## The hops

| Hop | IP | Hostname (PTR) | Owner (ASN) | Geo* | Whose problem if it's bad |
|----:|----|----------------|-------------|------|---------------------------|
| 1 | `172.18.0.1` | — | **Docker bridge** (the container's own gateway) | local | Ignore — internal Docker hop, not a real network device |
| 2 | `10.0.0.1` | — | **Your router / gateway** (LAN) | home | **You** — Wi-Fi / router / local network |
| 3 | `173.35.206.1` | `pool-173-35-206-1.cpe.net.cable.rogers.com` | **Rogers** (AS812) | Ottawa | **Rogers** — first hop into the ISP (cable access / CMTS) |
| 4 | `209.148.253.173` | — | **Rogers** (AS812) | ON/QC | **Rogers** — aggregation |
| 5 | `69.63.248.89` | — | **Rogers** (AS812) | Ottawa | **Rogers** — core |
| 6 | `72.139.139.190` | `unallocated-static.rogers.com` | **Rogers** (AS812) | ON/QC | **Rogers** — core / peering edge |
| 7 | *(no reply)* | — | usually a Rogers↔Google peering router that hides from traceroute | — | Normal to be blank |
| 8 | `192.178.86.183` | — | **Google** (AS15169) | Montréal | Google's edge — beyond Rogers' control |
| 9 | `172.253.77.117` | — | **Google** (AS15169) | Montréal | Google internal |
| 10 | `8.8.8.8` | `dns.google` | **Google** (AS15169) | anycast | The destination |

\* Geo for backbone IPs is the *registered* location (often Rogers' Toronto/Montréal
registration), not necessarily where the box physically sits. Don't over-read it.

## How to read the Per-Hop **Packet Loss** panel (important)

ICMP loss at a single middle hop is **often harmless**. Routers de-prioritize
replying to traceroute probes, so one hop can show 10–40% "loss" while traffic
*through* it is perfectly fine.

The rule:
- **Loss that appears at a hop and then DISAPPEARS at later hops** → cosmetic. That
  router just rate-limits ICMP. Ignore it.
- **Loss that STARTS at a hop and CONTINUES through every hop after it, all the way
  to hop 10 (`8.8.8.8`)** → real. The problem is at or after that hop.

So the diagnosis maps directly:
- Real loss starting at **hop 2 (`10.0.0.1`)** → your router / Wi-Fi.
- Real loss starting at **hops 3–6 (Rogers)** → **your ISP** — this is the evidence
  to give Rogers (timestamped, with the hop IP).
- Loss only at **hops 8–10 (Google)** → Google's network or just ICMP de-prio;
  rarely actionable for you.

## How to read the Per-Hop **Latency** panel

Latency should climb gently hop by hop. Look for a **step jump** that then persists:
- Jump at **hop 2** → local (Wi-Fi is the prime suspect; a wired Pi should be flat here).
- Jump at **hops 3–6** → inside Rogers.
- A big jump between **hop 6 → 8** is normal (that's the Rogers→Google handoff and
  possibly a city change, e.g. Ottawa → Montréal).

## TL;DR for your situation
Anything bad at **hops 3–6 = Rogers' fault**. Anything bad at **hop 2 = your side**
(and on Wi-Fi, hop 2 is the most likely culprit — wiring the Pi makes hop 2 flat so
this distinction becomes clean).
