"""Sanitisers for attacker-influenced values that must reach logs or results."""
import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9/.+-]{0,64}")


def token(value) -> str:
    """Return value if it is already a short safe token, else 'sha256:<12 hex>' of it."""
    s = str(value)
    if _TOKEN.fullmatch(s):
        return s
    return "sha256:" + hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12]
