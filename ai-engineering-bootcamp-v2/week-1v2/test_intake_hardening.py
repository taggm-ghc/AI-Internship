"""Regression tests for the independent security review of the intake gateway. Offline only."""
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from intake import decide, detectors, extract, fetch, licence, pipeline, policy
from scripts import fetch_vetted

FX = Path(__file__).parent / "tests_fixtures" / "intake"
PUB = "93.184.216.34"
HOSTILE = "HOSTILE MARKER"


def badge(code, ver="4.0"):
    return (f'<footer><a rel="license" href="https://creativecommons.org/licenses/{code}/{ver}/">x</a></footer>')


def article(extra=""):
    t = (FX / "benign_article.html").read_text(encoding="utf-8")
    return t.replace("</body></html>", extra + "</body></html>").encode()


class Resp:
    def __init__(self, body=b"", status=200, ctype="text/html", headers=None, on_read=None):
        self.status, self._b, self._on_read = status, body, on_read
        self.headers = {"content-type": ctype, **(headers or {})}

    def read(self, n):
        if self._on_read:
            self._on_read()
        out, self._b = self._b[:n], self._b[n:]
        return out

    def close(self):
        pass


@pytest.fixture
def cfg(tmp_path):
    os.environ.pop("INTAKE_QUARANTINE_DIR", None)
    c = pipeline.load_config()
    c["policy"]["quarantine_dir"] = str(tmp_path / "q")
    return c


@pytest.fixture
def pcfg(tmp_path):
    os.environ.pop("INTAKE_QUARANTINE_DIR", None)
    c = policy.load_config()
    c["quarantine_dir"] = str(tmp_path / "q")
    return c


def vet(cfg, resp, url="https://example.org/a", **kw):
    return pipeline.vet(url, opener=lambda req: resp, resolver=lambda h, p: [PUB], cfg=cfg, **kw)


def res(mapping=None, default=(PUB,)):
    return lambda host, port: list((mapping or {}).get(host, default))


# 1 raw file exposure
def test_raw_in_private_subdir_and_never_printed(cfg, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    r = vet(cfg, Resp(article(badge("by"))))
    q = Path(cfg["policy"]["quarantine_dir"]).resolve()
    assert r["verdict"] == "allow" and r["raw_path"] is None
    assert Path(r["vetted_path"]).parent == q / "vetted"
    assert stat.S_IMODE((q / "raw").stat().st_mode) == 0o700
    assert [p.suffix for p in (q / "raw").iterdir()] == [".raw"] and not list((q / "vetted").glob("*.raw"))
    assert not list(q.glob("*.raw"))
    real = pipeline.vet
    monkeypatch.setattr(pipeline, "vet", lambda u, **k: real(
        u, opener=lambda req: Resp(article(badge("by"))), resolver=lambda h, p: [PUB], cfg=cfg, **k))
    assert fetch_vetted.main(["https://example.org/a"]) == 0
    out = capsys.readouterr().out
    assert "vetted_path" in out and "raw" not in out.replace("vetted_path", "").replace(str(q / "vetted"), "")
    assert fetch_vetted.main(["https://example.org/a", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["raw_path"] is None
    assert str(q / "raw") not in caplog.text
    assert fetch_vetted.main(["https://example.org/a", "--admin"]) == 0
    assert str(q / "raw") in capsys.readouterr().out


@pytest.mark.parametrize("body,lic", [("hold", ""), ("reject", "<div hidden>x</div>")])
def test_hold_reject_do_not_expose_raw(cfg, body, lic):
    r = vet(cfg, Resp(article(lic if body == "reject" else "")) if body == "hold" else Resp(b"\x89PNG", ctype="image/png"))
    assert r["verdict"] == body and r["raw_path"] is None and r["vetted_path"] is None


# 2 total deadline
def test_total_deadline_across_reads(pcfg):
    pcfg.update(total_deadline_s=25, chunk_bytes=10, max_bytes=10_000, timeout_s=15)
    now = [0.0]

    def tick():
        now[0] += 10

    r = Resp(b"x" * 1000, ctype="text/plain", on_read=tick)
    with pytest.raises(fetch.FetchError) as ei:
        fetch.fetch_to_quarantine("https://a.org/", pcfg, lambda req: r, res(), clock=lambda: now[0])
    assert ei.value.code == "deadline_exceeded"
    assert os.listdir(Path(pcfg["quarantine_dir"]) / "raw") == []


def test_remaining_time_passed_to_connect_and_deadline_before_connect(pcfg):
    pcfg.update(total_deadline_s=5, timeout_s=15)
    seen = []
    now = [100.0]
    fetch.fetch_to_quarantine("https://a.org/", pcfg, lambda req: seen.append(req["timeout"]) or Resp(b"hi", ctype="text/plain"),
                              res(), clock=lambda: now[0])
    assert seen == [5]
    pcfg["total_deadline_s"] = 0
    with pytest.raises(fetch.FetchError) as ei:
        fetch.fetch_to_quarantine("https://a.org/", pcfg, lambda req: Resp(), res(), clock=lambda: now[0])
    assert ei.value.code == "deadline_exceeded"


def test_deadline_spans_redirects(pcfg):
    pcfg["max_redirects"] = 3
    pcfg["total_deadline_s"] = 5
    now = [0.0]

    def op(req):
        now[0] += 3
        return Resp(status=302, headers={"location": req["url"] + "x"})

    with pytest.raises(fetch.FetchError) as ei:
        fetch.fetch_to_quarantine("https://a.org/", pcfg, op, res(), clock=lambda: now[0])
    assert ei.value.code == "deadline_exceeded"


# 3 attacker text in logs / results
def test_hostile_headers_not_logged_or_returned(cfg, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    cases = [Resp(b"x", ctype=f"text/x\n{HOSTILE}"),
             Resp(b"x", ctype="text/html", headers={"content-encoding": f"gzip {HOSTILE}"})]
    for r in cases:
        out = vet(cfg, r)
        assert out["verdict"] == "reject"
        assert HOSTILE not in json.dumps(out)
        with pytest.raises(fetch.FetchError) as ei:
            fetch.fetch_to_quarantine("https://a.org/", cfg["policy"], lambda req, r=r: r, res())
        assert HOSTILE not in str(ei.value) and HOSTILE not in ei.value.reason
    cfg["policy"]["max_redirects"] = 2
    evil = f"https://{HOSTILE.replace(' ', '-')}.example.net/x"
    out = pipeline.vet("https://example.org/a", cfg=cfg, resolver=res({HOSTILE.replace(" ", "-").lower() + ".example.net": ["10.0.0.5"]}),
                       opener=lambda req: Resp(status=302, headers={"location": evil}))
    assert out["verdict"] == "reject"
    seen = caplog.text + capsys.readouterr().out + capsys.readouterr().err + json.dumps(out)
    assert HOSTILE not in seen and HOSTILE.replace(" ", "-") not in seen and "evil" not in seen.lower()


def test_hostile_exceptions_not_logged(cfg, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(pipeline.detectors, "run", lambda *a: (_ for _ in ()).throw(RuntimeError(HOSTILE)))
    out = vet(cfg, Resp(article(badge("by"))))
    assert out["reasons"] == ["gate_internal_error"]
    assert HOSTILE not in caplog.text and HOSTILE not in json.dumps(out)
    assert "RuntimeError" in caplog.text


def test_hostile_pdf_failure_text_not_surfaced(cfg, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    done = subprocess.CompletedProcess([], 1, stdout=b"", stderr=HOSTILE.encode())
    monkeypatch.setattr(extract.subprocess, "run", lambda *a, **k: done)
    out = vet(cfg, Resp(b"%PDF-1.4", ctype="application/pdf"))
    assert out["reasons"] == ["gate2_extract_reject"] and HOSTILE not in caplog.text + json.dumps(out)
    monkeypatch.setattr(extract.subprocess, "run", mock.Mock(side_effect=OSError(HOSTILE)))
    r = extract.to_visible_text(b"%PDF", "application/pdf", cfg["detectors"], workdir=Path(cfg["policy"]["quarantine_dir"]).expanduser())
    assert r["reject"] == "pdf_worker_unavailable"


def test_decide_reasons_sanitise_spdx():
    c = extract.load_config()
    r = decide.verdict({"flags": [], "scores": {}}, {"status": f"weird {HOSTILE}", "spdx": f"X {HOSTILE}"}, c)
    assert r["verdict"] == "hold" and HOSTILE not in json.dumps(r)


# 4 stale vetted file
def test_stale_vetted_removed_on_reject_hold_and_error(cfg, monkeypatch):
    body = article()
    ok = vet(cfg, Resp(body), declared={"spdx": "CC-BY-4.0"})
    assert ok["verdict"] == "allow" and Path(ok["vetted_path"]).exists()
    held = vet(cfg, Resp(body))
    assert held["verdict"] == "hold" and held["sha256"] == ok["sha256"]
    assert not Path(ok["vetted_path"]).exists()
    vet(cfg, Resp(body), declared={"spdx": "CC-BY-4.0"})
    assert Path(ok["vetted_path"]).exists()
    cfg["licence"]["nd_policy"] = cfg["licence"]["nc_only_policy"] = "reject"
    monkeypatch.setattr(pipeline.envelope, "wrap", lambda *a: 1 / 0)
    err = vet(cfg, Resp(body), declared={"spdx": "CC-BY-4.0"})
    assert err["verdict"] == "reject" and not Path(ok["vetted_path"]).exists()


def test_stale_vetted_removed_on_error_after_sha_known(cfg, monkeypatch):
    body = article()
    ok = vet(cfg, Resp(body), declared={"spdx": "CC-BY-4.0"})
    monkeypatch.setattr(pipeline.detectors, "run", lambda *a: 1 / 0)
    assert vet(cfg, Resp(body))["verdict"] == "reject"
    assert not Path(ok["vetted_path"]).exists()


# 5 PDF limits
def _tiny_pdf(path):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(72, 72)
    with open(path, "wb") as f:
        w.write(f)
    return Path(path).read_bytes()


def test_pdf_real_subprocess_ok_and_garbage_rejects(tmp_path):
    cfg = extract.load_config()
    pdf = _tiny_pdf(tmp_path / "t.pdf")
    wd = tmp_path / "wd"
    wd.mkdir()
    r = extract.to_visible_text(pdf, "application/pdf", cfg, workdir=wd)
    assert r["reject"] is None and list(wd.iterdir()) == []
    assert extract.to_visible_text(b"junk", "application/pdf", cfg, workdir=wd)["reject"] == "pdf_worker_failed"
    assert list(wd.iterdir()) == []
    assert extract.to_visible_text(pdf, "application/pdf", cfg)["reject"] == "pdf_no_workdir"


def test_pdf_hang_times_out_real_subprocess(tmp_path, monkeypatch):
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(extract, "PDF_WORKER", hang)
    cfg = extract.load_config()
    cfg["extract"]["pdf"]["timeout_s"] = 1
    r = extract.to_visible_text(b"%PDF", "application/pdf", cfg, workdir=tmp_path)
    assert r["reject"] == "pdf_worker_timeout" and not list(tmp_path.glob(".pdf-*"))


def test_pdf_crash_and_timeout_mocked_and_tempfile_in_workdir(tmp_path, monkeypatch):
    cfg = extract.load_config()
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, kw=kw, mode=stat.S_IMODE(os.stat(argv[-1]).st_mode))
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(extract.subprocess, "run", fake)
    assert extract.to_visible_text(b"%PDF", "application/pdf", cfg, workdir=tmp_path)["reject"] == "pdf_worker_timeout"
    assert Path(seen["argv"][-1]).parent == tmp_path and seen["mode"] == 0o600
    assert seen["kw"]["timeout"] == cfg["extract"]["pdf"]["timeout_s"] and callable(seen["kw"]["preexec_fn"])
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(extract.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], -9, b"", b""))
    assert extract.to_visible_text(b"%PDF", "application/pdf", cfg, workdir=tmp_path)["reject"] == "pdf_worker_failed"


def test_pdf_rlimits_applied(tmp_path, monkeypatch):
    import resource
    calls = []
    monkeypatch.setattr(extract.resource, "setrlimit", lambda r, v: calls.append((r, v)))
    extract._limiter({"rlimit_as_mb": 7, "rlimit_cpu_s": 3})()
    assert (resource.RLIMIT_AS, (7 << 20, 7 << 20)) in calls and (resource.RLIMIT_CPU, (3, 3)) in calls


def test_pdf_tight_memory_limit_rejects(tmp_path):
    cfg = extract.load_config()
    cfg["extract"]["pdf"]["rlimit_as_mb"] = 16
    pdf = _tiny_pdf(tmp_path / "t.pdf")
    assert extract.to_visible_text(pdf, "application/pdf", cfg, workdir=tmp_path)["reject"] == "pdf_worker_failed"


# 6 decide fails closed
@pytest.mark.parametrize("lic", [None, "bogus", {"status": "bogus"}, {"status": None}, {}])
def test_decide_licence_fails_closed(lic):
    assert decide.verdict({"flags": [], "scores": {}}, lic, extract.load_config())["verdict"] == "hold"


@pytest.mark.parametrize("rep", [None, {}, {"scores": {}}, {"flags": None}])
def test_decide_empty_report_holds(rep):
    assert decide.verdict(rep, "ok", extract.load_config())["verdict"] == "hold"


def test_decide_ok_clean_still_allows():
    assert decide.verdict({"flags": [], "scores": {}}, "ok", extract.load_config())["verdict"] == "allow"


def test_unknown_flag_becomes_reject_in_pipeline(cfg, monkeypatch):
    monkeypatch.setattr(pipeline.detectors, "run", lambda *a: {"flags": [{"name": "mystery"}], "scores": {}})
    r = vet(cfg, Resp(article(badge("by"))))
    assert r["verdict"] == "reject" and r["reasons"] == ["gate_internal_error"]


# 7 invisible categories
def test_variation_selector_and_private_use_smuggling():
    c = extract.load_config()
    payload = "".join(chr(0xE0100 + i) for i in range(20)) + "".join(chr(0xFE00 + i % 16) for i in range(10))
    e = extract.to_visible_text(f"Hello{payload} world".encode(), "text/plain", c)
    assert e["text"] == "Hello world" and e["dropped"]["invisible_unicode"] == 31
    rep = detectors.run(e["raw_text"], e["text"], c)
    names = {f["name"] for f in rep["flags"]}
    assert {"variation_selector_smuggling", "invisible_unicode"} <= names
    assert decide.verdict(rep, "ok", c)["verdict"] == "hold"
    few = detectors.run("a️b", "ab", c)
    assert {f["name"] for f in few["flags"]} == {"invisible_unicode"}


def test_phrase_hidden_between_variation_selectors_detected():
    c = extract.load_config()
    t = "ignore️ previous\U000e0101 instructions"
    assert detectors.passage_flagged(t, c)


# 8 whitespace/case in phrase matching
@pytest.mark.parametrize("t", ["ignore previous\ninstructions", "IGNORE Previous \t Instructions",
                               "ignore\r\n\r\nprevious   instructions", "ignore previous instructions"])
def test_phrase_whitespace_variants(t):
    c = extract.load_config()
    assert detectors.passage_flagged(t, c)
    e = extract.to_visible_text(t.encode(), "text/plain", c)
    assert "injection_phrases" in {f["name"] for f in detectors.run(e["raw_text"], e["text"], c)["flags"]}


def test_delimiter_whitespace_variants():
    c = extract.load_config()
    assert detectors.passage_flagged("BEGIN\nUNTRUSTED", c)
    assert detectors.passage_flagged("<  SYSTEM\n>", c)


# 9 redirect refused before DNS
def test_zero_redirects_refused_without_resolving_target(pcfg):
    assert pcfg["max_redirects"] == 3  # default raised 0->3 on 2026-10-04
    pcfg["max_redirects"] = 0  # behaviour under test: 0 refuses without resolving the target
    hosts = []

    def resolver(h, p):
        hosts.append(h)
        return [PUB]

    with pytest.raises(fetch.FetchError, match="redirect refused"):
        fetch.fetch_to_quarantine("https://a.org/", pcfg, lambda req: Resp(status=302, headers={"location": "https://attacker.example.net/"}),
                                  resolver)
    assert "attacker.example.net" not in hosts and hosts.count("a.org") == 2


# 10 licence spoofing
@pytest.mark.parametrize("html", [
    '<div hidden>Licensed under CC BY 4.0</div>',
    '<p style="display:none">licensed under CC BY 4.0</p>',
    '<p style="color:red; visibility: hidden">licensed under CC BY 4.0</p>',
    '<p aria-hidden="true">licensed under CC BY 4.0</p>',
    '<div hidden><p>licensed under <b>CC BY 4.0</b></p></div>',
    '<!-- licensed under CC BY 4.0 -->',
    '<script>var a="licensed under CC BY 4.0"</script><style>/* licensed under CC BY 4.0 */</style>',
    '<div hidden><a rel="license" href="https://creativecommons.org/licenses/by/4.0/">x</a></div>',
    '<span hidden><meta name="license" content="CC BY 4.0"></span>',
    '<template><p>licensed under CC BY 4.0</p></template>',
])
def test_hidden_licence_claims_ignored(html):
    r = licence.detect(f"<html><body><p>Hello</p>{html}</body></html>", "https://example.org/x", None, licence.load_config())
    assert r["source"] == "none" and r["verdict_hint"] == "hold" and r["spdx"] is None


def test_visible_licence_after_hidden_block_still_found():
    h = '<div hidden>x</div><p>Licensed under CC BY 4.0</p>'
    r = licence.detect(f"<body>{h}</body>", "https://example.org/x", None, licence.load_config())
    assert r["source"] == "page" and r["verdict_hint"] == "ok"


def test_declared_spdx_cli_flag_removed(capsys):
    assert fetch_vetted.main(["https://example.org/", "--declared-spdx", "MIT"]) == 2


def test_licence_source_distinguishes_declared_from_page():
    c = licence.load_config()
    d = licence.detect("nothing", "https://example.org/x", {"spdx": "CC-BY-4.0"}, c)
    assert d["source"] == "declared"
    p = licence.detect("<p>Licensed under CC BY 4.0</p>", "https://example.org/x", None, c)
    assert p["source"] == "page"
    h = licence.detect("<p>Licensed under CC BY 4.0</p>", "https://huggingface.co/x", None, c)
    assert h["source"] == "page-host"


# 11 single NC/ND setting
def test_nc_nd_single_setting(cfg):
    assert cfg["licence"]["nd_policy"] == "reject" and cfg["licence"]["nc_only_policy"] == "ok"
    for code, want in (("by-nc-sa", "allow"), ("by-nc", "allow"), ("by-nc-nd", "reject"), ("by-nd", "reject")):
        got = vet(cfg, Resp(article(badge(code))))["verdict"]
        assert got in ("allow", "label") if want == "allow" else got == want, (code, got)
    cfg["licence"]["nc_only_policy"] = "hold"
    assert vet(cfg, Resp(article(badge("by-nc-sa"))))["verdict"] == "hold"
    assert vet(cfg, Resp(article(badge("by-nc"))))["verdict"] == "hold"
    assert vet(cfg, Resp(article(badge("by-nc-nd"))))["verdict"] == "reject"
    assert vet(cfg, Resp(article(badge("by-nd"))))["verdict"] == "reject"
    cfg["licence"]["nd_policy"] = "hold"
    assert vet(cfg, Resp(article(badge("by-nc-nd"))))["verdict"] == "hold"


def test_no_duplicate_nc_nd_rules_in_other_configs(cfg):
    assert "licence_spdx_reject_substrings" not in cfg["detectors"]["decide"]
    assert not [k for k in cfg["pipeline"] if "nc_status" in k or "nd_status" in k]


def test_bad_policy_value_rejected():
    c = licence.load_config()
    c["nc_only_policy"] = "maybe"
    c.pop("_compiled", None)
    with pytest.raises(licence.LicenceConfigError):
        licence.detect("x", "https://example.org/", None, c)


# _PinnedHTTPSConnection without network
def test_pinned_connection_targets_ip_with_hostname_sni():
    ctx = mock.Mock()
    ctx.wrap_socket.return_value = "tls-sock"
    with mock.patch.object(fetch.socket, "create_connection", return_value="raw-sock") as cc:
        conn = fetch._PinnedHTTPSConnection("example.org", PUB, 443, 7, ctx)
        conn.connect()
    cc.assert_called_once_with((PUB, 443), 7)
    ctx.wrap_socket.assert_called_once_with("raw-sock", server_hostname="example.org")
    assert conn.sock == "tls-sock" and conn.host == "example.org"


def test_default_opener_uses_pinned_ip_and_requested_timeout():
    made = {}

    class C:
        def __init__(self, host, ip, port, timeout, ctx):
            made.update(host=host, ip=ip, port=port, timeout=timeout)

        def request(self, *a, **k):
            raise OSError("boom")

        def close(self):
            made["closed"] = True

    with mock.patch.object(fetch, "_PinnedHTTPSConnection", C), pytest.raises(OSError):
        fetch.default_opener({"host": "example.org", "ip": PUB, "port": 443, "timeout": 4, "path": "/", "headers": {}})
    assert made == {"host": "example.org", "ip": PUB, "port": 443, "timeout": 4, "closed": True}
