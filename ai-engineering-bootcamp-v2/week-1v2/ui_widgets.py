"""Shared Streamlit UI widgets, imported by every page that makes its own
API calls (MVP_Layered_Ask.py, pages/1_Observability_Dashboard.py,
pages/2_MVP_Layered_Health.py) -- p3m3 permanent item #26. Distinct from
api_client.py (pure Python, no Streamlit imports, explicitly side-effect
free per that module's own docstring) and ui_theme.py (CSS only): this
one is Streamlit-aware by design, same three-module split rag_service.py/
operational_store.py already model for server-side code.
"""
import html
import re

import streamlit as st

from api_client import default_api_base_url


def base_url_sidebar_widget() -> str:
    """Renders the sidebar's API-base-URL override, added 2026-09-24 to
    replace three copies of `st.sidebar.text_input("API base URL",
    default_api_base_url())` -- that pre-filled the field with the
    actual deployed API URL by default, which every screenshot sent this
    session leaked directly, contradicting this project's own README
    rule ("never share your live URL publicly... anyone with the URL can
    spend your API credits"). Renamed "Custom API base URL", blank by
    default (a placeholder explains the fallback instead of showing the
    real value), so a screenshot of this field alone can't leak
    anything. The override still works exactly as before when someone
    actually types a URL in -- this only changes what's shown, not what
    the app is capable of."""
    typed = st.sidebar.text_input(
        "Custom API base URL",
        value="",
        placeholder="(leave blank to use the configured API)",
        help=(
            "Overrides which API this page talks to -- e.g. to point this UI "
            "at your own local API instead of the deployed one. Leave blank "
            "to use whatever this UI is already configured for; that URL is "
            "deliberately never shown here, so a screenshot of this field "
            "can't leak it."
        ),
    )
    return typed.strip() or default_api_base_url()


def references_widget(references: list[str]) -> None:
    """APA 7 reference list (p3m3 item #48), shared by the Ask and Agent
    pages. Entries arrive as Markdown (*italics*) from citations.py; each is
    HTML-escaped, then its italics are converted, and it is rendered with
    APA's hanging indent, which plain Markdown can't express."""
    if not references:
        return
    st.markdown("**References**")
    for entry in references:
        safe = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", html.escape(entry))
        st.markdown(f'<div style="padding-left:2em;text-indent:-2em;margin-bottom:0.4em">{safe}</div>',
                    unsafe_allow_html=True)
