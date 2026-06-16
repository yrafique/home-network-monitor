"""Resolve IPs/hostnames/URLs to a friendly owner name and reverse-DNS hostname; cached per process."""
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

import requests

ENRICH = os.environ.get("ENRICH", "true").lower() in ("1", "true", "yes", "on")

_CACHE: dict[str, tuple[str, str]] = {}
_KNOWN = (
    ("rogers", "Rogers"), ("1e100", "Google"), ("google", "Google"),
    ("cloudflare", "Cloudflare"), ("one.one.one.one", "Cloudflare"),
    ("amazon", "AWS"), ("aws", "AWS"), ("microsoft", "Microsoft"),
    ("akamai", "Akamai"), ("fastly", "Fastly"), ("bell", "Bell"),
    ("comcast", "Comcast"), ("telus", "Telus"),
)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _reverse_dns(ip: str) -> str:
    try:
        socket.setdefaulttimeout(2)
        return socket.gethostbyaddr(ip)[0]
    except Exception:  # noqa: BLE001
        return ""


def _ipinfo_org(ip: str) -> str:
    try:
        resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=3)
        if resp.ok:
            return resp.json().get("org", "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _classify(ip: str, host: str) -> str:
    try:
        if ipaddress.ip_address(ip).is_private:
            return "Docker" if ip.startswith(("172.17.", "172.18.", "172.19.")) else "LAN"
    except ValueError:
        pass
    text = host.lower()
    for needle, name in _KNOWN:
        if needle in text:
            return name
    org = re.sub(r"^AS\d+\s+", "", _ipinfo_org(ip))
    low = org.lower()
    for needle, name in _KNOWN:
        if needle in low:
            return name
    if org:
        return org.split(",")[0][:24]
    if text:
        parts = text.split(".")
        return parts[-2].capitalize() if len(parts) >= 2 else text
    return "unknown"


def meta(target: str) -> tuple[str, str]:
    """Return (hostname, owner) for an IP or hostname. Cached."""
    if not ENRICH:
        return (target, "")
    if target in _CACHE:
        return _CACHE[target]
    try:
        ip = target if _is_ip(target) else socket.gethostbyname(target)
    except Exception:  # noqa: BLE001
        ip = target
    host = _reverse_dns(ip)
    if not host:
        host = target if not _is_ip(target) else ip
    _CACHE[target] = (host, _classify(ip, host or target))
    return _CACHE[target]


def owner(target: str) -> str:
    return meta(target)[1]


def url_owner(url: str) -> str:
    return owner(urlparse(url).hostname or url)
