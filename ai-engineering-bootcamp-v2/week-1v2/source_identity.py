"""Source identity: identifier normalisers and the precedence resolver (item #65, plan section 8).

Pure and offline: no DB, no network. Config lives in config/source_identity.json.

Trust model: canonical-original status is never taken from caller-supplied data. extract_identifiers()
marks identifiers canonical+original only when called with trusted=True (an authenticated ingest, i.e. a
valid X-Ingest-Key, or the admin backfill over verified metadata). The unkeyed `ingester` agent path therefore
yields derived, non-canonical ids only; resolve() can then only return NEW_SOURCE or REVIEW for it, never a
redirect onto another document. Canonical status for those items comes later from an admin backfill after
verification.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CONFIG_PATH = Path(__file__).parent / "config" / "source_identity.json"
CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

NEW_SOURCE = "NEW_SOURCE"
SAME_SOURCE = "SAME_SOURCE"
REVIEW = "REVIEW"
CONFLICT = "CONFLICT"

_ARXIV_NEW = re.compile(r"^\d{4}\.\d{4,5}$")
_ARXIV_OLD = re.compile(r"^[a-z][a-z\-]*(\.[a-z]{2})?/\d{7}$")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")


@dataclass
class Resolution:
    status: str
    source_id: object = None
    reason: str = ""
    candidates: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _strip_doi_trailing(value):
    return value.rstrip(CFG["doi_trailing_punctuation"])


def normalise_arxiv(raw):
    v = str(raw).strip()
    m = re.match(r"^https?://([^/]+)/(?:abs|pdf)/([^?#]+)", v, re.I)
    if m and m.group(1).lower() in CFG["arxiv_hosts"]:
        v = m.group(2)
    v = re.sub(r"^arxiv:\s*", "", v, flags=re.I)
    v = re.sub(r"\.pdf$", "", v, flags=re.I)
    v = re.sub(r"v\d+$", "", v, flags=re.I)
    if _ARXIV_NEW.match(v):
        return v
    v = v.lower()
    if _ARXIV_OLD.match(v):
        return v
    return None


def normalise_doi(raw):
    v = str(raw).strip()
    v = re.sub(r"^doi:\s*", "", v, flags=re.I)
    m = re.match(r"^https?://([^/]+)/([^?#]+)", v, re.I)
    if m and m.group(1).lower() in CFG["doi_hosts"]:
        v = m.group(2)
    v = _strip_doi_trailing(v.strip().lower())
    return v if _DOI.match(v) else None


def _isbn13_check(digits12):
    s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits12))
    return str((10 - s % 10) % 10)


def normalise_isbn(raw):
    v = re.sub(r"^isbn(-1[03])?:?\s*", "", str(raw).strip(), flags=re.I)
    v = re.sub(r"[\s\-]", "", v).upper()
    if re.fullmatch(r"\d{9}[\dX]", v):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(v))
        if total % 11 != 0:
            return None
        core = "978" + v[:9]
        return core + _isbn13_check(core)
    if re.fullmatch(r"\d{13}", v):
        return v if v[-1] == _isbn13_check(v[:12]) else None
    return None


def normalise_pmid(raw):
    v = re.sub(r"^pmid:?\s*", "", str(raw).strip(), flags=re.I)
    return v.lstrip("0") if re.fullmatch(r"\d{1,9}", v) and v.strip("0") else None


def normalise_url(raw):
    v = str(raw).strip()
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", v, re.I):
        return None
    parts = urlsplit(v)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in CFG["url"]["default_ports"] or not host:
        return None
    netloc = host
    if parts.port and parts.port != CFG["url"]["default_ports"][scheme]:
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    for idx in CFG["url"]["index_files"]:
        if path.lower().endswith("/" + idx):
            path = path[: -len(idx)]
            break
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    drop = set(CFG["url"]["tracking_params"])
    prefixes = tuple(CFG["url"]["tracking_param_prefixes"])
    query = [(k, val) for k, val in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in drop and not k.lower().startswith(prefixes)]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalise_other(raw):
    v = str(raw).strip()
    return v or None


def normalise(id_type, raw):
    """Return the normalised value, or None when raw is not valid for the type."""
    spec = CFG["types"].get(id_type)
    if spec is None or raw is None:
        return None
    return globals()[spec["normaliser"]](raw)


def strength(id_type):
    return CFG["types"].get(id_type, CFG["types"]["other"])["strength"]


def _is_strong(id_type):
    return strength(id_type) == 1


def _entry(id_type, raw, canonical, origin):
    norm = normalise(id_type, raw)
    if norm is None:
        id_type, norm = "other", normalise_other(raw)
        canonical, origin = False, "derived"
    if norm is None:
        return None
    return {"type": id_type, "value_norm": norm, "value_raw": str(raw),
            "is_canonical": canonical, "origin": origin}


def _arxiv_or_doi_from_url(url):
    host = (urlsplit(url).hostname or "").lower()
    if host in CFG["arxiv_hosts"]:
        return "arxiv"
    if host in CFG["doi_hosts"]:
        return "doi"
    return None


def extract_identifiers(provenance, metadata=None, trusted=False):
    """List identifier dicts {type, value_norm, value_raw, is_canonical, origin}.

    With trusted=False (default) every caller-supplied value is origin='derived', is_canonical=False,
    whatever identifier_origin, metadata DOI or arxiv.org / doi.org URL claims. With trusted=True, strong
    ids are canonical+original when provenance says so (identifier_origin == 'original'), when parsed from
    an arxiv.org / doi.org URL, or for the metadata DOI; everything else stays derived.
    """
    pk = CFG["provenance_keys"]
    provenance = provenance or {}
    declared_original = trusted and provenance.get(pk["origin"]) == pk["original_value"]
    url_canonical = bool(trusted)
    found = []
    for id_type in ("arxiv", "doi", "isbn"):
        raw = provenance.get(pk[id_type])
        if raw:
            found.append(_entry(id_type, raw, declared_original, "original" if declared_original else "derived"))
    url = provenance.get(pk["url"])
    if url:
        found.append(_entry("url", url, False, "derived"))
        kind = _arxiv_or_doi_from_url(str(url).strip()) if normalise_url(url) else None
        if kind and normalise(kind, url):
            found.append(_entry(kind, url, url_canonical, "original" if url_canonical else "derived"))
    if metadata:
        mk = CFG["metadata_keys"]
        if metadata.get(mk["doi"]):
            found.append(_entry("doi", metadata[mk["doi"]], url_canonical, CFG["metadata_doi_origin"] if url_canonical else "derived"))
        if metadata.get(mk["url"]):
            found.append(_entry("url", metadata[mk["url"]], False, "derived"))
    merged = {}
    for e in filter(None, found):
        key = (e["type"], e["value_norm"])
        cur = merged.get(key)
        if cur is None or (e["is_canonical"] and not cur["is_canonical"]):
            merged[key] = e
    return list(merged.values())


def _is_original_canonical(link):
    return bool(link.get("is_canonical")) and link.get("origin") == "original"


def resolve(new_ids, existing_links):
    """Decide how a document's identifiers relate to known sources.

    existing_links: dicts {source_id, type, value_norm, is_canonical, origin}.
    Returns Resolution with status NEW_SOURCE, SAME_SOURCE (source_id), REVIEW (reason,
    candidates) or CONFLICT (reason, candidates). Rules: plan section 8 precedence.
    """
    never = set(CFG["never_auto_match_types"])
    new_ids = [i for i in new_ids if i["type"] not in never]
    links = [l for l in existing_links if l["type"] not in never]
    warnings = []

    canon_new = {}
    for i in new_ids:
        if _is_original_canonical(i):
            prev = canon_new.setdefault(i["type"], i["value_norm"])
            if prev != i["value_norm"]:
                return Resolution(CONFLICT, reason=f"new record carries two different original canonical {i['type']} ids: {prev}, {i['value_norm']}")

    by_source = {}
    for l in links:
        by_source.setdefault(l["source_id"], []).append(l)

    def source_canon(sid, id_type):
        return {l["value_norm"] for l in by_source[sid] if l["type"] == id_type and _is_original_canonical(l)}

    # rule 1: original canonical match is decisive
    strong = set()
    for i in new_ids:
        if _is_original_canonical(i):
            for l in links:
                if l["type"] == i["type"] and l["value_norm"] == i["value_norm"] and _is_original_canonical(l):
                    strong.add(l["source_id"])
    if len(strong) > 1:
        return Resolution(CONFLICT, candidates=sorted(strong, key=str),
                          reason="original canonical ids match more than one existing source")
    if strong:
        sid = next(iter(strong))
        for t, v in canon_new.items():
            others = source_canon(sid, t) - {v}
            if others:
                return Resolution(CONFLICT, candidates=[sid],
                                  reason=f"source {sid} has a different original canonical {t} id ({sorted(others)}) than the new record ({v})")
        for i in new_ids:
            if not _is_original_canonical(i) and _is_strong(i["type"]):
                known = source_canon(sid, i["type"])
                if known and i["value_norm"] not in known:
                    warnings.append(f"derived {i['type']} {i['value_norm']} conflicts with original {sorted(known)}; original wins")
        return Resolution(SAME_SOURCE, source_id=sid, reason="original canonical id match", warnings=warnings)

    # rules 2/4: only non-decisive matches remain; can never merge
    candidates = set()
    for i in new_ids:
        for l in links:
            if l["type"] != i["type"] or l["value_norm"] != i["value_norm"]:
                continue
            sid = l["source_id"]
            distinct = [t for t, v in canon_new.items() if source_canon(sid, t) - {v}]
            if distinct:
                warnings.append(f"shared {i['type']} {i['value_norm']} ignored: source {sid} has a different original {distinct[0]} id")
                continue
            candidates.add(sid)
    if candidates:
        return Resolution(REVIEW, candidates=sorted(candidates, key=str), warnings=warnings,
                          reason="match only on non-decisive identifiers (derived, non-canonical or url); never auto-merged")
    return Resolution(NEW_SOURCE, reason="no identifier match", warnings=warnings)
