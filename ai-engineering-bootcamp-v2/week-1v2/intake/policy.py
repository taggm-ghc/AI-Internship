"""Gate 0: URL policy. Pure checks plus an injectable resolver; raises PolicyReject."""
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "intake_policy.json"


class PolicyReject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_config(path=None):
    with open(path or DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def system_resolver(host, port):
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [i[4][0] for i in infos]


def _suffix_match(host, suffixes):
    return any(host == s or host.endswith("." + s) for s in (x.lower().strip(".") for x in suffixes))


def _looks_like_ip_literal(host):
    if ":" in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)  # legacy forms: 2130706433, 0x7f.1, 017700000001, 127.1
        return True
    except OSError:
        pass
    last = host.rsplit(".", 1)[-1]
    return bool(re.fullmatch(r"\d+|0[xX][0-9a-fA-F]*", last))


def _embedded_v4(ip):
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in ipaddress.ip_network("2002::/16"):
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def _check_ip(addr, cfg):
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        raise PolicyReject(f"resolver returned unparseable address {addr!r}")
    candidates = [ip]
    if ip.version == 6:
        v4 = _embedded_v4(ip)
        if v4:
            candidates.append(v4)
    nets = [ipaddress.ip_network(n) for n in cfg.get("extra_blocked_networks", [])]
    for c in candidates:
        if (c.is_private or c.is_loopback or c.is_link_local or c.is_reserved
                or c.is_multicast or c.is_unspecified or not c.is_global):
            raise PolicyReject(f"address {addr} is not a public unicast address")
    for n in nets:
        if ip.version == n.version and ip in n:
            raise PolicyReject(f"address {addr} is in blocked network {n}")
    return ip


def check(url, cfg, resolver=None):
    """Validate url; return {url, host, port, ips} (ips = all validated addresses)."""
    resolver = resolver or system_resolver
    if not isinstance(url, str) or not url or any(c in url for c in "\r\n\t\x00 "):
        raise PolicyReject("url is empty or contains whitespace/control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise PolicyReject(f"url unparseable: {e}")
    if parts.scheme.lower() not in cfg["allowed_schemes"]:
        raise PolicyReject(f"scheme {parts.scheme!r} not allowed")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise PolicyReject("userinfo in url not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise PolicyReject("url has no host")
    if parts.netloc.startswith("[") or _looks_like_ip_literal(host):
        raise PolicyReject(f"ip-literal host {host!r} not allowed")
    port = port or 443
    if port not in cfg["allowed_ports"]:
        raise PolicyReject(f"port {port} not allowed")
    if _suffix_match(host, cfg.get("denied_host_suffixes", [])):
        raise PolicyReject(f"host {host!r} is denied")
    allowed = cfg.get("allowed_host_suffixes", [])
    if allowed and not _suffix_match(host, allowed):
        raise PolicyReject(f"host {host!r} is not on the allow list")
    try:
        addrs = resolver(host, port)
    except OSError as e:
        raise PolicyReject(f"dns resolution failed for {host!r}: {e}")
    if not addrs:
        raise PolicyReject(f"dns returned no addresses for {host!r}")
    ips = [str(_check_ip(a, cfg)) for a in addrs]
    return {"url": url, "host": host, "port": port, "ips": ips}


def check_redirect(current_url, location, cfg, resolver=None):
    """Re-validate a redirect target (resolved against current_url) with the full policy."""
    if not location:
        raise PolicyReject("redirect without Location header")
    return check(urljoin(current_url, location), cfg, resolver)
