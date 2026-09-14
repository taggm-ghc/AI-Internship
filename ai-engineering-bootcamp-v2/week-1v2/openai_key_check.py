"""OPENAI_API_KEY format classification: catches a missing, placeholder, or
malformed key locally, before spending a network call to find out OpenAI
will reject it too.

Ported 2026-09-13 from ../ai-eng-bootcamp.vera/vera/auth.py — only the
OpenAI-key half. That file also has a second, separate concern: a
VERA_API_KEY header check that authenticates *callers of VERA's own /ask*
(app-level auth, independent of the OpenAI key). This project's /ask has no
such caller-auth layer, so that half wasn't ported — see the checklist for
the research on whether it's worth adding.
"""
import re

_OPENAI_KEY_PLACEHOLDERS = {
    "your-api-key-here", "your_api_key_here", "your-openai-api-key",
    "youropenaikey", "changeme", "change-me", "change_me", "todo", "fixme",
    "placeholder", "insert-key-here", "insert_your_key_here", "api-key-here",
    "<your-api-key>", "<api_key>", "none", "null", "n/a", "openai_api_key",
}
_OPENAI_KEY_PLACEHOLDER_RE = re.compile(r"^sk-x{10,}$", re.IGNORECASE)
_OPENAI_KEY_MIN_LENGTH = 40  # real OpenAI keys run 50+ chars; this is a floor, not a format check


def classify_openai_api_key(key: str | None) -> str:
    """Classify OPENAI_API_KEY as 'missing', 'placeholder', 'malformed', or 'ok'."""
    if key is None:
        return "missing"
    key = key.strip()
    if not key:
        return "missing"
    if key.lower() in _OPENAI_KEY_PLACEHOLDERS or _OPENAI_KEY_PLACEHOLDER_RE.match(key):
        return "placeholder"
    if not key.startswith("sk-") or len(key) < _OPENAI_KEY_MIN_LENGTH:
        return "malformed"
    return "ok"


OPENAI_KEY_ERROR_DETAIL = {
    "missing": "Server misconfigured: OPENAI_API_KEY is not set.",
    "placeholder": "Server misconfigured: OPENAI_API_KEY still holds a placeholder value.",
    "malformed": "Server misconfigured: OPENAI_API_KEY does not look like a valid OpenAI key.",
}
