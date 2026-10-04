"""End-to-end intake tests: mock resolver/opener, local fixtures, no network.

V-b: Python 3.12.3 at authoring time; ipaddress.is_global/is_private differ for the
shared address space 100.64.0.0/10 (is_private False, is_global False), and the
semantics changed in recent releases, so test_shared_space_blocked_by_is_global
pins the behaviour on whatever interpreter runs it.
"""
import ipaddress
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from intake import pipeline, policy
from scripts import fetch_vetted

FX = Path(__file__).parent / "tests_fixtures" / "intake"
PUB = "93.184.216.34"
CC = '<footer><a rel="license" href="https://creativecommons.org/licenses/by/4.0/">CC BY</a></footer>'
NC = '<footer><a rel="license" href="https://creativecommons.org/licenses/by-nc-nd/4.0/">CC BY-NC-ND</a></footer>'


class Resp:
    def __init__(self, body=b"", status=200, ctype="text/html", headers=None):
        self.status, self._b = status, body
        self.headers = {"content-type": ctype, **(headers or {})}

    def read(self, n):
        out, self._b = self._b[:n], self._b[n:]
        return out

    def close(self):
        pass


def page(name, extra=""):
    t = (FX / name).read_text(encoding="utf-8")
    return (t.replace("</body></html>", extra + "</body></html>") if extra else t).encode()


def opener_for(resp):
    return lambda req: resp


@pytest.fixture
def cfg(tmp_path):
    os.environ.pop("INTAKE_QUARANTINE_DIR", None)
    c = pipeline.load_config()
    c["policy"]["quarantine_dir"] = str(tmp_path / "q")
    return c


def vet(cfg, resp, url="https://example.org/a", **kw):
    return pipeline.vet(url, opener=opener_for(resp), resolver=lambda h, p: [PUB], cfg=cfg, **kw)


def test_benign_allow(cfg):
    r = vet(cfg, Resp(page("benign_article.html", CC)))
    assert r["verdict"] == "allow", r
    assert Path(r["vetted_path"]).read_text().startswith("UNTRUSTED EXTERNAL DATA")
    assert r["raw_path"] is None and r["licence"]["ingest_ok"] is True


def test_hostile_hidden_payload_rejects_without_vetted_file(cfg):
    r = vet(cfg, Resp(page("hidden_text.html", CC)))
    assert r["verdict"] == "reject" and r["vetted_path"] is None
    assert not list(Path(cfg["policy"]["quarantine_dir"]).glob("*.vetted.md"))
    blob = json.dumps(r).lower()
    assert "ignore" not in blob and "instruction" not in blob.replace("instruction_", "")


def test_injection_article_labelled(cfg):
    r = vet(cfg, Resp(page("security_article.html", CC)))
    assert r["verdict"] == "label", r
    assert "non_actionable" in Path(r["vetted_path"]).read_text()


def test_nc_nd_flags(cfg):
    r = vet(cfg, Resp(page("benign_article.html", NC)))
    assert r["verdict"] == "reject" and r["vetted_path"] is None
    assert r["licence"]["read_ok"] is True and r["licence"]["ingest_ok"] is False
    assert r["licence"]["nc"] and r["licence"]["nd"]


def test_hold_on_undeclared_licence(cfg):
    r = vet(cfg, Resp(page("benign_article.html")))
    assert r["verdict"] == "hold" and r["vetted_path"] is None
    assert r["licence"]["ingest_ok"] is False


def test_declared_licence_allows(cfg):
    r = vet(cfg, Resp(page("benign_article.html")), declared={"spdx": "CC-BY-4.0"})
    assert r["verdict"] == "allow", r


def test_redirect_off_by_default(cfg):
    assert cfg["policy"]["max_redirects"] == 3  # default raised 0->3 on 2026-10-04; each hop re-validated
    cfg["policy"]["max_redirects"] = 0  # behaviour under test: 0 refuses redirects
    r = vet(cfg, Resp(status=302, headers={"location": "https://example.org/b"}))
    assert r["verdict"] == "reject" and r["reasons"] == ["gate1_fetch_error"]


def test_redirect_to_private_ip_rejects(cfg):
    cfg["policy"]["max_redirects"] = 2
    resolver = lambda h, p: ["10.0.0.5"] if h == "evil.example.net" else [PUB]
    r = pipeline.vet("https://example.org/a", cfg=cfg, resolver=resolver,
                     opener=opener_for(Resp(status=302, headers={"location": "https://evil.example.net/x"})))
    assert r["verdict"] == "reject" and r["reasons"] == ["gate0_policy_reject"]
    assert r["vetted_path"] is None


def test_oversize_rejects(cfg):
    cfg["policy"]["max_bytes"] = 100
    r = vet(cfg, Resp(page("benign_article.html", CC)))
    assert r["verdict"] == "reject" and r["reasons"] == ["gate1_fetch_error"]


def test_bad_content_type_rejects(cfg):
    r = vet(cfg, Resp(b"\x89PNG", ctype="image/png"))
    assert r["verdict"] == "reject" and r["sha256"] is None


def test_gate_exception_fails_closed(cfg, monkeypatch):
    monkeypatch.setattr(pipeline.detectors, "run", lambda *a: 1 / 0)
    r = vet(cfg, Resp(page("benign_article.html", CC)))
    assert r["verdict"] == "reject" and r["reasons"] == ["gate_internal_error"] and r["vetted_path"] is None


def test_shared_space_blocked_by_is_global(cfg):
    pol = dict(cfg["policy"], extra_blocked_networks=[])
    ip = ipaddress.ip_address("100.64.0.1")
    assert not ip.is_global
    with pytest.raises(policy.PolicyReject):
        policy.check("https://example.org/", pol, lambda h, p: ["100.64.0.1"])
    with pytest.raises(policy.PolicyReject):
        policy.check("https://example.org/", pol, lambda h, p: ["100.127.255.254"])


def test_cli_exit_codes(cfg, monkeypatch, capsys):
    for verdict, code in (("allow", 0), ("label", 0), ("hold", 3), ("reject", 4)):
        monkeypatch.setattr(pipeline, "vet", lambda u, **k: pipeline._result(verdict, ["x"], "s", "/r", None))
        assert fetch_vetted.main(["https://example.org/", "--json"]) == code
    assert json.loads(capsys.readouterr().out.split("\n}\n")[0] + "\n}")["verdict"] == "allow"
    assert fetch_vetted.main([]) == 2
