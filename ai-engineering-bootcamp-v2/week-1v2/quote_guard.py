"""Short-cited-quote guard (p3m3 item #62, W3; gaps G6/finding S3).

Pure functions, no network. Full chunks still go to the model; this limits
what an answer may quote back: short, verbatim, and tied to a bracketed
document ID. Mirrors the Phase 4.3 grounding-gate shape: validate returns
violations, the caller decides to fail, trim, or log.
"""
import re

MAX_QUOTE_CHARS = 300  # single source of truth; conservative vs 200-300 token practice
ELLIPSIS = "…"
ID_WINDOW = 3  # chars allowed between a closing quote mark and its [id]
MIN_QUOTE_CHARS = 4  # ignore scare quotes like "AI"

QUOTE_RULE_PROMPT = (
    "Quoting rule: quote at most one short sentence (under about "
    f"{MAX_QUOTE_CHARS} characters) per source, in quotation marks, "
    "immediately followed by that source's bracketed document ID; otherwise "
    "paraphrase. Never reproduce lists, tables or whole paragraphs from a source."
)

# Same rule for /ask, whose passages are numbered [1]..[N] instead of carrying document IDs.
QUOTE_RULE_PROMPT_NUMBERED = (
    "Quoting rule: quote at most one short sentence (under about "
    f"{MAX_QUOTE_CHARS} characters) per source, in quotation marks, "
    "immediately followed by that passage's number in square brackets, e.g. [2]; "
    "otherwise paraphrase. Never reproduce lists, tables or whole paragraphs from a passage."
)

# kind, regex (group 'q' = quoted text)
_QUOTE_PATTERNS = [
    ("quote", re.compile(r"“(?P<q>[^”]+)”")),
    ("quote", re.compile(r'"(?P<q>[^"\n]+)"')),
    ("block", re.compile(r"^[ \t]*>[ \t]?(?P<q>.+)$", re.M)),
]
_ID_AFTER = re.compile(r"\s{0,%d}[.,;:]?\s{0,%d}\[[^\]\s]+\]" % (ID_WINDOW, ID_WINDOW))
_ID_ANY = re.compile(r"\[[A-Za-z0-9][^\]\s]*\]")


def _norm(text: str) -> str:
    text = re.sub(r"[“”„\"]", '"', text)
    text = re.sub(r"[‘’‛]", "'", text)
    text = re.sub(r"[‐-―]", "-", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def extract_quotes(answer: str) -> list[dict]:
    """Quoted spans in order: {'text','start','end','kind'}. start/end cover the
    whole quoted span including marks (or the blockquote line)."""
    found, taken = [], []
    for kind, pat in _QUOTE_PATTERNS:
        for m in pat.finditer(answer or ""):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue  # already covered (e.g. straight quotes inside a blockquote)
            if len(m.group("q").strip()) < MIN_QUOTE_CHARS:
                continue
            taken.append((m.start(), m.end()))
            found.append({"text": m.group("q").strip(), "start": m.start(), "end": m.end(), "kind": kind})
    return sorted(found, key=lambda q: q["start"])


def _has_adjacent_id(answer: str, quote: dict) -> bool:
    tail = answer[quote["end"]:quote["end"] + 40]
    if quote["kind"] == "block":  # ID may sit at the end of the quote line itself
        return bool(_ID_ANY.search(quote["text"][-60:])) or bool(_ID_AFTER.match(tail))
    return bool(_ID_AFTER.match(tail))


def validate_quotes(answer: str, passages: list[str], max_chars: int = MAX_QUOTE_CHARS) -> list[dict]:
    """Violations: {'quote','reason','start','end'}; reason is one of
    'too_long', 'not_verbatim', 'missing_id'. Empty list means compliant."""
    haystack = [_norm(p) for p in passages or []]
    out = []
    for q in extract_quotes(answer):
        reasons = []
        if len(q["text"]) > max_chars:
            reasons.append("too_long")
        core = _norm(q["text"].replace(ELLIPSIS, " ... "))
        pieces = [p.strip() for p in core.split("...") if len(p.strip()) >= MIN_QUOTE_CHARS]
        if not pieces or not all(any(p in h for h in haystack) for p in pieces):
            reasons.append("not_verbatim")
        if not _has_adjacent_id(answer, q):
            reasons.append("missing_id")
        out += [{"quote": q["text"], "reason": r, "start": q["start"], "end": q["end"]} for r in reasons]
    return out


def _shorten(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars - 1]
    sent = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sent >= max_chars // 3:
        return cut[:sent + 1].rstrip() + ELLIPSIS
    word = cut.rfind(" ")
    cut = cut[:word] if word > 0 else cut
    return cut.rstrip(" ,;:-") + ELLIPSIS


def trim_quotes(answer: str, max_chars: int = MAX_QUOTE_CHARS) -> str:
    """Shorten every over-long quote (sentence boundary preferred, else word
    boundary) and append an ellipsis; marks and the following [id] are kept."""
    quotes = [q for q in extract_quotes(answer) if len(q["text"]) > max_chars]
    for q in sorted(quotes, key=lambda x: x["start"], reverse=True):
        span = answer[q["start"]:q["end"]]
        i = span.find(q["text"])
        new = span[:i] + _shorten(q["text"], max_chars) + span[i + len(q["text"]):]
        answer = answer[:q["start"]] + new + answer[q["end"]:]
    return answer


def enforce_quotes(answer: str, passages: list[str], max_chars: int = MAX_QUOTE_CHARS) -> tuple[str, list[dict]]:
    """Validate, then fix what is safely fixable. Returns (answer, violations).

    `too_long` and `missing_id` are repaired by trimming (the quote stays,
    shortened, with its citation). `not_verbatim` is NOT rewritten: it is
    reported so the caller can log it, because silently editing a quote the
    model made up would hide the problem. Violations are those found BEFORE
    trimming, so a caller can count and log them.
    """
    violations = validate_quotes(answer, passages, max_chars)
    if any(v["reason"] in ("too_long", "missing_id") for v in violations):
        answer = trim_quotes(answer, max_chars)
    return answer, violations
