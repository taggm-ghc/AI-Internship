"""Gates 0-5 glued together: URL -> quarantine -> vetted envelope. Fail closed.

Results carry fixed reason codes, flag names and numbers only; never page text.
raw_path is None in results unless admin=True (raw files are quarantined in quarantine_dir/raw, 0700).
purpose="verify" (item #96 H-21) writes no vetted file and returns an opaque in-memory VerifyHandle
only for registered callers and only when released (see _release_decision); every decision also
appends one licence-observation record (item #97) and fails closed if that write fails.
Logs never contain raw paths, response headers, redirect targets or exception text.
"""
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path

from . import decide, detectors, envelope, extract, fetch, licence, policy, verify
from .safe import token

log = logging.getLogger("intake.pipeline")
AUDIT_CODE = "gate_audit_error"  # fallback when the verify config (which names it) cannot be loaded
PIPELINE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "intake_pipeline.json"


def load_config():
    with open(PIPELINE_CONFIG_PATH, encoding="utf-8") as f:
        pipe = json.load(f)
    return {"pipeline": pipe, "policy": policy.load_config(), "detectors": extract.load_config(),
            "licence": licence.load_config(), "verify": _verify_config_or_none()}


def _verify_config_or_none():
    """A broken verify config must not raise out of load_config (ingest must not depend on it);
    vet() re-loads it inside its guarded section and fails closed."""
    try:
        return verify.load_config()
    except verify.VerifyError:
        return None


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


def _release_decision(v, lrep, report, cfg, pipe):
    """(released, reason_code) for purpose=verify. Gates ran unchanged; this only decides whether the
    in-memory text may be handed to a registered verifier."""
    if v["verdict"] == "reject":
        return False, "verdict_reject"
    if lrep is None:
        return False, "no_licence_report"
    if lrep.get("nd"):
        return False, "nd"
    if lrep.get("restrictions"):
        return False, "restrictions"
    licence_ok = v["verdict"] in ("allow", "label")
    if not licence_ok:
        if lrep.get("hold_kind") not in ("none", "conflict", "host"):
            return False, "licence_hold_not_unknown"
        if _licence_status(lrep, pipe) != "unknown":
            return False, "licence_status_not_unknown"
    # both branches: only a clean page releases. A label (any detector flag) never does.
    base = decide.verdict(report, "ok", cfg["detectors"])
    if base["verdict"] != "allow":
        return False, "detector_" + base["verdict"]
    if report["flags"]:
        return False, "detector_flags"
    return True, "licence_ok" if licence_ok else "licence_unknown_hold"


def vet(url, opener=None, resolver=None, cfg=None, declared=None, admin=False, purpose="ingest", caller=None):
    """declared: licence declared by the original source ({spdx|name|url, from_host?});
    for trusted calling code only, never from a CLI or page.
    purpose: "ingest" (default, writes the vetted file) or "verify" (writes nothing; result["verify_handle"]
    is a VerifyHandle only when released; needs a registered `caller`, checked against the calling file)."""
    cfg = cfg or load_config()
    pipe, codes = cfg["pipeline"], cfg["pipeline"]["reason_codes"]
    vcfg = None  # loaded inside the guarded section below; a broken verify config fails closed
    if purpose not in ("ingest", "verify"):
        raise ValueError(f"vet purpose must be 'ingest' or 'verify', got {purpose!r}")
    is_verify = purpose == "verify"
    caller_frame = sys._getframe(1) if is_verify else None
    sha = raw_path = qdir = None
    stage = "internal"
    full_usha = hashlib.sha256(str(url).encode("utf-8", "replace")).hexdigest()
    usha = full_usha[:12]
    st = {"lrep": None, "flags": [], "base": None, "released": False, "why": "not_evaluated", "handle": None,
          "release_id": hashlib.sha256(os.urandom(16)).hexdigest()[:16], "caller_sha": None}
    cver = ""

    def out(verdict, reasons, *a):
        handle = st["handle"]
        res = _result(verdict, reasons, sha, raw_path if admin else None, *a)
        if is_verify:
            res["vetted_path"] = None
        try:
            if qdir is None or vcfg is None:
                raise verify.AuditError("no quarantine dir or verify config for audit")
            if is_verify:
                verify.write_release_audit(qdir, vcfg, {
                    "ts": verify._now(), "event": "decision", "release_id": st["release_id"], "caller": caller,
                    "caller_sha": st["caller_sha"], "purpose": purpose, "url_sha256": full_usha,
                    "host": verify.host_of(url), "raw_sha256": sha, "licence": _audit_licence(st["lrep"]),
                    "base_verdict": st["base"], "verdict": verdict,
                    "release": "released" if handle is not None else "denied", "reason_code": st["why"],
                    "flags": st["flags"], "span_sha256": [], "span_words": [], "config_hash": cver})
            obs = verify.build_observation(
                purpose=purpose, url=url, usha=full_usha, verdict=verdict, reasons=reasons, lrep=st["lrep"],
                vcfg=vcfg, cfg_version=cver,
                reason_code=st["why"] if st["why"] != "not_evaluated" else verify.fixed_code(reasons[0] if reasons else ""))
            verify.write_observation(qdir, vcfg, obs)
            verify.emit_observation_db(obs, vcfg)  # after the fail-closed JSONL write; fail-open, never changes the verdict
        except Exception as e:  # any audit/observation problem fails closed, never an uncaught exception
            log.error("intake audit work failed closed url_sha=%s: %s", usha, token(type(e).__name__))
            if handle is not None:
                handle.close()
                handle = None
            verdict, reasons = "reject", [AUDIT_CODE]
            res = _result(verdict, reasons, sha, raw_path if admin else None)
        if not is_verify and qdir is not None and verdict not in pipe["write_vetted_for"]:
            _drop_vetted(qdir, pipe, sha)
        if is_verify:
            res["verify_handle"] = handle
        return res

    def deny(code):  # only reachable once vcfg is loaded
        st["why"] = code
        return out("reject", [vcfg["reason_codes"]["caller_denied"]])

    try:
        stage = "policy"
        vcfg = cfg.get("verify") or verify.load_config()
        cver = verify.config_hash(cfg)
        qdir = fetch.quarantine_dir(cfg["policy"])
        if is_verify:
            ok, why = verify.check_caller(caller_frame, caller, vcfg)
            if not ok:
                log.warning("intake verify caller denied url_sha=%s: %s", usha, why)
                return deny(why)
            st["caller_sha"] = vcfg["callers"][caller]["sha256"]
        fetched = fetch.fetch_to_quarantine(url, cfg["policy"], opener=opener, resolver=resolver)
        sha, raw_path = fetched["sha256"], fetched["path"]
        stage = "extract"
        raw = Path(raw_path).read_bytes()
        ex = extract.to_visible_text(raw, fetched["content_type"], cfg["detectors"], workdir=qdir)
        if ex["reject"]:
            log.warning("intake extract reject sha=%s: %s", sha, token(ex["reject"]))
            st["why"] = "extract_reject"
            return out("reject", [codes["extract"]])
        stage = "internal"
        report = detectors.run(ex["raw_text"], ex["text"], cfg["detectors"])
        st["flags"] = sorted({f["name"] for f in report["flags"]})
        ecfg = cfg["detectors"].get("extract", cfg["detectors"])
        is_html = fetched["content_type"] in ecfg["html_types"]
        lic_input = raw.decode("utf-8", errors="replace") if is_html else ex["text"]
        lrep = licence.detect(lic_input, fetched["url_final"], declared, cfg["licence"])
        st["lrep"] = lrep
        lic = _licence_summary(lrep, pipe)
        v = decide.verdict(report, {"status": _licence_status(lrep, pipe), "spdx": lrep.get("spdx") or ""},
                           cfg["detectors"])
        summary = {"flags": sorted({f["name"] for f in report["flags"]}), "scores": report["scores"],
                   "dropped": {k: n for k, n in ex["dropped"].items() if isinstance(n, int)}}
        log.info("intake sha=%s verdict=%s reasons=%s", sha, v["verdict"], v["reasons"])
        vetted = None
        if is_verify:
            st["base"] = decide.verdict(report, "ok", cfg["detectors"])["verdict"]
            released, why = _release_decision(v, lrep, report, cfg, pipe)
            if released and not verify.reserve_release(vcfg):
                released, why = False, "release_cap_exceeded"
                log.error("intake verify release cap %s reached; further releases denied this process",
                          vcfg["verify_max_releases"])
            st["why"] = why
            if released:
                rid = st["release_id"]

                def sink(span_hashes, span_words, calls):
                    verify.write_release_audit(qdir, vcfg, {
                        "ts": verify._now(), "event": "close", "release_id": rid, "caller": caller,
                        "caller_sha": st["caller_sha"], "purpose": purpose, "url_sha256": full_usha,
                        "span_sha256": span_hashes, "span_words": span_words, "calls": calls,
                        "config_hash": cver})
                st["handle"] = verify.VerifyHandle(ex["text"], vcfg, rid, sink)
        elif v["verdict"] in pipe["write_vetted_for"]:
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
        st["why"] = "gate0_policy_reject"
        return out("reject", [codes["policy"]])
    except fetch.FetchError as e:
        log.warning("intake gate1 fetch error url_sha=%s: %s", usha, token(e.code))
        st["why"] = "gate1_fetch_error"
        return out("reject", [codes["fetch"]])
    except Exception as e:
        log.error("intake %s stage failed closed url_sha=%s: %s", stage, usha, type(e).__name__)
        if st["handle"] is not None:
            st["handle"].close()
            st["handle"] = None
        st["why"] = "internal_error"
        return out("reject", [codes["extract"] if stage == "extract" else codes["internal"]])


def _audit_licence(lrep):
    if not lrep:
        return None
    return {"spdx": lrep.get("spdx"), "family": lrep.get("family"), "nc": lrep.get("nc"), "nd": lrep.get("nd"),
            "confidence": lrep.get("confidence"), "source": lrep.get("source"),
            "hold_kind": lrep.get("hold_kind")}
