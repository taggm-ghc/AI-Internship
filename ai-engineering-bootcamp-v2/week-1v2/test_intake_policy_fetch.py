import hashlib
import os
import stat

import pytest

from intake import fetch, policy
from intake.policy import PolicyReject

PUB = "93.184.216.34"


@pytest.fixture
def cfg(tmp_path):
    c = policy.load_config()
    c["quarantine_dir"] = str(tmp_path / "q")
    c["max_bytes"] = 1000
    c["chunk_bytes"] = 100
    os.environ.pop("INTAKE_QUARANTINE_DIR", None)
    return c


def res(mapping, default=(PUB,)):
    return lambda host, port: list(mapping.get(host, default))


class Resp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self.headers, self._b, self.closed = status, headers or {}, body, False
        self.reads = 0

    def read(self, n):
        self.reads += 1
        out, self._b = self._b[:n], self._b[n:]
        return out

    def close(self):
        self.closed = True


def opener_for(table):
    calls = []

    def op(req):
        calls.append(req)
        return table[req["url"]]
    op.calls = calls
    return op


@pytest.mark.parametrize("url,frag", [
    ("http://example.org/", "scheme"),
    ("https://user:pw@example.org/", "userinfo"),
    ("https://user@example.org/", "userinfo"),
    ("https://example.org:8443/", "port"),
    ("https://localhost/", "denied"),
    ("https://svc.internal/", "denied"),
    ("https://127.0.0.1/", "ip-literal"),
    ("https://[::1]/", "ip-literal"),
    ("https://[::ffff:127.0.0.1]/", "ip-literal"),
    ("https://2130706433/", "ip-literal"),
    ("https://0x7f.0.0.1/", "ip-literal"),
    ("https://0177.0.0.1/", "ip-literal"),
    ("https://127.1/", "ip-literal"),
    ("https://0x7f000001/", "ip-literal"),
    ("https://example.org/a b", "whitespace"),
    ("https:///path", "no host"),
])
def test_static_rejects(cfg, url, frag):
    with pytest.raises(PolicyReject) as e:
        policy.check(url, cfg, res({}))
    assert frag in e.value.reason


@pytest.mark.parametrize("addr", [
    "10.0.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1", "172.16.0.5", "224.0.0.1",
    "0.0.0.0", "240.0.0.1", "100.64.0.1", "::1", "fe80::1", "fc00::1", "ff02::1",
    "::ffff:10.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1", "2002:7f00:1::1",
])
def test_resolved_private_rejected(cfg, addr):
    with pytest.raises(PolicyReject):
        policy.check("https://example.org/", cfg, res({"example.org": [addr]}))


def test_any_bad_address_rejects(cfg):
    with pytest.raises(PolicyReject):
        policy.check("https://example.org/", cfg, res({"example.org": [PUB, "10.0.0.1"]}))


def test_public_ok_and_ipv6_public(cfg):
    r = policy.check("https://example.org/x", cfg, res({"example.org": [PUB, "2606:2800:220:1:248:1893:25c8:1946"]}))
    assert r["port"] == 443 and len(r["ips"]) == 2


def test_dns_failure_and_empty(cfg):
    def boom(h, p):
        raise OSError("nxdomain")
    with pytest.raises(PolicyReject):
        policy.check("https://example.org/", cfg, boom)
    with pytest.raises(PolicyReject):
        policy.check("https://example.org/", cfg, lambda h, p: [])


def test_allow_list(cfg):
    cfg["allowed_host_suffixes"] = ["example.org"]
    policy.check("https://docs.example.org/", cfg, res({}))
    with pytest.raises(PolicyReject):
        policy.check("https://evil.com/", cfg, res({}))


def test_check_redirect_relative_and_private(cfg):
    r = policy.check_redirect("https://example.org/a", "/b", cfg, res({}))
    assert r["url"] == "https://example.org/b"
    with pytest.raises(PolicyReject):
        policy.check_redirect("https://example.org/a", "http://example.org/b", cfg, res({}))
    with pytest.raises(PolicyReject):
        policy.check_redirect("https://example.org/a", None, cfg, res({}))


def test_fetch_ok_writes_quarantine(cfg):
    body = b"%PDF-1.4 hello" * 10
    r = Resp(headers={"content-type": "application/pdf; charset=binary"}, body=body)
    op = opener_for({"https://example.org/f.pdf": r})
    out = fetch.fetch_to_quarantine("https://example.org/f.pdf", cfg, op, res({}))
    assert out["sha256"] == hashlib.sha256(body).hexdigest()
    assert out["bytes_len"] == len(body) and out["content_type"] == "application/pdf"
    assert open(out["path"], "rb").read() == body
    assert stat.S_IMODE(os.stat(out["path"]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(out["path"])).st_mode) == 0o700
    req = op.calls[0]
    assert req["ip"] == PUB and req["headers"]["User-Agent"] == cfg["user_agent"]
    assert r.closed


def test_redirect_followed_each_hop_checked(cfg):
    cfg["max_redirects"] = 3
    t = {"https://a.org/": Resp(302, {"location": "https://b.org/x"}),
         "https://b.org/x": Resp(headers={"content-type": "text/html"}, body=b"<p>hi</p>")}
    out = fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for(t), res({}))
    assert out["url_final"] == "https://b.org/x"


def test_redirect_to_private_ip_blocked(cfg):
    cfg["max_redirects"] = 2
    t = {"https://a.org/": Resp(302, {"location": "https://b.org/"})}
    with pytest.raises(PolicyReject):
        fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for(t), res({"b.org": ["10.0.0.9"]}))


def test_redirect_to_ip_literal_and_http_blocked(cfg):
    cfg["max_redirects"] = 2
    for loc in ("https://169.254.169.254/latest", "http://a.org/"):
        t = {"https://a.org/": Resp(301, {"location": loc})}
        with pytest.raises(PolicyReject):
            fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for(t), res({}))


def test_redirect_loop(cfg):
    cfg["max_redirects"] = 3
    t = {"https://a.org/": Resp(302, {"location": "https://b.org/"}),
         "https://b.org/": Resp(302, {"location": "https://a.org/"})}
    with pytest.raises(fetch.FetchError, match="loop"):
        fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for(t), res({}))


def test_redirect_cap(cfg):
    cfg["max_redirects"] = 2
    class T(dict):
        def __missing__(self, k):
            n = int(k.rsplit("/", 1)[1] or 0)
            return Resp(302, {"location": f"https://a.org/{n + 1}"})
    with pytest.raises(fetch.FetchError, match="redirects"):
        fetch.fetch_to_quarantine("https://a.org/0", cfg, opener_for(T()), res({}))


def test_oversize_streamed_and_cleaned(cfg):
    r = Resp(headers={"content-type": "text/plain"}, body=b"x" * 5000)
    with pytest.raises(fetch.FetchError, match="cap"):
        fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for({"https://a.org/": r}), res({}))
    assert r.reads <= 12
    assert os.listdir(os.path.join(cfg["quarantine_dir"], cfg["raw_subdir"])) == []


def test_declared_length_oversize_not_read(cfg):
    r = Resp(headers={"content-type": "text/plain", "content-length": "99999"}, body=b"x")
    with pytest.raises(fetch.FetchError, match="content-length"):
        fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for({"https://a.org/": r}), res({}))
    assert r.reads == 0


def test_wrong_content_type_and_encoding(cfg):
    for h in ({"content-type": "application/x-msdownload"}, {}, {"content-type": "text/html", "content-encoding": "gzip"}):
        r = Resp(headers=h, body=b"x")
        with pytest.raises(fetch.FetchError):
            fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for({"https://a.org/": r}), res({}))
        assert r.reads == 0


def test_rebinding_second_resolve_differs(cfg):
    answers = iter([[PUB], ["10.0.0.1"]])
    r = Resp(headers={"content-type": "text/plain"}, body=b"x")
    op = opener_for({"https://a.org/": r})
    with pytest.raises(PolicyReject, match="rebinding"):
        fetch.fetch_to_quarantine("https://a.org/", cfg, op, lambda h, p: next(answers))
    assert op.calls == []


def test_non_200_status(cfg):
    with pytest.raises(fetch.FetchError, match="404"):
        fetch.fetch_to_quarantine("https://a.org/", cfg, opener_for({"https://a.org/": Resp(404)}), res({}))


def test_quarantine_under_tmp_refused(cfg):
    cfg["quarantine_dir"] = "/tmp/intake_q"
    with pytest.raises(fetch.FetchError, match="/tmp"):
        fetch.quarantine_dir(cfg)


def test_quarantine_env_override(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("INTAKE_QUARANTINE_DIR", str(tmp_path / "envq"))
    assert fetch.quarantine_dir(cfg) == (tmp_path / "envq").resolve()
