"""Intake gate 4: licence detection from page text/HTML and declared metadata (item #66).

Pure and offline. Patterns, mappings and verdicts live in config/intake_licence.json.
"""
import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "intake_licence.json"
VERDICT_RANK = {"ok": 0, "hold": 1, "reject": 2}
REQUIRED_KEYS = ("confidence", "verdicts", "cue_window", "cue_tail", "cues", "meta_names", "link_rels",
                 "link_itemprops", "hidden", "restrictions", "restriction_strip_regex", "nd_policy", "nc_only_policy", "jsonld_license_regex", "spdx_tag_regex", "host_domains",
                 "registry_url_patterns", "cc_versions", "cc_url_regex", "cc_url_template",
                 "cc_version_after_regex", "cc_codes", "cc_names", "alias_prefix", "alias_suffix",
                 "spdx_url_template", "spdx", "other")


class LicenceConfigError(ValueError):
    pass


def load_config(path=None):
    path = Path(path) if path else DEFAULT_CONFIG
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LicenceConfigError(f"cannot load licence config {path}: {e}") from e
    _validate(cfg, path)
    return cfg


def _validate(cfg, path="<cfg>"):
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise LicenceConfigError(f"licence config {path} missing keys: {missing}")
    for k, v in cfg["verdicts"].items():
        if v not in VERDICT_RANK:
            raise LicenceConfigError(f"licence config verdicts.{k}={v!r} not in {sorted(VERDICT_RANK)}")
    for k in ("nd_policy", "nc_only_policy"):
        if cfg[k] not in VERDICT_RANK:
            raise LicenceConfigError(f"licence config {k}={cfg[k]!r} not in {sorted(VERDICT_RANK)}")
    for entry in cfg["cc_names"]:
        if entry["code"] not in cfg["cc_codes"]:
            raise LicenceConfigError(f"cc_names code {entry['code']!r} not defined in cc_codes")
    for t in ("url", "cued", "structured", "declared", "host"):
        if t not in cfg["confidence"]:
            raise LicenceConfigError(f"licence config confidence.{t} missing")


def _compile(cfg):
    I = re.I
    try:
        spdx = []
        for key, e in cfg["spdx"].items():
            rx = "|".join(e["aliases"])
            spdx.append((key, e, re.compile(f"{cfg['alias_prefix']}(?:{rx}){cfg['alias_suffix']}", I)))
        return {
            "cues": [re.compile(f"(?:{c})\\s*{cfg['cue_tail']}$", I) for c in cfg["cues"]],
            "cc_url": re.compile(cfg["cc_url_regex"], I),
            "cc_names": [(e["code"], [re.compile(r, I) for r in e["regexes"]]) for e in cfg["cc_names"]],
            "ver_after": re.compile(cfg["cc_version_after_regex"], I),
            "spdx": spdx,
            "other": [(e, re.compile(e["regex"], I)) for e in cfg["other"]],
            "registry": [(e, re.compile(e["url_regex"], I)) for e in cfg["registry_url_patterns"]],
            "jsonld": re.compile(cfg["jsonld_license_regex"], I),
            "spdx_tag": re.compile(cfg["spdx_tag_regex"], I),
            "strip": re.compile(cfg["restriction_strip_regex"]),
            "restrictions": [(e, [re.compile(r, I) for r in e["reject_regexes"]],
                              [re.compile(r, I) for r in e["hold_regexes"]],
                              [re.compile(r, I) for r in e["permit_regexes"]]) for e in cfg["restrictions"]],
            "hosts": tuple(h.lower() for h in cfg["host_domains"]),
            "meta": {m.lower() for m in cfg["meta_names"]},
            "rels": {r.lower() for r in cfg["link_rels"]},
            "itemprops": {r.lower() for r in cfg["link_itemprops"]},
            "hidden": {"attrs": {a.lower() for a in cfg["hidden"]["attrs"]},
                       "attr_values": cfg["hidden"]["attr_values"],
                       "tags": set(cfg["hidden"]["tags"]),
                       "style": [re.compile(r, I) for r in cfg["hidden"]["style_regexes"]],
                       "void": set(cfg["hidden"]["void_tags"])},
        }
    except (re.error, KeyError) as e:
        raise LicenceConfigError(f"licence config pattern error: {e!r}") from e


def _c(cfg):
    if "_compiled" not in cfg:
        _validate(cfg)
        cfg["_compiled"] = _compile(cfg)
    return cfg["_compiled"]


class _Page(HTMLParser):
    """Collects licence signals from visible markup only: hidden elements, comments,
    script/style (except ld+json outside hidden elements) are ignored."""

    def __init__(self, c):
        super().__init__(convert_charrefs=True)
        self.c = c
        self.text, self.structured, self.hrefs, self.ld = [], [], [], []
        self._skip = 0
        self._ld = False
        self._buf = None
        self._stack = []  # [tag, hidden]

    def _is_hidden(self, tag, a):
        h = self.c["hidden"]
        if h["attrs"] & set(a) or tag in h["tags"]:
            return True
        if any((a.get(s["attr"]) or "").strip().lower() in s["values"] for s in h["attr_values"]):
            return True
        style = a.get("style", "")
        return any(rx.search(style) for rx in h["style"])

    def _under_hidden(self):
        return any(h for _, h in self._stack)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        void = tag in self.c["hidden"]["void"]
        hidden = self._under_hidden() or self._is_hidden(tag, a)
        if not void:
            self._stack.append([tag, hidden])
        if hidden:
            return
        rels = set(a.get("rel", "").lower().split())
        marked = bool(rels & self.c["rels"]) or a.get("itemprop", "").lower() in self.c["itemprops"]
        if tag in ("script", "style"):
            self._skip += 1
            self._ld = tag == "script" and "ld+json" in a.get("type", "").lower()
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            if key in self.c["meta"] and a.get("content"):
                self.structured.append(a["content"])
        elif tag in ("a", "link") and a.get("href"):
            self.hrefs.append(a["href"])
            if marked:
                if tag == "a":
                    self._buf = [a["href"]]
                else:
                    self.structured.append(a["href"])
        elif tag == "img" and self._buf is not None:
            self._buf.append(a.get("alt", ""))

    def handle_endtag(self, tag):
        hidden = False
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                hidden = self._stack[i][1] or self._under_hidden()
                del self._stack[i:]
                break
        if hidden:
            return
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            self._ld = False
        elif tag == "a" and self._buf is not None:
            self.structured.append(" ".join(self._buf))
            self._buf = None

    def handle_data(self, data):
        if self._under_hidden():
            return
        if self._ld:
            self.ld.append(data)
        elif not self._skip:
            self.text.append(data)
            if self._buf is not None:
                self._buf.append(data)


def _cand(family, spdx, name, url, nc, nd, verdict, conf, source, via, reason=None):
    return {"family": family, "spdx": spdx, "name": name, "url": url, "nc": nc, "nd": nd,
            "verdict": verdict, "confidence": conf, "source": source, "via": via, "reason": reason}


def _cc_cand(cfg, code, version, conf, source, via, url=None):
    e = cfg["cc_codes"][code]
    if version and version not in cfg["cc_versions"]:
        version = None
    spdx = e["id"] + (f"-{version}" if version else "")
    url = url or cfg["cc_url_template"].format(kind=e["kind"], code=code, version=f"{version}/" if version else "")
    name = e["name"] + (f" {version}" if version else "")
    return _cand(e["id"], spdx, name, url, e["nc"], e["nd"], e["verdict"], conf, source, via)


def _scan(text, cfg, structured, ignored):
    c = _c(cfg)
    conf = cfg["confidence"]
    out, claimed = [], []

    def free(s, e):
        return not any(s < ce and cs < e for cs, ce in claimed)

    def cued(s):
        if structured:
            return True
        window = text[max(0, s - cfg["cue_window"]):s]
        return any(p.search(window) for p in c["cues"])

    def tier(kind):
        return conf["structured"] if structured else conf[kind]

    for m in c["cc_url"].finditer(text):
        code = m.group("code").lower()
        e = cfg["cc_codes"].get(code)
        if not e or e["kind"] != m.group("kind").lower() or not free(*m.span()):
            continue
        claimed.append(m.span())
        out.append(_cc_cand(cfg, code, m.group("version"), tier("url"), "page", "cc-url",
                            url="https://" + m.group(0).rstrip("/")))
    for e, rx in c["other"]:
        for m in rx.finditer(text):
            if not free(*m.span()):
                continue
            if e["needs_cue"] and not cued(m.start()):
                ignored.append(m.group(0))
                continue
            claimed.append(m.span())
            ver = m.groupdict().get("version")
            spdx = e["spdx"] and e["spdx"] + (f"-{ver}" if ver else "")
            out.append(_cand(e["id"], spdx, e["name"], "https://" + m.group(0) if "." in m.group(0) and "/" in m.group(0) else None,
                             e["nc"], e["nd"], e["verdict"], tier(e["tier"]), e["source"], e["id"], e.get("reason")))
    for code, rxs in c["cc_names"]:
        for rx in rxs:
            for m in rx.finditer(text):
                if not free(*m.span()):
                    continue
                claimed.append(m.span())
                if not cued(m.start()):
                    ignored.append(m.group(0))
                    continue
                v = c["ver_after"].match(text[m.end():m.end() + 24])
                out.append(_cc_cand(cfg, code, v.group(1) if v else None, tier("cued"), "page", "cc-name"))
    for key, e, rx in c["spdx"]:
        for m in rx.finditer(text):
            if not free(*m.span()):
                continue
            claimed.append(m.span())
            if not cued(m.start()):
                ignored.append(m.group(0))
                continue
            out.append(_cand(key, key, e["name"], cfg["spdx_url_template"].format(spdx=key), False, False,
                             e["verdict"], tier("cued"), "page", "spdx", e.get("reason")))
    return out


def _page_candidates(raw, cfg, ignored, texts=None):
    c = _c(cfg)
    p = _Page(c)
    try:
        p.feed(raw)
        p.close()
    except Exception as e:
        raise ValueError(f"licence detection: cannot parse input as HTML/text: {e!r}") from e
    body = " ".join(p.text)
    frags = list(p.structured)
    frags += [m.group(1) for m in c["jsonld"].finditer(" ".join(p.ld))]
    frags += [m.group(1) for m in c["spdx_tag"].finditer(body)]
    if texts is not None:
        texts += [body] + frags
    cands = []
    for f in frags:
        cands += _scan(f, cfg, True, ignored)
    cands += _scan(body, cfg, False, ignored)
    cands += _scan("\n".join(p.hrefs), cfg, False, ignored)
    return cands


def _restrictions(texts, cfg):
    """Config-driven use-restriction detectors over visible text only (hidden markup is already
    dropped). Returns [{id, name, verdict, reason}]: reject only on a clear match with no
    contradicting permission phrase; ambiguous or contradicted matches give hold."""
    c = _c(cfg)
    text = unicodedata.normalize("NFKC", c["strip"].sub("", "\n".join(texts)))
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    hits = []
    for e, rej, hold, permit in c["restrictions"]:
        rejected = any(rx.search(text) for rx in rej)
        held = any(rx.search(text) for rx in hold)
        if not (rejected or held):
            continue
        permitted = any(rx.search(text) for rx in permit)
        if rejected and not permitted:
            hits.append({"id": e["id"], "name": e["name"], "verdict": "reject", "reason": e["reason"]})
        else:
            why = "conflicting permission and restriction phrasing" if rejected else "ambiguous restriction phrasing"
            hits.append({"id": e["id"], "name": e["name"], "verdict": "hold", "reason": f"{e['name']}: {why}"})
    return hits


def _is_host(url, cfg):
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _c(cfg)["hosts"])


def _result(spdx=None, name=None, url=None, source="none", confidence=0.0, nc=False, nd=False,
            verdict="hold", reasons=(), read_ok=False, candidates=(), family=None, hold_kind=None):
    return {"spdx": spdx, "name": name, "url": url, "source": source, "confidence": confidence,
            "nc": nc, "nd": nd, "verdict_hint": verdict, "ingest_ok": verdict == "ok",
            "read_ok": read_ok, "reasons": list(reasons), "candidates": list(candidates), "restrictions": [],
            "family": family, "hold_kind": hold_kind}


def detect(raw_text_or_html, url, declared=None, cfg=None):
    """Licence report: _detect_core plus the use-restriction overlay (no-educational-use,
    strictly-for-fee). verdict_hint is ok|hold|reject; restrictions lists the hits."""
    if not isinstance(raw_text_or_html, str):
        raise TypeError(f"licence.detect needs str text/html, got {type(raw_text_or_html).__name__}")
    cfg = cfg if cfg is not None else load_config()
    texts = []
    r = _detect_core(raw_text_or_html, url, declared, cfg, texts)
    if declared:
        texts += [str(declared[k]) for k in ("spdx", "name", "url") if declared.get(k)]
    hits = _restrictions(texts, cfg)
    r["restrictions"] = hits
    for h in hits:
        r["reasons"].append(h["reason"])
        if VERDICT_RANK[h["verdict"]] > VERDICT_RANK[r["verdict_hint"]]:
            r["verdict_hint"] = h["verdict"]
        if h["verdict"] == "reject":
            r["read_ok"] = False
    r["ingest_ok"] = r["verdict_hint"] == "ok"
    return r


def _detect_core(raw_text_or_html, url, declared, cfg, texts):
    """Return a licence report dict; verdict_hint is ok|hold|reject (never allow/label).
    source: page | page-host | registry-pattern (found in the fetched content) or declared
    (supplied by trusted caller code via `declared`), none."""
    if not isinstance(raw_text_or_html, str):
        raise TypeError(f"licence.detect needs str text/html, got {type(raw_text_or_html).__name__}")
    c = _c(cfg)
    conf, verdicts = cfg["confidence"], cfg["verdicts"]
    ignored, reasons = [], []
    cands = _page_candidates(raw_text_or_html, cfg, ignored, texts)
    for e, rx in c["registry"]:
        if rx.search(url or ""):
            cands.append(_cand(e["id"], e.get("spdx"), e["name"], url, e.get("nc", False), e.get("nd", False),
                               e["verdict"], conf["url"], "registry-pattern", e["id"], e.get("reason")))
    host = _is_host(url, cfg)
    if host:
        for k in cands:
            k["source"], k["confidence"] = "page-host", conf["host"]
        if cands:
            reasons.append("licence claim comes from a hosting/aggregator site, not the original source")
    decl_host = False
    if declared:
        decl_host = bool(declared.get("from_host"))
        dtext = "\n".join(str(declared[k]) for k in ("spdx", "name", "url") if declared.get(k))
        dc = _scan(dtext, cfg, True, ignored)
        if not dc and dtext:
            reasons.append(f"declared licence not recognised: {dtext!r}")
        for k in dc:
            k["source"] = "declared"
            k["confidence"] = conf["host"] if decl_host else conf["declared"]
        cands += dc
        if dc and decl_host:
            reasons.append("declared licence was taken from a hosting/aggregator site")
    if ignored:
        reasons.append("ignored licence mentions without licence context: " + "; ".join(sorted(set(ignored))[:3]))

    groups = {}
    for k in cands:
        groups.setdefault(k["family"], []).append(k)
    nc, nd = any(k["nc"] for k in cands), any(k["nd"] for k in cands)
    summary = sorted({k["spdx"] or k["name"] for k in cands})
    if not cands:
        return _result(reasons=["no licence found"] + reasons, verdict=verdicts["none"], hold_kind="none")
    versions = {fam: {k["spdx"] for k in ks if k["spdx"] != fam} for fam, ks in groups.items()}
    if len(groups) > 1 or any(len(v) > 1 for v in versions.values()):
        why = f"conflicting licences on one page: {summary}"
        cv = verdicts["conflict"]
        if nd:  # an ND claim in a conflict is still ND: the stricter verdict wins (incident 2026-10-09)
            cv = max(cv, cfg["nd_policy"], key=VERDICT_RANK.get)
            why += "; one claim is ND"
        clean = all(k["verdict"] == "ok" for k in cands) and not nd
        return _result(confidence=min(k["confidence"] for k in cands), nc=nc, nd=nd, source="page",
                       verdict=cv, reasons=[why] + reasons, candidates=summary,
                       hold_kind="conflict" if clean and cv == "hold" else None)

    best = max(cands, key=lambda k: (k["confidence"], k["spdx"] is not None and k["spdx"] != k["family"]))
    verdict = max((k["verdict"] for k in cands), key=VERDICT_RANK.get)
    for k in cands:
        if k["reason"]:
            reasons.append(f"{k['family']}: {k['reason']}")
    if nc or nd:
        policy_v = cfg["nd_policy"] if nd else cfg["nc_only_policy"]
        verdict = max(verdict, policy_v, key=VERDICT_RANK.get)
        if policy_v == "ok":
            reasons.append("NC licence accepted by policy (labelled non-commercial)")
        else:
            reasons.append(f"NC/ND licence ({'nd' if nd else 'nc-only'} policy {policy_v}): not ingestible; research-only reading may be allowed")
    source = best["source"]
    flagged_host = source == "page-host" or (source == "declared" and (host or decl_host))
    hold_kind = None
    if flagged_host:
        # a host flag may raise a verdict but never lower it (ND reject must stay reject)
        hold_kind = "host" if verdict == "ok" and verdicts["host"] == "hold" else None
        verdict = max(verdict, verdicts["host"], key=VERDICT_RANK.get)
        reasons.append("host-sourced licence metadata is unreliable; human review required")
    read_ok = verdict == "ok" or (bool(nc or nd) and not flagged_host)
    return _result(best["spdx"], best["name"], best["url"], source, best["confidence"], nc, nd, verdict,
                   reasons, read_ok, summary, family=best["family"], hold_kind=hold_kind)
