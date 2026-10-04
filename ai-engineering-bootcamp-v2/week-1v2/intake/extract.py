"""Gate 2: untrusted bytes -> visible plain text (+ everything that was dropped).

Limits: white-on-white / same-colour-as-background text, text hidden by external
CSS classes, clipping (height:0; overflow:hidden), z-index/overlap and
canvas/SVG/image text are NOT detectable here without rendering.
"""
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from .safe import token

PDF_WORKER = Path(__file__).resolve().parent / "_pdf_worker.py"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "intake_detectors.json"


class ExtractError(ValueError):
    pass


def load_config(path=None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtractError(f"cannot load intake config {p}: {exc}") from exc


def _ecfg(cfg: dict) -> dict:
    return cfg.get("extract", cfg)


def is_invisible(ch: str, categories, ranges=()) -> bool:
    cp = ord(ch)
    return unicodedata.category(ch) in categories or any(lo <= cp <= hi for lo, hi in ranges)


def strip_invisible(text: str, categories, ranges=()) -> tuple[str, int]:
    kept = [ch for ch in text if not is_invisible(ch, categories, ranges)]
    return "".join(kept), len(text) - len(kept)


def _num(value: str):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", value)
    return float(m.group(1)) if m else None


def _style_hidden(style: str, rules: list) -> str | None:
    decls = {}
    for part in style.lower().split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip()] = v.replace("!important", "").strip()
    for rule in rules:
        v = decls.get(rule["prop"])
        if v is None:
            continue
        if "values" in rule and v in rule["values"]:
            return f"{rule['prop']}:{v}"
        n = _num(v)
        if n is not None and (("eq" in rule and n == rule["eq"]) or ("lt" in rule and n < rule["lt"])):
            return f"{rule['prop']}:{v}"
    return None


class _Extractor(HTMLParser):
    def __init__(self, ecfg: dict):
        super().__init__(convert_charrefs=True)
        self.c = ecfg
        self.stack: list[list] = []  # [tag, hidden_reason]
        self.raw: list[str] = []
        self.visible: list[str] = []
        self.dropped: dict[str, int] = {}

    def _drop(self, cat: str, text: str, raw: bool):
        if not text.strip():
            return
        self.dropped[cat] = self.dropped.get(cat, 0) + len(text)
        if raw:
            self.raw.append("\n" + text.strip() + "\n")

    def _in(self, tag: str) -> bool:
        return any(t == tag for t, _ in self.stack)

    def _hidden(self) -> bool:
        return any(r for _, r in self.stack)

    def _hidden_reason(self, tag: str, attrs: dict) -> str | None:
        c = self.c
        for a in c["hidden_attrs"]:
            if a in attrs:
                return a
        for spec in c["hidden_attr_values"]:
            if spec.get("tags") and tag not in spec["tags"]:
                continue
            if (attrs.get(spec["attr"]) or "").strip().lower() in spec["values"]:
                return f"{spec['attr']}"
        if tag in c["hidden_tags"]:
            return tag
        return _style_hidden(attrs.get("style") or "", c["style_rules"])

    def _attr_text(self, tag: str, attrs: dict):
        c = self.c
        for k, v in attrs.items():
            if not v:
                continue
            if k in c["text_attrs"] or any(k.startswith(p) for p in c["text_attr_prefixes"]):
                self._drop("attributes", v, True)
            elif tag in c["meta_tags"] and k == "content":
                self._drop("head", v, True)

    def _block(self):
        self.visible.append("\n")
        self.raw.append("\n")

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v for k, v in attrs}
        self._attr_text(tag, a)
        if tag in self.c["void_tags"]:
            if tag in self.c["block_tags"] and not self._hidden():
                self._block()
            return
        if tag in self.c["auto_close_tags"] and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        self.stack.append([tag, self._hidden_reason(tag, a)])
        if tag in self.c["block_tags"] and not self._hidden():
            self._block()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.c["void_tags"]:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in self.c["block_tags"] and not self._hidden():
            self._block()

    def handle_comment(self, data):
        self._drop("comments", data, True)

    def handle_data(self, data):
        c = self.c
        if any(t in c["drop_tags"] for t, _ in self.stack):
            self._drop("script_style", data, False)
        elif self._in("head") and not self._in("title"):
            self._drop("head", data, True)
        elif self._hidden():
            self._drop("hidden_elements", data, True)
        else:
            self.visible.append(data)
            self.raw.append(data)


def _tidy(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _finish(visible: str, raw: str, dropped: dict, ecfg: dict) -> dict:
    cats = set(ecfg["invisible_categories"])
    text, n_inv = strip_invisible(visible, cats, ecfg.get("invisible_ranges", []))
    raw_clean = raw
    text = _tidy(text)
    raw_text = raw_clean.replace("\x00", "�")
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
    if n_inv:
        dropped["invisible_unicode"] = n_inv
    if len(text) > ecfg["max_chars"]:
        dropped["truncated_chars"] = len(text) - ecfg["max_chars"]
        text = text[: ecfg["max_chars"]]
    raw_text = raw_text[: ecfg["max_raw_chars"]]
    return {"text": text, "raw_text": raw_text, "raw_text_len": len(raw_text), "dropped": dropped, "reject": None}


def _rejected(reason: str) -> dict:
    return {"text": "", "raw_text": "", "raw_text_len": 0, "dropped": {}, "reject": reason}


def to_visible_text(raw: bytes, content_type: str, cfg: dict, workdir=None) -> dict:
    """Returns {text, raw_text, raw_text_len, dropped, reject}. raw_text keeps
    visible + hidden/comment/attribute text (invisible Unicode still present)
    for the detectors; text is the cleaned visible text. reject is a reason
    string (fixed code, never input text) when the input cannot be safely converted,
    else None. workdir: private directory for the PDF temp file (required for PDFs)."""
    ecfg = _ecfg(cfg)
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not isinstance(raw, (bytes, bytearray)):
        raise ExtractError(f"raw must be bytes, got {type(raw).__name__}")
    dropped = {}
    if len(raw) > ecfg["max_input_bytes"]:
        dropped["truncated_bytes"] = len(raw) - ecfg["max_input_bytes"]
        raw = raw[: ecfg["max_input_bytes"]]
    if ctype in ecfg["pdf_types"]:
        return _pdf(bytes(raw), ecfg, dropped, workdir)
    decoded = bytes(raw).decode("utf-8", errors="replace")
    if ctype in ecfg["html_types"]:
        p = _Extractor(ecfg)
        p.feed(decoded)
        p.close()
        res = _finish("".join(p.visible), "".join(p.raw), {**dropped, **p.dropped}, ecfg)
        return res
    if ctype in ecfg["text_types"]:
        return _finish(decoded, decoded, dropped, ecfg)
    return _rejected(f"unsupported content type {token(ctype)}; allowed: "
                     f"{ecfg['html_types'] + ecfg['pdf_types'] + ecfg['text_types']}")


def _limiter(pcfg: dict):
    def apply():
        mb = pcfg["rlimit_as_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mb, mb))
        resource.setrlimit(resource.RLIMIT_CPU, (pcfg["rlimit_cpu_s"], pcfg["rlimit_cpu_s"]))
    return apply


def _pdf(raw: bytes, ecfg: dict, dropped: dict, workdir) -> dict:
    """Parse in a child process (timeout + RLIMIT_AS/CPU); child errors never reach logs or results."""
    if workdir is None:
        return _rejected("pdf_no_workdir")
    pcfg = ecfg["pdf"]
    fd, name = tempfile.mkstemp(dir=workdir, prefix=".pdf-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        try:
            proc = subprocess.run([sys.executable, "-I", str(PDF_WORKER), name], capture_output=True,
                                  timeout=pcfg["timeout_s"], env={}, preexec_fn=_limiter(pcfg), check=False)
        except subprocess.TimeoutExpired:
            return _rejected("pdf_worker_timeout")
        except OSError:
            return _rejected("pdf_worker_unavailable")
        if proc.returncode != 0:
            return _rejected("pdf_worker_failed")
        text = proc.stdout.decode("utf-8", errors="replace")
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
    return _finish(text, text, dropped, ecfg)
