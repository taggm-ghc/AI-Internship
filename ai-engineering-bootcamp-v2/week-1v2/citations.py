"""APA 7 in-text citations and reference lists, rendered from verified
metadata only (p3m3 item #48). The model never writes an author, year or
title: it places passage markers, and this module replaces them.

Metadata: config/citation_metadata.json (CSL-JSON items keyed by
document_id, built by scripts/build_citation_metadata.py from publisher
records). A document with no record falls back to APA's no-author rule
(title in place of author) and "n.d." -- never a guessed author.

Rules implemented (APA 7; sources recorded in p3m3 D-N+9, taken from
university guides reproducing the Publication Manual because apastyle.apa.org
and Purdue OWL block automated access):
- in text: 1 author (Walker, 2007); 2 authors (Walker & Allen, 2004);
  3+ authors (Bradley et al., 1999) from the first citation; group author
  as written; no author -> ("Title," year); no date -> n.d.; several works
  in one parenthesis ordered as in the reference list, separated by
  semicolons; same author(s) + same year -> 2024a, 2024b.
- reference list: Family, G. I. initials; "&" before the last author; up
  to 20 authors, 21+ -> first 19, ". . .", last; sorted by first author's
  family name, then the next authors, then year, then title.
- Known deviation: titles keep their published capitalization instead of
  APA's sentence case -- automatic conversion lowercases proper nouns
  (Bayesian, Adam), and the official CSL APA style makes the same choice.
Output is Markdown (*italics*), which both Streamlit pages render.
"""
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

METADATA_PATH = Path(__file__).resolve().parent / "config" / "citation_metadata.json"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


@lru_cache(maxsize=1)
def _metadata() -> dict:
    try:
        return json.loads(METADATA_PATH.read_text())["items"]
    except (OSError, ValueError, KeyError):
        return {}


def lookup(document_id: str, title: str | None = None) -> dict:
    """The CSL item for a document, or an honest no-author/no-date stub."""
    item = _metadata().get(document_id)
    if item:
        return item
    return {"id": document_id, "type": "document", "title": title or document_id}


# LaTeX accent commands -> combining marks (R\\'esum\\'e -> Résumé)
_ACCENTS = {"'": "\u0301", "`": "\u0300", '"': "\u0308", "^": "\u0302", "~": "\u0303"}
_SUPERSCRIPT = str.maketrans("0123456789+-", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b")


def _math(expr: str) -> str:
    expr = expr.replace("\\ell", "\u2113")
    expr = re.sub(r"\^\{?([0-9+-]+)\}?", lambda m: m.group(1).translate(_SUPERSCRIPT), expr)
    return re.sub(r"[_{}\\]", "", expr)


def _clean_title(title: str) -> str:
    """Publisher records carry BibTeX/LaTeX markup that must not print in a
    reference: capitalization braces ({LLM}s, {A}ssociation) and inline math
    ($V$, F$^{2}$DR, $\\ell_2$). Found in 8 titles + 1 venue, 2026-09-25."""
    title = re.sub(r"\s+", " ", title or "").strip().rstrip(".")
    title = re.sub(r"\$([^$]*)\$", lambda m: _math(m.group(1)), title)
    title = re.sub(r"\\([\'`\"^~])\{?([A-Za-z])\}?", lambda m: unicodedata.normalize("NFC", m.group(2) + _ACCENTS[m.group(1)]), title)
    return title.replace("{", "").replace("}", "")


def _year(item: dict) -> str:
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    return str(parts[0]) if parts and parts[0] else "n.d."


def _family(name: dict) -> str:
    return name.get("literal") or name.get("family") or ""


def _initials(given: str) -> str:
    # "Lun-Wei" -> "L.-W.", "Yu" -> "Y.", "Mary Ann" -> "M. A."
    out = []
    for word in given.replace(".", " ").split():
        out.append("-".join(p[0].upper() + "." for p in word.split("-") if p))
    return " ".join(out)


def _ref_name(name: dict) -> str:
    if "literal" in name:
        return name["literal"]
    initials = _initials(name.get("given", ""))
    return f"{name['family']}, {initials}" if initials else name["family"]


def in_text_author(item: dict) -> str:
    authors = item.get("author") or []
    if not authors:
        return f"“{_clean_title(item.get('title', 'Untitled'))},”"
    if len(authors) == 1:
        return _family(authors[0])
    if len(authors) == 2:
        return f"{_family(authors[0])} & {_family(authors[1])}"
    return f"{_family(authors[0])} et al."


def _sort_key(item: dict) -> tuple:
    authors = item.get("author") or []
    names = tuple(_family(a).lower() for a in authors) or (re.sub(r"^(a|an|the)\s+", "", _clean_title(item.get("title", "")).lower()),)
    year = _year(item)
    return (names, year if year != "n.d." else "0000", _clean_title(item.get("title", "")).lower())


def _suffixes(items: list[dict]) -> dict:
    """APA 2024a/2024b: same in-text author string and same year."""
    groups: dict[tuple, list[dict]] = {}
    for it in items:
        groups.setdefault((in_text_author(it), _year(it)), []).append(it)
    out = {}
    for group in groups.values():
        if len(group) > 1:
            for i, it in enumerate(sorted(group, key=lambda x: _clean_title(x.get("title", "")).lower())):
                out[it["id"]] = "abcdefghijklmnopqrstuvwxyz"[i]
    return out


def _cite(item: dict, suffix: str = "") -> str:
    """One work inside a parenthetical: 'Lin et al., 2026', 'Walker & Allen,
    2004', '“Title,” n.d.' (the comma sits inside the quotes)."""
    author, year = in_text_author(item), _year(item)
    year = f"{year}-{suffix}" if suffix and year == "n.d." else year + suffix
    return f"{author} {year}" if author.endswith(",\u201d") else f"{author}, {year}"


def _parenthetical(cites: list[dict], suffix: dict) -> str:
    """Works in reference-list order, separated by semicolons; works by the
    same author(s) share one name with their years comma-separated:
    (Bradley et al., 1999; Walker, 2007a, 2007b)."""
    groups: list[tuple[str, list[str]]] = []
    for it in cites:
        author = in_text_author(it)
        year = _cite(it, suffix.get(it["id"], "")).removeprefix(author).lstrip(", ").strip()
        if groups and groups[-1][0] == author:
            groups[-1][1].append(year)
        else:
            groups.append((author, [year]))
    return "; ".join(f"{a} {', '.join(ys)}" if a.endswith(",\u201d") else f"{a}, {', '.join(ys)}" for a, ys in groups)


def reference_entry(item: dict, suffix: str = "") -> str:
    authors = [_ref_name(a) for a in (item.get("author") or [])]
    if len(authors) > 20:
        authors = authors[:19] + ["…"] + authors[-1:]
        author_str = ", ".join(authors[:-2]) + ", . . . " + authors[-1]
    elif len(authors) > 1:
        author_str = ", ".join(authors[:-1]) + ", & " + authors[-1]
    else:
        author_str = authors[0] if authors else ""
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    year = _year(item) + suffix if _year(item) != "n.d." else "n.d." + (f"-{suffix}" if suffix else "")
    kind = item.get("type")
    if kind in ("article-newspaper", "post-weblog") and len(parts) >= 3:
        date = f"{year}, {MONTHS[parts[1] - 1]} {parts[2]}"
    else:
        date = year
    title = _clean_title(item.get("title", "Untitled"))
    link = f"https://doi.org/{item['DOI']}" if item.get("DOI") else (item.get("URL") or "")
    container = _clean_title(item["container-title"]) if item.get("container-title") else None
    pages = (item.get("page") or "").replace("-", "–")

    if kind == "article" and item.get("genre") == "Preprint":
        body = f"*{title}* [Preprint]. {item.get('publisher', 'arXiv')}."
    elif kind == "paper-conference":
        details = ", ".join(x for x in [f"Vol. {item['volume']}" if item.get("volume") else "", f"pp. {pages}" if pages else ""] if x)
        editors = item.get("editor") or []
        if editors:  # "In L.-W. Ku, A. Martins, & V. Srikumar (Eds.), *Proceedings*"
            names = [f"{_initials(e.get('given', ''))} {e.get('family', e.get('literal', ''))}".strip() for e in editors]
            joined = names[0] if len(names) == 1 else (" & ".join(names) if len(names) == 2 else ", ".join(names[:-1]) + ", & " + names[-1])
            eds = f"{joined} ({'Ed.' if len(names) == 1 else 'Eds.'}), "
        else:
            eds = ""
        body = f"{title}. In {eds}*{container}*" + (f" ({details})" if details else "") + "." if container else f"*{title}*."
        if item.get("publisher"):
            body += f" {item['publisher']}."
    elif kind == "article-journal":
        vol = f", *{item['volume']}*" if item.get("volume") else ""
        issue = f"({item['issue']})" if item.get("issue") else ""
        body = f"{title}. *{container}*{vol}{issue}" + (f", {pages}" if pages else "") + "."
    elif kind == "article-newspaper":
        body = f"*{title}*. {container}." if container else f"*{title}*."
    elif kind == "post-weblog":
        body = f"{title}. *{container}*." if container else f"{title}."
    else:
        body = f"*{title}*."
    head = f"{author_str} ({date})." if author_str else ""
    if not author_str:  # APA: no author -> the title moves to the author position, date follows it
        return f"*{title}*. ({date}). {link}".strip()
    return f"{head} {body} {link}".strip()


_NUM_MARKER = re.compile(r"(?:\s*\[\s*\d+(?:\s*,\s*\d+)*\s*\])+")
_ID_MARKER = re.compile(r"(?:\s*\[[A-Za-z0-9][\w.]*(?:-[\w.]+)+\])+")


def _render(text: str, marker_re: re.Pattern, resolve) -> tuple[str, list[str]]:
    """Replace each run of markers with one APA parenthetical; move it before
    a preceding sentence-final period; return (text, reference list)."""
    cited_order: list[str] = []
    spans = []
    for m in marker_re.finditer(text):
        ids = [d for tok in re.findall(r"\[([^\]]+)\]", m.group(0)) for part in tok.split(",") if (d := resolve(part.strip()))]
        ids = list(dict.fromkeys(ids))
        spans.append((m.start(), m.end(), ids))
        cited_order += [d for d in ids if d not in cited_order]
    items = {d: lookup(d) for d in cited_order}
    suffix = _suffixes(list(items.values()))
    out, last = [], 0
    for start, end, ids in spans:
        out.append(text[last:start])
        if ids:
            label = _parenthetical(sorted((items[d] for d in ids), key=_sort_key), suffix)
            # "claim. [1]" -> "claim (Author, 2024)."
            prev = "".join(out)
            if prev.rstrip().endswith((".", "!", "?")):
                stripped = prev.rstrip()
                out = [stripped[:-1] + f" ({label})" + stripped[-1]]
            else:
                out.append(f" ({label})")
        last = end
    out.append(text[last:])
    rendered = re.sub(r" +([.,;:!?])", r"\1", "".join(out))
    refs = [reference_entry(it, suffix.get(it["id"], "")) for it in sorted(items.values(), key=_sort_key)]
    return rendered, refs


def render_numbered(text: str, passage_document_ids: list[str]) -> tuple[str, list[str]]:
    """/ask: [n] markers -> APA; n indexes the 1-based passages shown to the
    model. Out-of-range numbers are dropped, never guessed."""
    def resolve(tok: str):
        return passage_document_ids[int(tok) - 1] if tok.isdigit() and 1 <= int(tok) <= len(passage_document_ids) else None
    return _render(text, _NUM_MARKER, resolve)


def render_document_ids(text: str, allowed_document_ids: list[str]) -> tuple[str, list[str]]:
    """/agent: [document_id] markers -> APA, accepted only for documents the
    tool actually returned (item #47's trace-derived sources)."""
    allowed = list(dict.fromkeys(allowed_document_ids))

    def resolve(tok: str):
        # Exact ID, or an unambiguous prefix of exactly one returned document:
        # the model sometimes shortens "arxiv-2609.18063-other-half-..." to
        # "arxiv-2609.18063" (observed 1 run in 3, 2026-09-25). Still limited
        # to documents the tool returned; an ambiguous prefix is dropped.
        if tok in allowed:
            return tok
        matches = [d for d in allowed if d.startswith(tok + "-")]
        return matches[0] if len(matches) == 1 else None
    return _render(text, _ID_MARKER, resolve)
