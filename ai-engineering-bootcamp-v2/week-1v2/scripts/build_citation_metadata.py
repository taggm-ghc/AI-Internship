"""p3m3 item #48 -- builds config/citation_metadata.json: verified CSL-JSON
citation records (authors, year, title, venue, DOI/URL) for every corpus
document, so APA 7 citations are rendered from publisher records, never from
the model. Design: p3m3/week2-priority-checklist.md, section D-N+9.

Only 9/261 documents carried author/date in their provenance; this recovers
the rest from each publisher's authoritative record, keyed by the identifier
already encoded in document_id / source_url:
  arXiv, PMLR, NeurIPS, JMLR -> the paper's landing page citation_* meta
                 tags (Highwire/Google Scholar standard). arXiv abstract pages
                 at >= 3 s spacing: the export API answered HTTP 406 to every
                 request on 2026-09-25, whatever the headers.
  ACL Anthology -> the paper's .bib file (its landing pages carry no
                 citation_* tags; probed 2026-09-25)
  news/blogs  -> the existing provenance record (author, published_at)
Anything unrecoverable is left out (with a reason) and falls back to APA's
no-author/no-date rules at render time -- never guessed.

Read-only against the production DB (one SELECT). Network reads only to
public publisher endpoints. Writes one repo file.
Run with: python scripts/build_citation_metadata.py
"""
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from sqlalchemy import text

from db import get_engine

OUT = BASE / "config" / "citation_metadata.json"
UA = "AI-Internship-citation-metadata/1.0 (course project; polite, low volume)"
DELAY_S = 1.0
ARXIV_DELAY_S = 3.0  # arXiv's API terms ask for >= 3 s between requests
PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "di", "du", "le", "la", "dos", "das", "ter", "ten"}
WEB_SITE_NAMES = {
    "venturebeat": "VentureBeat", "techcrunch": "TechCrunch", "aws-machine-learning-blog": "AWS Machine Learning Blog",
    "coralogix": "Coralogix", "greptime": "Greptime", "respan": "Respan",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def split_name(raw: str) -> dict:
    """'Lin, Yu' or 'Yu Lin' -> CSL {family, given}; keeps lowercase particles
    ('van der Berg') with the family name."""
    raw = re.sub(r"\s+", " ", raw).strip()
    if "," in raw:
        family, given = [p.strip() for p in raw.split(",", 1)]
        return {"family": family, "given": given}
    parts = raw.split(" ")
    if len(parts) == 1:
        return {"literal": raw}
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    return {"family": " ".join(parts[i:]), "given": " ".join(parts[:i])}


def date_parts(value: str) -> list | None:
    m = re.match(r"(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?", value or "")
    return [int(x) for x in m.groups() if x] if m else None


def meta_tags(page: str) -> dict:
    tags: dict[str, list[str]] = {}
    for m in re.finditer(r'<meta\s+[^>]*?name="(citation_[a-z_]+)"[^>]*?content="([^"]*)"', page, re.I):
        tags.setdefault(m.group(1).lower(), []).append(html.unescape(m.group(2)).strip())
    for m in re.finditer(r'<meta\s+[^>]*?content="([^"]*)"[^>]*?name="(citation_[a-z_]+)"', page, re.I):
        tags.setdefault(m.group(2).lower(), []).append(html.unescape(m.group(1)).strip())
    return tags


def from_meta(doc_id: str, url: str, csl_type: str) -> dict:
    t = meta_tags(fetch(url))
    if not t.get("citation_title") or not t.get("citation_author"):
        raise ValueError(f"no citation_* meta at {url}")
    date = (t.get("citation_publication_date") or t.get("citation_date") or [""])[0]
    item = {
        "id": doc_id, "type": csl_type, "title": t["citation_title"][0],
        "author": [split_name(a) for a in t["citation_author"]],
        "issued": {"date-parts": [date_parts(date)]} if date_parts(date) else None,
        "container-title": (t.get("citation_conference_title") or t.get("citation_journal_title")
                            or t.get("citation_inbook_title") or [None])[0],
        "volume": (t.get("citation_volume") or [None])[0],
        "issue": (t.get("citation_issue") or [None])[0],
        "page": "-".join(x for x in [(t.get("citation_firstpage") or [None])[0], (t.get("citation_lastpage") or [None])[0]] if x) or None,
        "DOI": (t.get("citation_doi") or [None])[0],
        "URL": (t.get("citation_abstract_html_url") or [url])[0],
        "publisher": (t.get("citation_publisher") or [None])[0],
        "_source": url,
    }
    return {k: v for k, v in item.items() if v}


def from_acl_bib(doc_id: str, anthology_id: str) -> dict:
    bib = fetch(f"https://aclanthology.org/{anthology_id}.bib")
    field = lambda name: (m.group(1) if (m := re.search(name + r'\s*=\s*"((?:[^"\\]|\\.)*)"', bib, re.S)) else None)
    names = lambda raw: [split_name(n) for n in re.split(r"\s+and\s+", re.sub(r"\s+", " ", raw or "")) if n.strip()]
    year = field("year")
    item = {
        "id": doc_id, "type": "paper-conference", "title": re.sub(r"\s+", " ", field("title") or "").strip(),
        "author": names(field("author")), "editor": names(field("editor")) or None,
        "issued": {"date-parts": [[int(year)]]} if year else None,
        "container-title": field("booktitle"), "publisher": field("publisher"), "page": (field("pages") or "").replace("--", "-") or None,
        "DOI": field("doi"), "URL": field("url"), "_source": f"https://aclanthology.org/{anthology_id}.bib",
    }
    if not item["title"] or not item["author"]:
        raise ValueError("incomplete .bib")
    return {k: v for k, v in item.items() if v}


def landing_url(doc_id: str, source: str, source_url: str) -> tuple[str, str] | None:
    if source == "arxiv":
        m = re.match(r"arxiv-(\d{4}\.\d{4,5})", doc_id)
        return (f"https://arxiv.org/abs/{m.group(1)}", "article") if m else None
    if source == "pmlr":
        m = re.match(r"pmlr-(v\d+)-([a-z0-9]+)-", doc_id)
        return (f"https://proceedings.mlr.press/{m.group(1)}/{m.group(2)}.html", "paper-conference") if m else None

    if source == "neurips":
        m = re.search(r"paper/(\d{4})/file/([0-9a-f]+)-Paper-Conference\.pdf", source_url or "")
        return (f"https://papers.nips.cc/paper_files/paper/{m.group(1)}/hash/{m.group(2)}-Abstract-Conference.html", "paper-conference") if m else None
    if source == "jmlr":
        m = re.search(r"volume(\d+)/([^/]+)/", source_url or "")
        return (f"https://jmlr.org/papers/v{m.group(1)}/{m.group(2)}.html", "article-journal") if m else None
    return None


def web_item(row) -> dict | None:
    if not (row.author and row.published_at and row.title):
        return None
    raw = row.author.strip()
    if raw.lower().endswith(" team"):  # "Coralogix Team" -> group author
        authors = [{"literal": raw[: -len(" team")].strip()}]
    else:
        # provenance keeps a job title after the name for some posts ("Name, Founder and CEO, Org")
        names = [n.strip() for n in raw.split(",") if n.strip()]
        site = WEB_SITE_NAMES.get(row.source, row.source or "").lower()
        names = [n for n in names if not re.search(r"\b(ceo|founder|cto|editor|writer|head|director)\b", n, re.I)
                 and n.lower() != site]  # the org after a job title is not a second author
        authors = [split_name(n) for n in names]
    return {
        "id": row.document_id, "type": "article-newspaper" if row.source in ("venturebeat", "techcrunch") else "post-weblog",
        "title": row.title, "author": authors, "issued": {"date-parts": [date_parts(row.published_at)]},
        "container-title": WEB_SITE_NAMES.get(row.source, row.source), "URL": row.source_url,
        "_source": "provenance record (original fetch)",
    }


def main():
    with get_engine().connect() as c:
        rows = c.execute(text("SELECT document_id, source, source_url, author, published_at, title "
                              "FROM internship.document_provenance ORDER BY document_id")).fetchall()
    items, failures = {}, {}
    for i, r in enumerate(rows, 1):
        try:
            if r.source == "acl_anthology" and (m := re.search(r"aclanthology\.org/([^/]+?)\.pdf", r.source_url or "")):
                items[r.document_id] = from_acl_bib(r.document_id, m.group(1))
                time.sleep(DELAY_S)
            elif (lu := landing_url(r.document_id, r.source, r.source_url)):
                item = from_meta(r.document_id, *lu)
                if r.source == "arxiv":
                    aid = lu[0].rsplit("/", 1)[-1]
                    item.update(genre="Preprint", publisher="arXiv", DOI=item.get("DOI") or f"10.48550/arXiv.{aid}",
                                URL=f"https://arxiv.org/abs/{aid}")
                    item.pop("container-title", None)
                items[r.document_id] = item
                time.sleep(ARXIV_DELAY_S if r.source == "arxiv" else DELAY_S)
            elif (w := web_item(r)):
                items[r.document_id] = w
            else:
                failures[r.document_id] = f"no authoritative record (source={r.source!r})"
        except Exception as exc:
            failures[r.document_id] = f"{type(exc).__name__}: {exc}"[:160]
        if i % 25 == 0:
            print(f"  {i}/{len(rows)}", flush=True)
    OUT.write_text(json.dumps({"_meta": {
        "item": "p3m3 #48", "format": "CSL-JSON items keyed by document_id",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "documents": len(rows),
        "covered": len(items), "failures": failures,
    }, "items": items}, indent=1, ensure_ascii=False) + "\n")
    by_type = {}
    for it in items.values():
        by_type[it["type"]] = by_type.get(it["type"], 0) + 1
    print(f"covered {len(items)}/{len(rows)} documents; by type {by_type}")
    for k, v in failures.items():
        print(f"  MISSING {k}: {v}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
