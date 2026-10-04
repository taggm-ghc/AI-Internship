"""Gates 0-5 glued together: URL -> quarantine -> vetted envelope. Fail closed.

Results carry fixed reason codes, flag names and numbers only; never page text.
raw_path is None in results unless admin=True (raw files are quarantined in quarantine_dir/raw, 0700).
Logs never contain raw paths, response headers, redirect targets or exception text.
"""
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from . import decide, detectors, envelope, extract, fetch, licence, policy
from .safe import token

log = logging.getLogger("intake.pipeline")
PIPELINE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "intake_pipeline.json"


def load_config():
    with open(PIPELINE_CONFIG_PATH, encoding="utf-8") as f:
        pipe = json.load(f)
    return {"pipeline": pipe, "policy": policy.load_config(), "detectors": extract.load_config(),
            "licence": licence.load_config()}


def _result(verdict, reasons, sha=None, raw_path=None, vetted=None, lic=None, summary=None):
    return {"verdict": verdict, "reasons": list(reasons), "sha256": sha, "raw_path": raw_path,
            "vetted_path": vetted, "licence": lic, "report_summary": summary}


def _licence_summary(rep, pipe):
    spdx = rep.get("spdx")
    if spdx and not re.match(pipe["spdx_safe_regex"], str(spdx)):
        spdx = "unrecognised"
    keys = ("nc", "nd", "verdict_hint", "ingest_ok", "read_ok", "source", "confidence")
    return {"spdx": spdx, **{k: rep.get(k) for k in keys}}


def _licence_status(rep, pipe):
    hint = rep["verdict_hint"]
    try:
        return pipe["licence_hint_to_status"][hint]
    except KeyError:
        raise ValueError(f"licence verdict_hint {hint!r} has no mapping in intake_pipeline.json")


def _vetted_dir(qdir, pipe):
    d = qdir / pipe["vetted_subdir"]
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _drop_vetted(qdir, pipe, sha):
    if sha:
        try:
            (qdir / pipe["vetted_subdir"] / f"{sha}{pipe['vetted_suffix']}").unlink()
        except FileNotFoundError:
            pass


def vet(url, opener=None, resolver=None, cfg=None, declared=None, admin=False):
    """declared: licence declared by the original source ({spdx|name|url, from_host?});
    for trusted calling code only, never from a CLI or page."""
    cfg = cfg or load_config()
    pipe, codes = cfg["pipeline"], cfg["pipeline"]["reason_codes"]
    sha = raw_path = qdir = None
    stage = "internal"
    usha = hashlib.sha256(str(url).encode("utf-8", "replace")).hexdigest()[:12]

    def out(verdict, reasons, *a):
        if verdict not in pipe["write_vetted_for"]:
            _drop_vetted(qdir, pipe, sha)
        return _result(verdict, reasons, sha, raw_path if admin else None, *a)

    try:
        stage = "policy"
        qdir = fetch.quarantine_dir(cfg["policy"])
        fetched = fetch.fetch_to_quarantine(url, cfg["policy"], opener=opener, resolver=resolver)
        sha, raw_path = fetched["sha256"], fetched["path"]
        stage = "extract"
        raw = Path(raw_path).read_bytes()
        ex = extract.to_visible_text(raw, fetched["content_type"], cfg["detectors"], workdir=qdir)
        if ex["reject"]:
            log.warning("intake extract reject sha=%s: %s", sha, token(ex["reject"]))
            return out("reject", [codes["extract"]])
        stage = "internal"
        report = detectors.run(ex["raw_text"], ex["text"], cfg["detectors"])
        ecfg = cfg["detectors"].get("extract", cfg["detectors"])
        is_html = fetched["content_type"] in ecfg["html_types"]
        lic_input = raw.decode("utf-8", errors="replace") if is_html else ex["text"]
        lrep = licence.detect(lic_input, fetched["url_final"], declared, cfg["licence"])
        lic = _licence_summary(lrep, pipe)
        v = decide.verdict(report, {"status": _licence_status(lrep, pipe), "spdx": lrep.get("spdx") or ""},
                           cfg["detectors"])
        summary = {"flags": sorted({f["name"] for f in report["flags"]}), "scores": report["scores"],
                   "dropped": {k: n for k, n in ex["dropped"].items() if isinstance(n, int)}}
        log.info("intake sha=%s verdict=%s reasons=%s", sha, v["verdict"], v["reasons"])
        vetted = None
        if v["verdict"] in pipe["write_vetted_for"]:
            meta = {"url": fetched["url_final"], "sha256": sha, "verdict": v["verdict"]}
            body = envelope.wrap(ex["text"], report, meta, cfg["detectors"])
            vp = _vetted_dir(qdir, pipe) / f"{sha}{pipe['vetted_suffix']}"
            fd = os.open(vp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            vetted = str(vp)
        return out(v["verdict"], v["reasons"], vetted, lic, summary)
    except policy.PolicyReject as e:
        log.warning("intake gate0 reject url_sha=%s: %s", usha, token(e.reason))
        return out("reject", [codes["policy"]])
    except fetch.FetchError as e:
        log.warning("intake gate1 fetch error url_sha=%s: %s", usha, token(e.code))
        return out("reject", [codes["fetch"]])
    except Exception as e:
        log.error("intake %s stage failed closed url_sha=%s: %s", stage, usha, type(e).__name__)
        return out("reject", [codes["extract"] if stage == "extract" else codes["internal"]])
