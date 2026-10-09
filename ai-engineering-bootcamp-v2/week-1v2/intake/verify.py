"""Verify-only mode support for the gateway (item #96 H-21) and the licence-observation log (item #97).

Nothing here ever logs page text. Page text exists only inside a VerifyHandle (in memory, capped
methods, single process). Audit records carry hashes, counts and fixed codes only.
"""
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


log = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "intake_verify_callers.json"
BASE_DIR = CONFIG_PATH.parent.parent  # week-1v2; caller paths in the config are relative to it
GENESIS = "0" * 64
TOKEN_RE = re.compile(r"\w+")
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
QUOTE_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})
HOST_RE = re.compile(r"[a-z0-9.-]{1,253}")
REQUIRED = ("callers", "verify_max_span_words", "verify_max_releases", "verify_max_calls", "verify_ttl_s",
            "audit_subdir", "release_audit_file", "observation_file", "observation_max_bytes",
            "observation_keep", "portal_map", "second_level_markers", "reason_codes")


class AuditError(Exception):
    """An audit or observation record could not be written; callers fail closed."""


class VerifyError(Exception):
    pass


def load_config(path=None):
    try:
        c = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise VerifyError(f"cannot load verify config: {type(e).__name__}") from e
    missing = [k for k in REQUIRED if k not in c]
    if missing:
        raise VerifyError(f"verify config missing keys: {missing}")
    return c


# ------------------------------------------------------------------ helpers
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_hex(b):
    return hashlib.sha256(b if isinstance(b, bytes) else str(b).encode("utf-8", "replace")).hexdigest()


def _strip_private(o):
    """Drop keys starting with '_' (runtime caches such as licence's compiled regexes) recursively, so the
    hash depends only on the loaded config content, not on whether detect() has run."""
    if isinstance(o, dict):
        return {k: _strip_private(v) for k, v in o.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(o, (list, tuple)):
        return [_strip_private(v) for v in o]
    return o


def config_hash(cfg):
    return sha256_hex(json.dumps(_strip_private(cfg), sort_keys=True, default=repr))[:16]


FIXED_CODE_RE = re.compile(r"[a-z0-9_.:-]{1,64}")


def fixed_code(value):
    """A reason code is a fixed, safe token; anything else becomes 'unspecified' (never a hash of page-derived text)."""
    s = str(value)
    return s if FIXED_CODE_RE.fullmatch(s) else "unspecified"


DEFAULT_MAX_WINDOW_FACTOR = 3.0


def inside_git_root(path):
    p = Path(path).resolve()
    for d in (p, *p.parents):
        g = d / ".git"
        if g.is_file() or (g.is_dir() and (g / "HEAD").exists()):  # worktree file, or a real repository
            return True
    return False


def host_of(url):
    try:
        h = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return h if HOST_RE.fullmatch(h) else ""


def portal_of(host, vcfg):
    for suffix, portal in vcfg["portal_map"].items():
        s = suffix.lower().strip(".")
        if host == s or host.endswith("." + s):
            return portal
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    n = 3 if len(labels[-1]) == 2 and labels[-2] in vcfg["second_level_markers"] else 2
    return ".".join(labels[-n:])


def normalise(s, case_insensitive=False):
    s = unicodedata.normalize("NFKC", str(s)).translate(QUOTE_TRANS)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower() if case_insensitive else s


# ------------------------------------------------------------------ callers
def check_caller(frame, caller, vcfg):
    """Return (ok, reason_code). The calling frame's __file__ must equal the registered path and its
    sha256 must equal the pin. Deny by default."""
    if not caller or not isinstance(caller, str):
        return False, "caller_missing"
    entry = vcfg["callers"].get(caller)
    if not entry:
        return False, "caller_unlisted"
    pin = entry.get("sha256")
    if not pin:
        return False, "caller_unpinned"
    try:
        registered = (BASE_DIR / entry["path"]).resolve()
        actual = Path(frame.f_globals.get("__file__") or "").resolve()
    except (OSError, KeyError, TypeError):
        return False, "caller_path_unresolved"
    if actual != registered:
        return False, "caller_path_mismatch"
    try:
        digest = hashlib.sha256(registered.read_bytes()).hexdigest()
    except OSError:
        return False, "caller_unreadable"
    if digest != pin:
        return False, "caller_hash_mismatch"
    return True, "ok"


# ------------------------------------------------------------------ append-only files
def audit_dir(qdir, vcfg):
    d = Path(qdir) / vcfg["audit_subdir"]
    if d.is_symlink():
        raise AuditError("audit directory is a symlink; refused")
    if inside_git_root(d):
        raise AuditError("audit directory is inside a git repository; refused")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _last_line(fd):
    size = os.fstat(fd).st_size
    if not size:
        return b""
    os.lseek(fd, max(0, size - 65536), os.SEEK_SET)
    data = os.read(fd, 65536)
    lines = [ln for ln in data.split(b"\n") if ln]
    return lines[-1] if lines else b""


def append_chained(path, record):
    """Append record to a hash-chained JSONL file (0600): prev_hash = sha256 of the previous line."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        last = _last_line(fd)
        prev = sha256_hex(last) if last else GENESIS
        line = json.dumps({**record, "prev_hash": prev}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_chain(path):
    """Return (ok, n_lines, first_bad_line or None)."""
    prev, n = GENESIS, 0
    try:
        lines = Path(path).read_bytes().split(b"\n")
    except OSError:
        return False, 0, 1
    for i, ln in enumerate(l for l in lines if l):
        n = i + 1
        try:
            rec = json.loads(ln)
        except ValueError:
            return False, n, n
        if rec.get("prev_hash") != prev:
            return False, n, n
        prev = sha256_hex(ln)
    return True, n, None


def _rotate(path, keep):
    for i in range(keep - 1, 0, -1):
        src = Path(f"{path}.{i}")
        if src.exists():
            os.replace(src, Path(f"{path}.{i + 1}"))
    os.replace(path, Path(f"{path}.1"))
    stale = Path(f"{path}.{keep + 1}")
    if stale.exists():
        stale.unlink()


def append_observation(path, record, max_bytes, keep):
    p = Path(path)
    try:
        if p.exists() and p.stat().st_size >= max_bytes:
            _rotate(p, keep)
    except OSError as e:
        raise AuditError(f"observation rotation failed: {type(e).__name__}") from e
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    finally:
        os.close(fd)


def write_release_audit(qdir, vcfg, record):
    try:
        append_chained(audit_dir(qdir, vcfg) / vcfg["release_audit_file"], record)
    except AuditError:
        raise
    except Exception as e:
        raise AuditError(f"release audit write failed: {type(e).__name__}") from e


def write_observation(qdir, vcfg, record):
    try:
        append_observation(audit_dir(qdir, vcfg) / vcfg["observation_file"], record,
                           vcfg["observation_max_bytes"], vcfg["observation_keep"])
    except AuditError:
        raise
    except Exception as e:
        raise AuditError(f"observation write failed: {type(e).__name__}") from e


def build_observation(*, purpose, url, usha, verdict, reasons, lrep, vcfg, cfg_version, reason_code=""):
    host = host_of(url)
    shown = verdict in ("allow", "label")
    lic = None
    if lrep:
        lic = {"spdx": lrep.get("spdx"), "family": lrep.get("family"), "nc": lrep.get("nc"),
               "nd": lrep.get("nd"), "confidence": lrep.get("confidence"),
               "detected_via": lrep.get("source")}
    return {"obs_id": str(uuid.uuid4()), "ts": _now(), "source_system": "verify-only" if purpose == "verify" else "course-gateway",
            "portal": portal_of(host, vcfg) if host else "", "host": host, "url_sha256": usha,
            "url": str(url) if shown else None, "licence": lic, "decision": verdict,
            "reason_code": fixed_code(reason_code), "config_version": cfg_version}


# ------------------------------------------------------------------ DB mirror of the observation outbox (H-23)
# The JSONL line is the outbox and the record of truth; the DB row is a best-effort mirror. Never raises.
OBS_NS = uuid.UUID("6f1b3a52-9d0e-4c1a-8a57-0b6e3f2c9d41")  # fixed namespace for deriving obs_id of legacy lines
OBS_INSERT_SQL = (
    "INSERT INTO internship.licence_observations (obs_id, observed_at, source_system, portal, host, url_sha256, url, "
    "spdx, family, nc, nd, confidence, detected_via, decision, reason_code, config_version) "
    "VALUES (:obs_id, :observed_at, :source_system, :portal, :host, :url_sha256, :url, "
    ":spdx, :family, :nc, :nd, :confidence, :detected_via, :decision, :reason_code, :config_version) "
    "ON CONFLICT (obs_id) DO NOTHING")


def observation_obs_id(record):
    """The record's own obs_id, or a deterministic uuid5 over (url_sha256, source_system, ts) for legacy lines."""
    if record.get("obs_id"):
        return str(record["obs_id"])
    key = "|".join(str(record.get(k, "")) for k in ("url_sha256", "source_system", "ts"))
    return str(uuid.uuid5(OBS_NS, key))


def observation_row(record):
    """Map one JSONL observation record to INSERT parameters (metadata only; no text columns exist)."""
    lic = record.get("licence") or {}
    return {"obs_id": observation_obs_id(record), "observed_at": record["ts"], "source_system": record["source_system"],
            "portal": record.get("portal") or "", "host": record.get("host") or "", "url_sha256": record["url_sha256"],
            "url": record.get("url"), "spdx": lic.get("spdx"), "family": lic.get("family"), "nc": lic.get("nc"),
            "nd": lic.get("nd"), "confidence": lic.get("confidence"), "detected_via": lic.get("detected_via"),
            "decision": record["decision"], "reason_code": record.get("reason_code"),
            "config_version": record.get("config_version")}


def db_enabled(vcfg):
    return vcfg.get("licence_observations_db_enabled", False) is True


def insert_observation_db(record, vcfg, engine=None):
    """One INSERT through the rw app-account engine with a statement_timeout. Raises on failure."""
    if engine is None:
        from db import get_engine  # lazy: nothing touches the DB module unless the flag is on
        engine = get_engine()
    ms = int(vcfg.get("licence_observations_db_timeout_ms", 2000))
    with engine.begin() as conn:
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {ms}")
        from sqlalchemy import text
        conn.execute(text(OBS_INSERT_SQL), observation_row(record))


def emit_observation_db(record, vcfg, engine=None):
    """Fail-open DB mirror: True if inserted, False if disabled or failed (warning logged, JSONL stays the outbox)."""
    if not db_enabled(vcfg):
        return False
    try:
        insert_observation_db(record, vcfg, engine)
        return True
    except Exception as e:
        log.warning("licence observation DB insert failed (outbox kept) url_sha=%s: %s",
                    str(record.get("url_sha256", ""))[:12], type(e).__name__)
        return False


# ------------------------------------------------------------------ release cap
_lock = threading.Lock()
_releases = 0


def reserve_release(vcfg):
    """Count a release against the per-process cap. Returns False when the cap is exhausted."""
    global _releases
    with _lock:
        if _releases >= vcfg["verify_max_releases"]:
            return False
        _releases += 1
        return True


def _reset_for_tests():
    global _releases
    with _lock:
        _releases = 0


# ------------------------------------------------------------------ the handle
class VerifyHandle:
    """In-memory vetted text with capped query methods. No text in str/repr, not picklable or copyable.
    Not a security boundary against code in the same process (D-025); it limits accidental and
    casual exposure and caps what any legitimate caller can read out."""
    __slots__ = ("__nt", "_cap", "_deadline", "_calls", "_max_calls", "_closed", "_id",
                 "_spans", "_sink", "_wf_max", "__weakref__")

    def __init__(self, text, vcfg, release_id, sink=None):
        object.__setattr__(self, "_VerifyHandle__nt", normalise(text))
        object.__setattr__(self, "_cap", int(vcfg["verify_max_span_words"]))
        object.__setattr__(self, "_deadline", time.monotonic() + vcfg["verify_ttl_s"])
        object.__setattr__(self, "_calls", 0)
        object.__setattr__(self, "_max_calls", int(vcfg["verify_max_calls"]))
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_id", release_id)
        object.__setattr__(self, "_spans", [])
        object.__setattr__(self, "_sink", sink)
        object.__setattr__(self, "_wf_max", float(vcfg.get("verify_max_window_factor", DEFAULT_MAX_WINDOW_FACTOR)))

    def _words(self, max_words):
        """Effective word cap: max_words must be a positive int (or None = the config cap); clamped to [1, cap]."""
        if max_words is None:
            return self._cap
        if isinstance(max_words, bool) or not isinstance(max_words, int) or max_words <= 0:
            raise VerifyError("max_words must be a positive integer")
        return max(1, min(max_words, self._cap))

    def _factor(self, window_factor):
        try:
            f = float(window_factor)
        except (TypeError, ValueError) as e:
            raise VerifyError("window_factor must be a number") from e
        if f != f:
            raise VerifyError("window_factor must be a number")
        return max(1.0, min(f, self._wf_max))

    release_id = property(lambda s: s._id)

    def __repr__(self):
        return f"<VerifyHandle {self._id} closed={self._closed}>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("VerifyHandle cannot be pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("VerifyHandle cannot be pickled")

    def __getstate__(self):
        raise TypeError("VerifyHandle has no exportable state")

    def __copy__(self):
        raise TypeError("VerifyHandle cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("VerifyHandle cannot be copied")

    def __setattr__(self, k, v):
        raise AttributeError("VerifyHandle is read-only")

    def _use(self):
        if self._closed:
            raise VerifyError("verify handle is closed")
        if time.monotonic() > self._deadline:
            self.close()
            raise VerifyError("verify handle expired (ttl)")
        if self._calls >= self._max_calls:
            self.close()
            raise VerifyError("verify handle call limit reached")
        object.__setattr__(self, "_calls", self._calls + 1)

    def _record(self, span):
        w = span.split()
        if w and w[-1] == "...":
            w = w[:-1]
        self._spans.append((sha256_hex(span), len(w)))

    def _view(self, ci):
        return self.__nt.lower() if ci else self.__nt

    def contains(self, norm_quote, case_insensitive=False):
        """True if the (normalised) quote is a substring of the normalised text. Returns a bool only."""
        self._use()
        q = normalise(norm_quote, case_insensitive)
        return bool(q) and q in self._view(case_insensitive)

    def missing_numbers(self, nums):
        """Subset of nums (strings) that do not occur as numbers in the text."""
        self._use()
        have = {n.replace(",", "").rstrip(".") for n in NUM_RE.findall(self.__nt)}
        return sorted(n for n in nums if n not in have)

    def best_span(self, quote, max_words=None, window_factor=1.5, case_insensitive=False):
        """(overlap fraction, span text) of the best-matching window of the text for the quote tokens.
        The span is at most min(max_words, verify_max_span_words) words."""
        self._use()
        cap = self._words(max_words)
        window_factor = self._factor(window_factor)
        t_tokens = TOKEN_RE.findall(self._view(case_insensitive))
        q_tokens = TOKEN_RE.findall(normalise(quote, case_insensitive))
        n = len(q_tokens)
        if n == 0 or not t_tokens:
            return 0.0, ""
        w = min(len(t_tokens), max(n, math.ceil(n * window_factor)))
        need, have, hits, best = Counter(q_tokens), Counter(), 0, (0.0, 0, w)

        def add(t, d):
            nonlocal hits
            if t in need:
                before = min(have[t], need[t])
                have[t] += d
                hits += min(have[t], need[t]) - before

        for i, t in enumerate(t_tokens):
            add(t, 1)
            if i >= w:
                add(t_tokens[i - w], -1)
            if i >= w - 1 and hits / n > best[0]:
                best = (hits / n, i - w + 1, i + 1)
        if len(t_tokens) < w:
            best = (hits / n, 0, len(t_tokens))
        frac, a, b = best
        if frac <= 0:
            return 0.0, ""
        words = t_tokens[a:b]
        sp = " ".join(words[:cap]) + (" ..." if len(words) > cap else "")
        self._record(sp)
        return round(frac, 3), sp

    def passage(self, anchor, max_words=None, case_insensitive=False):
        """Up to max_words (capped) words starting at the first occurrence of anchor, else None."""
        self._use()
        cap = self._words(max_words)
        a = normalise(anchor, case_insensitive)
        v = self._view(case_insensitive)
        i = v.find(a) if a else -1
        if i < 0:
            return None
        words = self.__nt[i:].split()
        sp = " ".join(words[:cap]) + (" ..." if len(words) > cap else "")
        self._record(sp)
        return sp

    def close(self):
        """Wipe the text and write the close record (span hashes and counts only)."""
        if self._closed:
            return
        spans, sink = list(self._spans), self._sink
        object.__setattr__(self, "_closed", True)
        object.__setattr__(self, "_VerifyHandle__nt", "")
        if sink:
            sink([h for h, _ in spans], [n for _, n in spans], self._calls)
