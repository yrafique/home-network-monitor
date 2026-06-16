#!/usr/bin/env python3
"""LAN device scanner: nmap + mDNS + SSDP + DHCP. Exposes Prometheus metrics on :8001."""
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import defusedxml.ElementTree as ET

import nmap
from prometheus_client import Gauge, start_http_server
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf


def _collect_own_ips() -> set[str]:
    ips: set[str] = set()
    try:
        out = subprocess.check_output(["ip", "-4", "addr", "show"], text=True, stderr=subprocess.DEVNULL)
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            ips.add(m.group(1))
        if ips:
            return ips
    except Exception:
        pass
    for flag in ["-i", "-I"]:
        try:
            out = subprocess.check_output(["hostname", flag], text=True, stderr=subprocess.DEVNULL)
            ips.update(out.split())
            if ips:
                return ips
        except Exception:
            pass
    return ips


def _detect_lan_subnet(fallback: str = "10.0.0.0/24") -> tuple[str, str]:
    try:
        out = subprocess.check_output(["ip", "route", "show"], text=True, stderr=subprocess.DEVNULL)
        gateway = iface = subnet = ""
        for line in out.splitlines():
            if line.startswith("default "):
                m = re.search(r"via (\S+).*dev (\S+)", line)
                if m:
                    gateway, iface = m.group(1), m.group(2)
        for line in out.splitlines():
            if not line.startswith("default") and iface and f"dev {iface}" in line:
                m = re.match(r"(\d+\.\d+\.\d+\.\d+/\d+)", line)
                if m:
                    subnet = m.group(1)
                    break
        if subnet and gateway:
            return subnet, gateway
    except Exception:
        pass
    for ip in _OWN_IPS:
        if not ip.startswith("127.") and not ip.startswith("172."):
            parts = ip.rsplit(".", 1)
            return f"{parts[0]}.0/24", f"{parts[0]}.1"
    return fallback, fallback.rsplit(".", 1)[0] + ".1"


_OWN_IPS: set[str] = _collect_own_ips()

_AUTO_SUBNET, _AUTO_GATEWAY = _detect_lan_subnet()
LAN_SUBNET           = os.environ.get("LAN_SUBNET",  _AUTO_SUBNET)
LAN_GATEWAY          = os.environ.get("LAN_GATEWAY", _AUTO_GATEWAY)
LAN_SCAN_INTERVAL    = int(os.environ.get("LAN_SCAN_INTERVAL_SECONDS", "120"))
LAN_PORTS            = os.environ.get(
    "LAN_PORTS",
    "21,22,23,25,53,80,443,445,548,631,1883,3000,3001,4200,5000,5353,7080,8080,8081,8443,8888,9090,9100,32400",
)
EXPORTER_PORT        = int(os.environ.get("LAN_EXPORTER_PORT", "8001"))
OS_SCAN_ENABLED      = os.environ.get("OS_SCAN_ENABLED", "false").lower() in ("1", "true", "yes")
NMAP_OUI_PATH        = "/usr/share/nmap/nmap-mac-prefixes"

DEVICE_INFO = Gauge(
    "lan_device_info", "LAN device metadata (info metric)",
    ["ip", "mac", "hostname", "vendor", "os_family", "device_type", "mdns_name"],
)
DEVICE_ONLINE   = Gauge("lan_device_online",         "1 if device responded",          ["ip"])
DEVICE_RTT      = Gauge("lan_device_rtt_ms",         "ARP round-trip time (ms)",       ["ip"])
DEVICE_PORT     = Gauge("lan_device_open_port",      "1 per open port detected",       ["ip", "port", "service", "product"])
DEVICES_TOTAL   = Gauge("lan_devices_total",         "Total devices seen this cycle")
DEVICES_ONLINE  = Gauge("lan_devices_online",        "Devices that responded this cycle")
SCAN_DURATION   = Gauge("lan_scan_duration_seconds", "Wall-clock time for full scan cycle")

_oui_db: dict[str, str] = {}
_oui_lock = threading.Lock()


def _load_oui_db() -> None:
    try:
        db: dict[str, str] = {}
        with open(NMAP_OUI_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and len(parts[0]) == 6:
                    raw = parts[0].lower()
                    prefix = ":".join(raw[i:i+2] for i in range(0, 6, 2))
                    db[prefix] = parts[1].strip()
        with _oui_lock:
            _oui_db = db
        print(f"[lanscan] OUI database loaded: {len(db):,} entries (nmap bundled)", flush=True)
    except Exception as exc:
        print(f"[lanscan] OUI load failed: {exc}", flush=True)


def _vendor_from_oui(mac: str) -> str:
    if not mac or mac == "unknown":
        return "Unknown"
    try:
        if int(mac.split(":")[0], 16) & 0x02:
            return "Randomized MAC"
    except (ValueError, IndexError):
        pass
    with _oui_lock:
        return _oui_db.get(mac.lower()[:8], "Unknown")


_mdns_names: dict[str, str] = {}
_mdns_lock = threading.Lock()

MDNS_SERVICES = [
    "_http._tcp.local.", "_https._tcp.local.", "_airplay._tcp.local.",
    "_googlecast._tcp.local.", "_spotify-connect._tcp.local.", "_sonos._tcp.local.",
    "_homekit._tcp.local.", "_hap._tcp.local.", "_printer._tcp.local.",
    "_ipp._tcp.local.", "_ssh._tcp.local.", "_sftp-ssh._tcp.local.",
    "_smb._tcp.local.", "_afpovertcp._tcp.local.", "_daap._tcp.local.",
    "_raop._tcp.local.", "_device-info._tcp.local.", "_matter._tcp.local.",
    "_meshcop._udp.local.",
]


class _MDNSListener(ServiceListener):
    def add_service(self, zc: Zeroconf, svc_type: str, name: str) -> None:
        try:
            info = zc.get_service_info(svc_type, name, timeout=2000)
            if not info or not info.addresses:
                return
            friendly = info.server.rstrip(".") if info.server else name.split(".")[0]
            our_prefix = LAN_SUBNET.rsplit(".", 2)[0]
            for addr_bytes in info.addresses:
                ip = socket.inet_ntoa(addr_bytes)
                if (ip.startswith("169.254.") or ip.startswith("127.")
                        or ip == "0.0.0.0" or ip.startswith("172.")
                        or ip in _OWN_IPS or not ip.startswith(our_prefix)):
                    continue
                with _mdns_lock:
                    _mdns_names[ip] = friendly
        except Exception:
            pass

    def remove_service(self, zc: Zeroconf, svc_type: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, svc_type: str, name: str) -> None:
        self.add_service(zc, svc_type, name)


def _run_mdns_listener() -> None:
    try:
        zc = Zeroconf()
        listener = _MDNSListener()
        browsers = [ServiceBrowser(zc, svc, listener) for svc in MDNS_SERVICES]
        print(f"[lanscan] mDNS listener active on {len(browsers)} service types", flush=True)
        while True:
            time.sleep(60)
    except Exception as exc:
        print(f"[lanscan] mDNS listener error: {exc}", flush=True)


_ssdp_names: dict[str, str] = {}
_ssdp_lock = threading.Lock()

SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 3\r\n"
    "ST: ssdp:all\r\n\r\n"
)


def _fetch_upnp_name(location: str, timeout: float = 3.0) -> str:
    try:
        req = urllib.request.Request(location, headers={"User-Agent": "lanscan/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml = resp.read(32768)
        root = ET.fromstring(xml)
        ns = {"u": re.search(r'\{(.+?)\}', root.tag).group(1)} if root.tag.startswith("{") else {}
        def find(tag: str) -> str:
            el = root.find(f".//{{{ns['u']}}}{tag}") if ns else root.find(f".//{tag}")
            return (el.text or "").strip() if el is not None else ""
        return " — ".join(filter(None, [find("friendlyName") or find("modelName"), find("manufacturer")]))[:80]
    except Exception:
        return ""


def _ssdp_scan() -> None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(5)
        sock.sendto(SSDP_MSEARCH.encode(), ("239.255.255.250", 1900))
        responses: dict[str, str] = {}
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                ip = addr[0]
                if ip in _OWN_IPS or ip in responses:
                    continue
                text = data.decode(errors="replace")
                location = server = ""
                for line in text.splitlines():
                    low = line.lower()
                    if low.startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                    elif low.startswith("server:"):
                        server = line.split(":", 1)[1].strip()
                responses[ip] = location or server
            except socket.timeout:
                break
        sock.close()

        def _resolve(ip: str, loc: str) -> None:
            name = _fetch_upnp_name(loc) if loc.startswith("http") else loc[:60]
            if name:
                with _ssdp_lock:
                    _ssdp_names[ip] = name

        threads = [threading.Thread(target=_resolve, args=(ip, loc), daemon=True) for ip, loc in responses.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        if responses:
            print(
                f"[lanscan] SSDP: {len(responses)} devices → "
                + ", ".join(f"{ip}={_ssdp_names.get(ip,'?')[:30]}" for ip in list(responses)[:5]),
                flush=True,
            )
    except Exception as exc:
        print(f"[lanscan] SSDP scan error: {exc}", flush=True)


def _run_ssdp_loop() -> None:
    while True:
        _ssdp_scan()
        time.sleep(60)


_dhcp_names: dict[str, str] = {}
_mac_names:  dict[str, str] = {}
_dhcp_lock = threading.Lock()


def _parse_dhcp(pkt: bytes) -> None:
    if len(pkt) < 42:
        return
    eth_type = struct.unpack_from("!H", pkt, 12)[0]
    if eth_type != 0x0800:
        return
    src_mac = ":".join(f"{b:02x}" for b in pkt[6:12])
    ip_offset = 14
    ihl = (pkt[ip_offset] & 0x0F) * 4
    if pkt[ip_offset + 9] != 17:
        return
    udp_offset = ip_offset + ihl
    if len(pkt) < udp_offset + 8:
        return
    src_port, dst_port = struct.unpack_from("!HH", pkt, udp_offset)
    if 67 not in (src_port, dst_port) and 68 not in (src_port, dst_port):
        return
    dhcp_offset = udp_offset + 8
    if len(pkt) < dhcp_offset + 236:
        return
    ciaddr = socket.inet_ntoa(pkt[dhcp_offset + 12: dhcp_offset + 16])
    yiaddr = socket.inet_ntoa(pkt[dhcp_offset + 16: dhcp_offset + 20])
    if pkt[dhcp_offset + 236: dhcp_offset + 240] != b'\x63\x82\x53\x63':
        return
    opts_offset = dhcp_offset + 240
    hostname = ""
    msg_type = 0
    while opts_offset < len(pkt):
        opt = pkt[opts_offset]
        if opt == 255:
            break
        if opt == 0:
            opts_offset += 1
            continue
        if opts_offset + 1 >= len(pkt):
            break
        length = pkt[opts_offset + 1]
        val = pkt[opts_offset + 2: opts_offset + 2 + length]
        if opt == 53 and length == 1:
            msg_type = val[0]
        elif opt == 12:
            hostname = val.decode(errors="replace").strip()
        opts_offset += 2 + length

    if not hostname or hostname.lower() in ("localhost",):
        return
    with _dhcp_lock:
        if src_mac:
            _mac_names[src_mac] = hostname
        if msg_type == 5 and yiaddr != "0.0.0.0":
            _dhcp_names[yiaddr] = hostname
            print(f"[lanscan] DHCP ACK: {yiaddr} ({src_mac}) → {hostname!r}", flush=True)
        elif ciaddr != "0.0.0.0":
            _dhcp_names[ciaddr] = hostname


def _run_dhcp_sniffer() -> None:
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
        sock.settimeout(5.0)
        print("[lanscan] DHCP sniffer active (raw AF_PACKET, no libpcap)", flush=True)
        while True:
            try:
                _parse_dhcp(sock.recv(65535))
            except socket.timeout:
                continue
            except Exception:
                pass
    except Exception as exc:
        print(f"[lanscan] DHCP sniffer failed: {exc}", flush=True)


def _classify_device(vendor: str, os_family: str, mdns: str, ssdp: str, ports: list[str]) -> str:
    combined = " ".join([vendor, os_family, mdns, ssdp]).lower()
    port_set = set(ports)

    checks = [
        (["raspberry", "raspberrypi"],                           "Raspberry Pi"),
        (["sonos"],                                              "Sonos Speaker"),
        (["apple", "iphone", "ipad", "airplay", "raop", "daap", "homepod", "appleTV"], "Apple Device"),
        (["android", "google", "chromecast", "googlecast"],      "Google / Android"),
        (["samsung"],                                            "Samsung Device"),
        (["hp ", "hewlett", "jetdirect", "hpsetup"],             "HP Printer"),
        (["canon", "epson", "brother", "xerox", "lexmark"],      "Printer"),
        (["tuya", "meross", "shelly", "tasmota", "espressif", "chengdu"], "Smart Home / IoT"),
        (["vantiva", "hitron", "rogers", "arris", "technicolor"], "Router / Modem"),
        (["netgear", "asus", "linksys", "ubiquiti", "mikrotik", "zyxel", "d-link"], "Network Device"),
        (["synology", "qnap", "drobo", "readynas"],              "NAS"),
        (["windows", "microsoft", "workstation"],                "Windows PC"),
        (["linux", "ubuntu", "debian", "centos", "fedora"],      "Linux Device"),
        (["randomized"],                                         "Mobile Device"),
    ]
    for keywords, label in checks:
        if any(k in combined for k in keywords):
            if label == "Network Device" and any(k in combined for k in ["tp-link", "tplink"]):
                if any(k in combined for k in ["kasa", "tapo", "hs1", "hs2", "ep2", "lb1"]):
                    return "Smart Home / IoT"
            return label
    if any(k in combined for k in ["tp-link", "tplink"]):
        if any(k in combined for k in ["kasa", "tapo", "hs1", "hs2", "ep2", "lb1"]):
            return "Smart Home / IoT"
        return "Network Device"
    if "631" in port_set or "ipp" in combined:
        return "Printer"
    if "445" in port_set or "139" in port_set:
        return "Windows / Samba"
    if "1883" in port_set:
        return "MQTT Broker"
    if "9090" in port_set or "9100" in port_set or "3000" in port_set:
        return "Monitoring"
    if "22" in port_set:
        return "Linux / Server"
    if "80" in port_set or "443" in port_set:
        return "Web Device"
    return "Unknown"


def _nmap_discovery(nm: nmap.PortScanner) -> dict[str, dict]:
    try:
        nm.scan(hosts=LAN_SUBNET, arguments="-sn -PR --send-eth -T4 --host-timeout 5s")
    except Exception as exc:
        print(f"[lanscan] discovery scan error: {exc}", flush=True)
        return {}

    devices: dict[str, dict] = {}
    for ip in nm.all_hosts():
        h = nm[ip]
        if h.state() != "up":
            continue
        mac = h["addresses"].get("mac", "").lower()
        vendor = (next(iter(h["vendor"].values()), "") if mac and h.get("vendor") else "") or _vendor_from_oui(mac) if mac else "Unknown"
        rtt = 0.0
        try:
            rtt = float(h.get("times", {}).get("rtt", "0")) / 1000.0
        except (TypeError, ValueError):
            pass
        devices[ip] = {"mac": mac or "unknown", "vendor": vendor, "hostname": h.hostname() or ip, "rtt_ms": rtt}
    return devices


def _nmap_details(nm: nmap.PortScanner, ips: list[str]) -> dict[str, dict]:
    if not ips:
        return {}
    args = f"-T4 --open -p {LAN_PORTS} -sV --host-timeout 90s"
    if OS_SCAN_ENABLED:
        args += " -O --osscan-guess --script=smb-os-discovery,nbstat"
    try:
        nm.scan(hosts=" ".join(ips), arguments=args)
    except Exception as exc:
        print(f"[lanscan] detail scan error: {exc}", flush=True)
        return {}

    results: dict[str, dict] = {}
    for ip in nm.all_hosts():
        h = nm[ip]
        ports = [
            {"port": str(port), "service": info.get("name", ""), "product": (info.get("product", "") or info.get("version", ""))[:50]}
            for proto in h.all_protocols()
            for port, info in h[proto].items()
            if info["state"] == "open"
        ]
        os_family = ""
        if OS_SCAN_ENABLED:
            os_matches = h.get("osmatch", [])
            if os_matches:
                best = os_matches[0]
                os_family = best.get("osclass", [{}])[0].get("osfamily", "") if best.get("osclass") else best.get("name", "")[:40]
            if not os_family:
                for script in h.get("hostscript", []):
                    if script.get("id") in ("smb-os-discovery", "nbstat"):
                        m = re.search(r"OS:\s*(.+)", script.get("output", ""))
                        if m:
                            os_family = m.group(1).strip()[:40]
                            break
        results[ip] = {"ports": ports, "os_family": os_family}
    return results


_prev_online: set[str] = set()


def _scan_once() -> None:
    global _prev_online
    t0 = time.perf_counter()
    nm = nmap.PortScanner()

    devices = _nmap_discovery(nm)
    current_online = set(devices)
    details = _nmap_details(nm, list(current_online))

    DEVICE_INFO.clear()
    DEVICE_ONLINE.clear()
    DEVICE_RTT.clear()
    DEVICE_PORT.clear()

    with _mdns_lock:
        mdns_snapshot = dict(_mdns_names)
    with _ssdp_lock:
        ssdp_snapshot = dict(_ssdp_names)
    with _dhcp_lock:
        dhcp_snapshot = dict(_dhcp_names)
        mac_snapshot  = dict(_mac_names)

    for ip, info in devices.items():
        det       = details.get(ip, {})
        ports     = det.get("ports", [])
        os_family = det.get("os_family", "")
        mdns_name = mdns_snapshot.get(ip, "")
        ssdp_name = ssdp_snapshot.get(ip, "")
        dhcp_name = dhcp_snapshot.get(ip) or mac_snapshot.get(info["mac"], "")
        port_nums = [p["port"] for p in ports]
        best_hostname = mdns_name or dhcp_name or info["hostname"]
        device_type   = _classify_device(info["vendor"], os_family, mdns_name or dhcp_name, ssdp_name, port_nums)

        DEVICE_INFO.labels(
            ip=ip, mac=info["mac"], hostname=best_hostname,
            vendor=info["vendor"], os_family=os_family,
            device_type=device_type, mdns_name=mdns_name,
        ).set(1)
        DEVICE_ONLINE.labels(ip=ip).set(1)
        if info["rtt_ms"] > 0:
            DEVICE_RTT.labels(ip=ip).set(info["rtt_ms"])
        for p in ports:
            DEVICE_PORT.labels(ip=ip, port=p["port"], service=p["service"], product=p["product"]).set(1)

    DEVICES_TOTAL.set(len(devices))
    DEVICES_ONLINE.set(len(current_online))
    elapsed = time.perf_counter() - t0
    SCAN_DURATION.set(elapsed)
    _prev_online = current_online

    print(
        f"[lanscan] {len(current_online)} online | "
        f"{sum(len(details.get(ip,{}).get('ports',[])) for ip in current_online)} open ports | "
        f"mDNS: {len(mdns_snapshot)} | SSDP: {len(ssdp_snapshot)} | DHCP: {len(dhcp_snapshot)} | {elapsed:.1f}s",
        flush=True,
    )


def main() -> None:
    print(
        f"[lanscan] starting on :{EXPORTER_PORT} | "
        f"subnet={LAN_SUBNET} gateway={LAN_GATEWAY} interval={LAN_SCAN_INTERVAL}s",
        flush=True,
    )
    start_http_server(EXPORTER_PORT)
    threading.Thread(target=_load_oui_db,       daemon=True).start()
    threading.Thread(target=_run_mdns_listener, daemon=True, name="mdns").start()
    threading.Thread(target=_run_ssdp_loop,     daemon=True, name="ssdp").start()
    threading.Thread(target=_run_dhcp_sniffer,  daemon=True, name="dhcp").start()
    time.sleep(5)
    while True:
        try:
            _scan_once()
        except Exception as exc:
            print(f"[lanscan] cycle error: {exc}", flush=True)
        time.sleep(LAN_SCAN_INTERVAL)


if __name__ == "__main__":
    main()
