"""Gate 1: fetch to quarantine. Never decodes, parses or executes content.

DNS pinning: the default opener connects to the IP validated by policy.check
(http.client subclass overriding connect, TLS SNI/cert check still use the hostname),
so the connection cannot be re-pointed by a later DNS answer. fetch also re-resolves
and requires the new answer to be a subset of the validated set (catches rebinding
for custom openers). Not closed: the validated set may contain several IPs; we
connect to the first only.
"""
import hashlib
import http.client
import os
import socket
import ssl
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import policy
from .safe import token
from .policy import PolicyReject, system_resolver

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class FetchError(Exception):
    """reason/code are fixed strings or sanitised tokens, never raw response text."""

    def __init__(self, reason, code="fetch_error"):
        super().__init__(reason)
        self.reason, self.code = reason, code


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _Response:
    def __init__(self, conn, resp):
        self._conn, self._resp = conn, resp
        self.status = resp.status
        self.headers = {k.lower(): v for k, v in resp.getheaders()}

    def read(self, n):
        return self._resp.read(n)

    def set_timeout(self, seconds):
        if self._conn.sock is not None:
            self._conn.sock.settimeout(seconds)

    def close(self):
        self._conn.close()


def default_opener(req):
    """req: {url, host, port, ip, path, headers, timeout}. Returns object with status/headers/read/close."""
    conn = _PinnedHTTPSConnection(req["host"], req["ip"], req["port"], req["timeout"],
                                  ssl.create_default_context())
    try:
        conn.request("GET", req["path"], headers=req["headers"])
        return _Response(conn, conn.getresponse())
    except Exception:
        conn.close()
        raise


def quarantine_dir(cfg):
    raw = os.environ.get("INTAKE_QUARANTINE_DIR") or cfg.get("quarantine_dir")
    if not raw:
        raise FetchError("no quarantine_dir configured (config or INTAKE_QUARANTINE_DIR)")
    d = Path(raw).expanduser().resolve()
    if d == Path("/tmp") or Path("/tmp") in d.parents:
        raise FetchError(f"quarantine dir {d} is under /tmp, which is not allowed")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(d.stat().st_mode) & 0o077:
        os.chmod(d, 0o700)
    return d


def raw_dir(cfg):
    d = quarantine_dir(cfg) / cfg["raw_subdir"]
    d.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(d.stat().st_mode) & 0o077:
        os.chmod(d, 0o700)
    return d


def _remaining(deadline, clock):
    left = deadline - clock()
    if left <= 0:
        raise FetchError("total fetch deadline exceeded", "deadline_exceeded")
    return left


def _hop(url, cfg, resolver, opener, deadline, clock):
    _remaining(deadline, clock)
    info = policy.check(url, cfg, resolver)
    again = {str(a) for a in resolver(info["host"], info["port"])}
    if not again <= set(info["ips"]):
        raise PolicyReject(f"dns answer for {info['host']!r} changed between checks (possible rebinding)")
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    req = {
        "url": url, "host": info["host"], "port": info["port"], "ip": info["ips"][0],
        "path": path, "timeout": min(cfg["timeout_s"], _remaining(deadline, clock)),
        "headers": {"User-Agent": cfg["user_agent"], "Accept-Encoding": "identity",
                    "Accept": ", ".join(cfg["allowed_content_types"]), "Host": parts.netloc},
    }
    try:
        return opener(req)
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"request to {token(info['host'])} failed: {type(e).__name__}")


def fetch_to_quarantine(url, cfg, opener=None, resolver=None, clock=time.monotonic):
    opener = opener or default_opener
    resolver = resolver or system_resolver
    deadline = clock() + cfg["total_deadline_s"]
    rdir = raw_dir(cfg)
    current, seen = url, set()
    for hop in range(cfg["max_redirects"] + 1):
        if current in seen:
            raise FetchError(f"redirect loop at {token(current)}")
        seen.add(current)
        try:
            resp = _hop(current, cfg, resolver, opener, deadline, clock)
        except PolicyReject as e:
            if hop == 0:
                raise
            raise PolicyReject(f"redirect target rejected by policy ({token(e.reason)})")
        try:
            if resp.status in REDIRECT_STATUSES:
                if hop >= cfg["max_redirects"]:
                    raise FetchError(f"redirect refused: more than {cfg['max_redirects']} redirects allowed")
                loc = resp.headers.get("location")
                try:
                    current = policy.check_redirect(current, loc, cfg, resolver)["url"]
                except PolicyReject as e:
                    raise PolicyReject(f"redirect target rejected by policy ({token(e.reason)})")
                continue
            if resp.status != 200:
                raise FetchError(f"unexpected http status {token(resp.status)}")
            return _store(resp, current, cfg, rdir, deadline, clock)
        finally:
            resp.close()
    raise FetchError(f"more than {cfg['max_redirects']} redirects")


def _store(resp, url, cfg, qdir, deadline, clock):
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in cfg["allowed_content_types"]:
        raise FetchError(f"content-type {token(ctype)} not allowed")
    enc = resp.headers.get("content-encoding", "identity").strip().lower()
    if enc not in ("", "identity"):
        raise FetchError(f"content-encoding {token(enc)} not allowed")
    cap = cfg["max_bytes"]
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and (len(declared) > 18 or int(declared) > cap):
        raise FetchError(f"declared content-length exceeds cap {cap}")
    h, total = hashlib.sha256(), 0
    tmp = qdir / f".part-{os.getpid()}-{id(resp)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                left = _remaining(deadline, clock)
                if hasattr(resp, "set_timeout"):
                    resp.set_timeout(min(cfg["timeout_s"], left))
                chunk = resp.read(cfg["chunk_bytes"])
                if not chunk:
                    _remaining(deadline, clock)
                    break
                total += len(chunk)
                if total > cap:
                    raise FetchError(f"body exceeds cap {cap} bytes")
                h.update(chunk)
                f.write(chunk)
        digest = h.hexdigest()
        final = qdir / f"{digest}.raw"
        os.replace(tmp, final)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return {"url_final": url, "status": resp.status, "content_type": ctype,
            "bytes_len": total, "sha256": digest, "path": str(final)}
