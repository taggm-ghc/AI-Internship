"""Shared Streamlit CSS override, imported by every page (MVP_Layered_Ask.py,
pages/1_Observability_Dashboard.py, pages/2_MVP_Layered_Health.py) so a
typography fix applies once, not three drifting copies -- same
component-reuse discipline api_client.py established.

Selectors below were extracted directly from the installed Streamlit
1.63.0 frontend bundle (grepped stCaptionContainer/stHeadingWithAction
Elements/stMarkdownContainer out of the compiled JS -- Streamlit's
*.css-hash* class names are unstable across versions, but its
data-testid attributes are the documented, stable styling hook), not
guessed from an older Streamlit version's docs.

!important is deliberate here: Streamlit's own stylesheet sets these
values at a specificity/load-order this session can't fully control from
outside a browser, and a silently-not-applied override is worse than an
explicit one -- flagged honestly rather than assumed to work, since this
hasn't been screenshot-verified (no browser tool in this session).
"""
import streamlit as st

CUSTOM_CSS = """
<style>
html, body, [data-testid="stAppViewContainer"], [data-testid="stMarkdownContainer"] {
    font-size: 12pt !important;
}
[data-testid="stCaptionContainer"] {
    font-size: 12pt !important;
}
[data-testid="stMarkdownContainer"] h1 {
    font-size: 20pt !important;
}
[data-testid="stMarkdownContainer"] h2 {
    font-size: 16pt !important;
}
[data-testid="stMarkdownContainer"] h3 {
    font-size: 14pt !important;
}
</style>
"""


def apply_custom_css() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
