"""Gate 3: heuristic detectors. Catch crude/known patterns only; a clean report
is not proof of safety (novel or adaptive attacks, rendering-only tricks,
non-English phrasing and paraphrased injections pass)."""
import re
import unicodedata

from intake.extract import is_invisible, load_config, strip_invisible  # noqa: F401  (load_config re-exported)


def _dcfg(cfg: dict) -> dict:
    return cfg.get("detectors", cfg)


def normalise(text: str, cfg: dict) -> str:
    d = _dcfg(cfg)
    text, _ = strip_invisible(text, set(d["invisible_categories"]), d.get("invisible_ranges", []))
    return re.sub(r"[ \t\r\f\v]+", " ", unicodedata.normalize("NFKC", text))


def squash(text: str) -> str:
    """Casefold and collapse all whitespace (newlines, NBSP, ...) for phrase/delimiter matching."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).casefold()


def _phrase_hits(norm: str, d: dict) -> list[str]:
    low = squash(norm)
    return [p for p in d["injection_phrases"] + d["action_phrases"] if squash(p) in low]


def _delim_hits(norm: str, d: dict) -> list[str]:
    sq = squash(norm)
    return [m.group(0).strip() for p in d["delimiter_patterns"] for m in re.finditer(p, sq, re.I)]


def _role_hits(norm: str, d: dict) -> list[str]:
    return [m.group(0).strip() for p in d["role_line_patterns"] for m in re.finditer(p, norm, re.I | re.M)]


def passage_flagged(passage: str, cfg: dict) -> bool:
    d, n = _dcfg(cfg), normalise(passage, cfg)
    return bool(_phrase_hits(n, d) or _delim_hits(n, d) or _role_hits(n, d))


def _blob_chars(text: str, d: dict) -> int:
    b = d["blob"]
    total = 0
    for m in re.finditer(r"[A-Za-z0-9+/]{%d,}={0,2}" % b["base64_min_run"], text):
        s = m.group(0)
        if re.search(r"\d", s) and re.search(r"[a-z]", s) and re.search(r"[A-Z]", s):
            total += len(s)
    for m in re.finditer(r"\b[0-9a-fA-F]{%d,}\b" % b["hex_min_run"], text):
        total += len(m.group(0))
    return total


def _mixed_script_words(text: str, d: dict) -> int:
    scripts = d["mixed_script"]["scripts"]
    n = 0
    for w in re.findall(r"\w+", text):
        seen = set()
        for ch in w:
            if ch.isalpha():
                name = unicodedata.name(ch, "")
                for s in scripts:
                    if name.startswith(s):
                        seen.add(s)
        if len(seen) > 1:
            n += 1
    return n


def run(raw_text: str, visible_text: str, cfg: dict) -> dict:
    """Returns {flags: [{name, detail}], scores: {...}}."""
    d = _dcfg(cfg)
    flags, scores = [], {}

    def flag(name, detail):
        flags.append({"name": name, "detail": detail})

    cats, ranges = set(d["invisible_categories"]), d.get("invisible_ranges", [])
    lo, hi = d["tag_char_range"]
    n_inv = sum(1 for ch in raw_text + visible_text if is_invisible(ch, cats, ranges))
    vs = d["variation_smuggling"]
    n_vs = sum(1 for ch in raw_text + visible_text if any(a <= ord(ch) <= b for a, b in vs["ranges"]))
    n_tag = sum(1 for ch in raw_text + visible_text if lo <= ord(ch) <= hi)
    scores["invisible_chars"] = n_inv
    if n_tag:
        flag("unicode_tag_smuggling", f"{n_tag} Unicode tag characters")
    if n_inv:
        flag("invisible_unicode", f"{n_inv} invisible format/private-use/variation characters")
    if n_vs >= vs["min_count"]:
        flag("variation_selector_smuggling", f"{n_vs} variation selector characters")

    raw_n, vis_n = normalise(raw_text, cfg), normalise(visible_text, cfg)
    ph_raw, ph_vis = _phrase_hits(raw_n, d), set(_phrase_hits(vis_n, d))
    dl_raw, dl_vis = _delim_hits(raw_n, d), set(_delim_hits(vis_n, d))
    rl = _role_hits(vis_n, d) + [h for h in _role_hits(raw_n, d)]
    raw_sq = squash(raw_n)
    scores["injection_phrase_count"] = sum(raw_sq.count(squash(p)) for p in set(ph_raw))
    if set(ph_raw) - ph_vis or set(dl_raw) - dl_vis:
        hid = sorted((set(ph_raw) - ph_vis) | (set(dl_raw) - dl_vis))
        flag("hidden_payload", f"present only in dropped content: {hid}")
    if ph_vis:
        flag("injection_phrases", sorted(ph_vis))
    if dl_vis:
        flag("delimiter_forgery", sorted(dl_vis))
    if rl:
        flag("role_marker", sorted(set(rl)))

    hd = d["hidden_delta"]
    frac = 0.0
    if len(raw_n) > 0:
        frac = max(0.0, (len(raw_n) - len(vis_n)) / len(raw_n))
    scores["hidden_delta"] = round(frac, 4)
    if len(raw_n) >= hd["min_raw_chars"] and frac >= hd["fraction"]:
        flag("hidden_delta_high", f"{frac:.0%} of text was not visible")

    blob = _blob_chars(raw_n, d)
    scores["encoded_blob_chars"] = blob
    if blob >= d["blob"]["min_total_chars"]:
        flag("encoded_blob", f"{blob} chars of base64/hex-like runs")

    urls = [m.group(0) for p in d["url_patterns"] for m in re.finditer(p, raw_n, re.I)]
    if urls:
        flag("data_javascript_url", [u[:40] for u in urls[:5]])

    ld = d["link_density"]
    url_chars = sum(len(u) for u in re.findall(r"https?://\S+", vis_n))
    scores["link_density"] = round(url_chars / len(vis_n), 4) if vis_n else 0.0
    if len(vis_n) >= ld["min_chars"] and scores["link_density"] >= ld["fraction"]:
        flag("high_link_density", f"{scores['link_density']:.0%} of text is URLs")

    ms = _mixed_script_words(raw_n, d)
    scores["mixed_script_words"] = ms
    if ms >= d["mixed_script"]["min_words"]:
        flag("homoglyph_mixed_script", f"{ms} words mix scripts")

    ri = d["repeated_instructions"]
    low = raw_sq
    cnt = sum(low.count(squash(p)) for p in ri["phrases"])
    words = max(1, len(low.split()))
    scores["instruction_density_per_1000_words"] = round(cnt * 1000 / words, 2)
    if cnt >= ri["min_count"] and scores["instruction_density_per_1000_words"] >= ri["per_1000_words"]:
        flag("repeated_instructions", f"{cnt} imperative phrases")

    ex = d["md_exfil"]
    imgs = re.findall(ex["image_pattern"], raw_n)
    if imgs:
        flag("md_image_exfil", [u[:80] for u in imgs[:5]])
    lq = re.findall(ex["link_query_pattern"], raw_n)
    if lq:
        flag("long_query_link", [u[:80] for u in lq[:5]])
    return {"flags": flags, "scores": scores}
