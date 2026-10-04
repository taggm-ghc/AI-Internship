"""Sidebar debug-key box shared by the pages (p3m3 item #62, W2).

The key lives ONLY in st.session_state (widget key below): never written to
disk, never put in a URL or query param, never echoed back. Kept separate
from api_client.py so that module stays Streamlit-free.
"""
import streamlit as st

from api_client import debug_headers

_STATE_KEY = "debug_key_input"


def debug_key_sidebar_widget() -> str:
    st.sidebar.text_input(
        "Debug key",
        type="password",
        key=_STATE_KEY,
        placeholder="(optional: unlocks raw request-level data)",
        help="Held in this browser session only. Never stored, logged or put in the URL.",
    )
    return (st.session_state.get(_STATE_KEY) or "").strip()


def debug_key_headers() -> dict | None:
    return debug_headers(st.session_state.get(_STATE_KEY))


RESTRICTED_MESSAGE = "restricted: enter the debug key"
INVALID_MESSAGE = "invalid key"
