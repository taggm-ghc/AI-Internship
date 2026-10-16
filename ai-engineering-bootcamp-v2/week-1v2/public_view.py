"""Public-view projection for the Ask page (p3m3 item 9).

Everything the PUBLIC Ask page shows is built here by COPYING allow-listed
fields out of an API response, never by deleting internal ones. A field the
tables below do not know is never displayed (it fails closed on disclosure);
its NAME (never its value) is written to the server log so the maintainers
notice, and the schema-classification test fails until someone classifies it.

No Streamlit and no `main` import: pure functions, testable offline. The
`/ask` API contract is untouched (this filters what the Streamlit server
sends to the browser, not what the API returns).
"""
from __future__ import annotations

import html
import ipaddress
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote, urlsplit

logger = logging.getLogger("public_view")

Cls = Literal["public", "internal"]

# One row per field of main.AskResponse / ask_service.Answer / main.AttemptResult.
# "public" fields are the only ones ever copied. tests/test_public_view.py
# compares these keys with the live pydantic models: adding an API field makes
# that test fail until the field is reviewed and classified here.
ASK_FIELDS: dict[str, Cls] = {
    "answer": "public",              # container; only answer.answer is copied
    "tokens_used": "internal",       # usage telemetry
    "prompt_tokens": "internal",
    "completion_tokens": "internal",
    "model": "internal",             # provider:model, can carry a LAN host
    "latency_ms": "internal",
    "cost_usd": "internal",
    "input_cost_usd": "internal",
    "output_cost_usd": "internal",
    "free_tier_note": "internal",    # names the provider
    "attempts": "internal",          # retry trace, raw model output, validation text
    "citations": "internal",         # raw chunk ids; only len() is shown
    "references": "public",          # APA entries, sanitized field by field
    "status": "public",              # three literals only
    "embedding_cost_usd": "internal",
    "rag_mode": "public",            # echo of the user's own choice
    "skills_used": "internal",       # back-end / development metadata
}
ANSWER_FIELDS: dict[str, Cls] = {
    "answer": "public",
    "confidence": "internal",        # the model's self-reported score
    "sources_needed": "internal",    # internal refusal flag; status covers it
    "used_passage_numbers": "internal",
}
ATTEMPT_FIELDS: dict[str, Cls] = {
    "attempt": "internal",
    "step": "internal",
    "ok": "internal",
    "message": "internal",
    "raw_output": "internal",
    "validation_error": "internal",
}
PUBLIC_STATUS = frozenset({"supported", "insufficient", "not_applicable"})
PUBLIC_RAG_MODES = frozenset({"auto", "force_rag", "no_rag"})

WITHHELD_MESSAGE = "This response could not be shown because its format was not recognised."

# status class -> fixed text. Nothing from the API, an upstream or api_client
# ever passes through (not even truncated).
PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "unreachable": "The API did not respond. If it was idle it can take up to a minute to wake; try again.",
    "bad_request": "The request was not accepted. Check the options and try again.",
    "unavailable": "The AI service is unavailable right now.",
    "rate_limited": "Too many requests. Wait a few seconds and retry.",
    "provider_failed": "The AI provider request failed. Try again later.",
    "other": "Request failed.",
}


def error_class(http_status: int | None) -> str:
    s = http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else 0
    if s == 0:
        return "unreachable"
    if s in (400, 422):
        return "bad_request"
    if s in (401, 402, 403):
        return "unavailable"
    if s == 429:
        return "rate_limited"
    if 500 <= s <= 599:
        return "provider_failed"
    return "other"


# Exact format written by main._provider_failure_detail. A match yields only
# the 12-hex support reference, never the rest of the string.
_REFERENCE_RE = re.compile(r"The AI provider request failed: [A-Za-z ]+\. Reference: ([0-9a-f]{12})")


def public_error_message(http_status: int | None, data: object = None) -> str:
    message = PUBLIC_ERROR_MESSAGES[error_class(http_status)]
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str):
        m = _REFERENCE_RE.fullmatch(detail)
        if m:
            message += f" Reference: {m.group(1)}"
    return message


def _log_unknown(where: str, keys: list) -> None:
    # Names only, sanitised and capped; values are never logged.
    names = sorted({re.sub(r"[^A-Za-z0-9_]", "?", str(k))[:40] for k in keys})[:20]
    if names:
        logger.warning("public_view: unclassified API field(s) ignored at %s: %s", where, ", ".join(names))


# ---- reference sanitising -------------------------------------------------

_CHUNK_ID_RE = re.compile(r"\S+::\d+")
_IPV4_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_IPV6_RE = re.compile(r"\[[0-9A-Fa-f:.]{2,}\]")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s)\]>\"'<]*")
_BARE_SLASH_RE = re.compile(r"(?<![:\w/])//[^\s)\]>\"'<]+")  # scheme-relative URL
_MD_TARGET_RE = re.compile(r"\]\(\s*<?([^)\s>]*)")                 # inline link, titles/leading spaces ok
_MD_REFDEF_RE = re.compile(r"(?m)^\s{0,3}\[[^\]]*\]:\s*<?(\S+)")     # reference-style definition
_BAD_SCHEME_RE = re.compile(r"(?<![A-Za-z0-9])(?:javascript|data|vbscript|file|mailto|ftp|tel|blob|http):", re.I)
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")
_HTML_TAG_RE = re.compile(r"<[A-Za-z/!?]")
_DOI_RE = re.compile(r"doi:10\.\d{4,9}/\S+", re.I)
_HOST_CHARS_RE = re.compile(r"[a-z0-9.\-]+")
_EMBEDDED_IPV4_RE = re.compile(r"\d{1,3}[.\-]\d{1,3}[.\-]\d{1,3}[.\-]\d{1,3}")
_WILDCARD_DNS = ("nip.io", "sslip.io", "xip.io", "traefik.me", "localtest.me", "lvh.me", "local.gd", "vcap.me",
                 "1u.ms", "lacolhost.com", "burpcollaborator.net", "oast.fun", "oast.me", "oast.pro")
_LOCAL_SUFFIXES = (".localdomain", ".localhost", ".local", ".internal", ".lan", ".home", ".corp")


def _api_host(api_host: str | None) -> str:
    h = (api_host or "").strip().lower()
    if "://" in h:
        h = (urlsplit(h).hostname or "")
    return h.rstrip(".")


def _bad_chars(text: str) -> bool:
    """Control characters (NUL, CR, ...) other than newline/tab, and invisible
    format characters (zero-width etc.), are never allowed."""
    return any((unicodedata.category(c) == "Cc" and c not in "\n\t") or unicodedata.category(c) in ("Cf", "Cs")
               for c in text)


def _host_ok(host: str, host_blocked: str) -> bool:
    host = host.rstrip(".")
    if not host or not _HOST_CHARS_RE.fullmatch(host) or "." not in host:
        return False  # empty, '%', non-ASCII, underscores, single labels
    labels = host.split(".")
    if any(not l or l.startswith("-") or l.endswith("-") or l.startswith("xn--") for l in labels):
        return False
    last = labels[-1]
    if last.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last) or any(re.fullmatch(r"0x[0-9a-f]*", l) for l in labels):
        return False  # numeric / hex / decimal IPv4 forms (127.1, 0x7f.0.0.1, 2130706433)
    if host.startswith("localhost") or host.endswith(_LOCAL_SUFFIXES):
        return False
    if host.endswith(tuple("." + d for d in _WILDCARD_DNS)) or host in _WILDCARD_DNS or _EMBEDDED_IPV4_RE.search(host):
        return False
    if host_blocked and (host == host_blocked or host.endswith("." + host_blocked)):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _url_ok(url: str, host_blocked: str) -> bool:
    if _bad_chars(url) or any(c.isspace() for c in url) or not url.isascii() or "\\" in url:
        return False
    if _DOI_RE.fullmatch(url):
        return True
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port is not None or ":" in parts.netloc:
        return False
    return _host_ok((parts.hostname or "").lower(), host_blocked)


_WWW_RE = re.compile(r"(?i)(?<![\w/.@])www\.[^\s<>()]+")
_EMAIL_RE = re.compile(r"\S+@[\w.-]+\.\w+")


def _reference_text_ok(text: str, host_blocked: str) -> bool:
    if not text.strip() or len(text) > 2000 or _bad_chars(text):
        return False
    norm = re.sub("[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]", "-", unicodedata.normalize("NFKC", text))
    low = norm.lower()
    if _CHUNK_ID_RE.search(norm) or "local-inference" in low or "localhost" in low:
        return False
    if _IPV4_RE.search(norm) or _IPV6_RE.search(norm) or re.search(r"@[\w.\-]+:\d+", norm):
        return False
    if host_blocked and host_blocked in low:
        return False
    if _BAD_SCHEME_RE.search(norm) or _ENTITY_RE.search(text) or _HTML_TAG_RE.search(text) or "![" in text:
        return False
    # Bare "www." hosts and e-mail addresses are GFM autolink candidates: the
    # host must pass the same checks as a URL host, and e-mail shapes never pass.
    if _EMAIL_RE.search(norm):
        return False
    for token in _WWW_RE.findall(norm):
        if not _host_ok(re.split(r"[/?#:]", token, 1)[0].rstrip(".,;!").lower(), host_blocked):
            return False
    for target in _MD_TARGET_RE.findall(text) + _MD_REFDEF_RE.findall(text):
        if not _url_ok(target, host_blocked):
            return False
    if _BARE_SLASH_RE.search(text):
        return False
    for url in _URL_RE.findall(text):
        if not _url_ok(url.rstrip(".,;"), host_blocked):
            return False
    return True


def public_references(refs: object, api_host: str | None = None) -> tuple[list[str], int]:
    """Return (kept reference strings in order, number withheld).

    Each item must be a str, or a dict whose keys are only {"text", "url"}
    with str values (a url is appended as a markdown link). Anything else, or
    an entry failing the text/URL checks, is dropped and counted."""
    if not isinstance(refs, list):
        return [], (0 if refs is None else 1)
    host = _api_host(api_host)
    kept: list[str] = []
    withheld = 0
    for item in refs:
        text: str | None = None
        if isinstance(item, str) and len(item) <= MAX_REFERENCE_CHARS:
            text = item
        elif isinstance(item, dict) and set(item) <= {"text", "url"} and "text" in item \
                and all(isinstance(v, str) and len(v) <= MAX_REFERENCE_CHARS for v in item.values()):
            text = item["text"]
            if item.get("url"):
                if not _url_ok(item["url"], host):
                    text = None
                else:
                    text = f"{text} {item['url']}"
        if text is not None:
            if _bad_chars(text):  # before collapsing: CR, NUL, zero-width etc. must still reject
                text = None
            else:
                text = " ".join(text.split())  # no newlines: a blank line would end the widget's HTML block
        if text is not None and _reference_text_ok(text, host):
            kept.append(text)
        else:
            withheld += 1
    return kept, withheld


# ---- answer / streamed-answer Markdown (SF10) -----------------------------
# The model answer is rendered as Markdown. Attacker-influenced text (prompt
# injection through retrieved passages) must not make the viewer's browser fetch
# a URL (images), render raw HTML, or autolink an internal/wildcard host.

_ALT = r"(?:[^\[\]]|\[[^\]]*\])*"
_PAREN = r"(?:[^()]|\([^()]*\))*"
_IMG_INLINE_RE = re.compile(rf"!\s*\[{_ALT}\]\s*\({_PAREN}\)")
_IMG_REF_RE = re.compile(rf"!\s*\[({_ALT})\][ \t]*\[([^\]]*)\]")
_IMG_SHORT_RE = re.compile(rf"!\s*\[({_ALT})\]")
_DEF_RE = re.compile(r"(?m)^[ ]{0,3}\[([^\]\n]+)\]:[ \t]*<?([^\s>]*)>?.*$\n?")
_LINK_RE = re.compile(rf"\[({_ALT})\]\(\s*(<[^>]*>|(?:[^\s()]|\([^\s()]*\))*)((?:\s+(?:\"[^\"]*\"|'[^']*'))?)\s*\)")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_PI_RE = re.compile(r"<\?.*?\?>", re.S)
_DECL_RE = re.compile(r"<![^>]*>")
_TAG_RE = re.compile(r"<(/?[A-Za-z][^<>]*)>")
_AUTOLINK_INNER_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:\S*")
_STRAY_LT_RE = re.compile(r"<(?=[A-Za-z/!?])(?![A-Za-z][A-Za-z0-9+.\-]*:[^\s<>]*>)")
_BARE_EMAIL_RE = re.compile(r"(?<![\w.+\-])[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def _target_ok(target: str, host_blocked: str) -> bool:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return bool(target) and _url_ok(target, host_blocked) and _url_ok(unquote(target), host_blocked)


MAX_ANSWER_CHARS = 20000   # sanitiser input cap (linear-time regexes + this bound = no ReDoS)
MAX_REFERENCE_CHARS = 2000
TRUNCATION_NOTICE = "\n\n(Answer truncated for display.)"
_ESCAPE_ALL_RE = re.compile(r"([!-/:-@\[-`{-~])")


def _escape_plain(text: str) -> str:
    """Fallback: every ASCII punctuation mark backslash-escaped, so nothing in
    the text can be parsed as an image, link, autolink, HTML or definition."""
    return _ESCAPE_ALL_RE.sub(r"\\\1", text)


def _regex_unsafe(text: str, host: str) -> bool:
    """Used only if markdown-it-py is not importable: any link-ish construct
    (`](`, `]:`, `<`) with a target that fails _url_ok means escape everything."""
    targets = [m for m in re.findall(r"\]\(\s*<?([^)\s>]*)", text)]
    targets += re.findall(r"\]:\s*<?([^\s>]+)", text)
    targets += re.findall(r"<([^<>\s]+)>", text)
    if "<" in text and not re.fullmatch(r"(?:[^<]|<(?=[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>]*>))*", text):
        return True
    return any(not _target_ok(t, host) for t in targets)


def _markdown_unsafe(text: str, host: str) -> bool:
    """Final verification: parse with CommonMark (+tables/strikethrough). True if
    an image, an HTML token, or a link/autolink whose href fails _url_ok survives."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return _regex_unsafe(text, host)
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    md.validateLink = lambda url: True  # the default hides javascript:/vbscript:/file:/data: from the check

    def walk(tokens) -> bool:
        for t in tokens:
            if t.type in ("image", "html_inline", "html_block"):
                return True
            if t.type == "link_open":
                href = str(t.attrs.get("href", ""))
                if not (_url_ok(href, host) and _url_ok(unquote(href), host)):
                    return True
            if t.children and walk(t.children):
                return True
        return False
    return walk(md.parse(text))


_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_URL_END = frozenset(" \t\r\n<>")
_MAX_BARE_URL = 2000


def _break_bare_urls(text: str, host: str) -> str:
    """Linear scan: for every "://", take the whole run of scheme-like characters
    before it and the non-space run after it. Anything but a plain https URL that
    passes _url_ok gets its colon backslash-escaped (so GFM will not autolink it)."""
    out: list[str] = []
    pos = 0
    nxt = 0
    safe_end = 0
    for m in re.finditer(r"://", text):
        i = m.start()
        if i < pos or (i > 0 and text[i - 1] == "\\"):  # behind us, or already escaped: never stack escapes
            continue
        if i < safe_end:  # inside the query/path of a kept safe https URL: no backslash (it would break the outer URL)
            out.append(text[pos:i])
            out.append("%3A")
            pos = i + 1
            continue
        j = i
        while j > pos and text[j - 1] in _SCHEME_CHARS:
            j -= 1
        if nxt < m.end():  # next URL end at/after here; one forward pass overall
            nxt = m.end()
            while nxt < len(text) and text[nxt] not in _URL_END:
                nxt += 1
        too_long = nxt - m.end() > _MAX_BARE_URL
        k = min(nxt, m.end() + _MAX_BARE_URL)
        scheme, token = text[j:i], text[j:k]
        if scheme.lower() == "https" and not too_long and _url_ok(token.replace("\\:", ":").rstrip(".,;:!?"), host):
            safe_end = k
            continue
        out.append(text[pos:i])
        out.append("\\:")
        pos = i + 1
    out.append(text[pos:])
    return "".join(out)


def _answer_pass(text: str, host: str) -> str:
    text = "".join(chr(ord(c) - 0xFEE0) if "\uff01" <= c <= "\uff5e" else c for c in text)
    text = "".join(c for c in text if not ((unicodedata.category(c) == "Cc" and c not in "\n\t")
                                           or unicodedata.category(c) in ("Cf", "Cs")))
    text = html.unescape(text)
    # raw HTML
    text = _COMMENT_RE.sub("", text)
    text = _PI_RE.sub("", text)
    text = _DECL_RE.sub("", text)

    def tag(m):
        inner = m.group(1)
        if _AUTOLINK_INNER_RE.fullmatch(inner):
            return m.group(0) if _target_ok(inner, host) else ""
        return ""
    text = _TAG_RE.sub(tag, text)
    text = _STRAY_LT_RE.sub("", text)
    # images, all forms, and the definitions they used; unsafe other definitions
    labels: set[str] = set()

    def norm_label(x):
        return " ".join(x.split()).lower()

    text = _IMG_INLINE_RE.sub("", text)
    text = _IMG_REF_RE.sub(lambda m: labels.add(norm_label(m.group(2) or m.group(1))) or "", text)
    text = _IMG_SHORT_RE.sub(lambda m: labels.add(norm_label(m.group(1))) or "", text)
    text = _DEF_RE.sub(lambda m: m.group(0) if norm_label(m.group(1)) not in labels and _target_ok(m.group(2), host) else "", text)
    # links: keep the text, drop an unsafe target
    text = _LINK_RE.sub(lambda m: m.group(0) if _target_ok(m.group(2), host) else m.group(1), text)

    # bare autolink candidates: break the unsafe ones so GFM will not link them
    text = _break_bare_urls(text, host)
    text = _WWW_RE.sub(lambda m: m.group(0) if _host_ok(re.split(r"[/?#:]", m.group(0), 1)[0].rstrip(".,;!").lower(), host)
                       else m.group(0).replace(".", "\\.", 1), text)
    text = _BARE_EMAIL_RE.sub(lambda m: m.group(0).replace("@", "\\@", 1), text)
    return text


def public_answer_markdown(text: str, api_host: str | None = None) -> str:
    """Markdown-safe form of a model answer (also used for streamed text).
    Input is capped at MAX_ANSWER_CHARS. Keeps ordinary Markdown and links whose
    target passes _url_ok (https/doi); removes images, raw HTML, entity/percent/
    full-width/zero-width variants and unsafe link targets; breaks bare autolinks
    of unsafe hosts and all emails. Runs to a fixed point (max 5 passes), then a
    CommonMark parse verifies the result; if anything unsafe survives, the whole
    answer is returned as fully escaped plain text (words kept)."""
    if not isinstance(text, str):
        return ""
    host = _api_host(api_host)
    truncated = len(text) > MAX_ANSWER_CHARS
    if truncated:
        text = text[:MAX_ANSWER_CHARS]
    for _ in range(5):
        new = _answer_pass(text, host)
        if new == text:
            break
        text = new
    if _markdown_unsafe(text, host):
        text = _escape_plain(text)
    return text + (TRUNCATION_NOTICE if truncated else "")


# ---- the view -------------------------------------------------------------


@dataclass
class PublicView:
    kind: Literal["answer", "error", "withheld"]
    http_status: int = 0
    answer_text: str = ""
    status: str | None = None
    rag_mode: str | None = None
    citation_count: int = 0
    reference_count: int = 0
    references: list[str] = field(default_factory=list)
    references_withheld: int = 0
    message: str = ""
    panel: dict = field(default_factory=dict)


def public_ask_view(http_status: int | None, data: object, api_host: str | None = None) -> PublicView:
    code = http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else 0
    is_api_error = isinstance(data, dict) and "error" in data and "answer" not in data
    if code != 200 or is_api_error or not isinstance(data, dict):
        if code == 200:  # 200 with an unusable body: not an answer we can show
            code = 0 if is_api_error else 500
        msg = public_error_message(code, data)
        return PublicView(kind="error", http_status=code, message=msg,
                          panel={"http_status": code, "error": msg})

    _log_unknown("response", [k for k in data if k not in ASK_FIELDS])
    ans = data.get("answer")
    if isinstance(ans, dict):
        _log_unknown("response.answer", [k for k in ans if k not in ANSWER_FIELDS])
    text = ans.get("answer") if isinstance(ans, dict) else None
    if not isinstance(text, str):
        return PublicView(kind="withheld", http_status=code, message=WITHHELD_MESSAGE,
                          panel={"withheld": True, "message": WITHHELD_MESSAGE})
    attempts = data.get("attempts")
    if isinstance(attempts, list):
        _log_unknown("response.attempts[]", [k for a in attempts if isinstance(a, dict) for k in a
                                             if k not in ATTEMPT_FIELDS])

    status = data.get("status")
    status = status if isinstance(status, str) and status in PUBLIC_STATUS else None
    rag_mode = data.get("rag_mode")
    rag_mode = rag_mode if isinstance(rag_mode, str) and rag_mode in PUBLIC_RAG_MODES else None
    citations = data.get("citations")
    citation_count = len(citations) if isinstance(citations, list) else 0
    refs, withheld = public_references(data.get("references"), api_host)
    answer_md = public_answer_markdown(text, api_host)  # sanitise once; panel and page share it
    return PublicView(
        kind="answer", http_status=code, answer_text=answer_md, status=status, rag_mode=rag_mode,
        citation_count=citation_count, reference_count=len(refs), references=refs,
        references_withheld=withheld,
        panel={"answer": answer_md, "status": status, "rag_mode": rag_mode,
               "citation_count": citation_count, "reference_count": len(refs)},
    )


@dataclass
class PublicStreamView:
    ok: bool
    http_status: int = 0
    text: str = ""
    grounded: bool | None = None
    citation_count: int = 0
    message: str = ""


def public_stream_view(http_status: int | None, text: object, grounded: object = None,
                       citations: object = None, api_host: str | None = None) -> PublicStreamView:
    """Streamed answer view: text on 200 only; never served_by, skills or chunk ids."""
    import json
    code = http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else 0
    if code == 200 and isinstance(text, str):
        return PublicStreamView(
            ok=True, http_status=200, text=public_answer_markdown(text, api_host),
            grounded=grounded if isinstance(grounded, bool) else None,
            citation_count=len(citations) if isinstance(citations, list) else 0,
        )
    body = None
    if isinstance(text, str):
        try:
            body = json.loads(text)
        except ValueError:
            body = None
    if code == 200:
        code = 500
    return PublicStreamView(ok=False, http_status=code, message=public_error_message(code, body))


# ---- development-only controls ---------------------------------------------

AUTO_PROVIDER_LABEL = "Auto (free-tier-first)"


def provider_choices(entries: object, dev_enabled: bool) -> list[str]:
    """Provider selector options. Public: Auto only (no inventory). Development
    (server-verified debug key): Auto, openai and configured providers."""
    if not dev_enabled:
        return [AUTO_PROVIDER_LABEL]
    names = sorted({
        e["provider"] for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict) and isinstance(e.get("provider"), str)
        and e.get("provider") != "openai" and str(e.get("status", "")).startswith("configured")
    })
    return [AUTO_PROVIDER_LABEL, "openai"] + names
