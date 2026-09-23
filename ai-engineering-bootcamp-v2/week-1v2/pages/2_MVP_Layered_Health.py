"""MVP Layered Health — a dedicated page over GET /health. p3m3 permanent
item #22, follow-on to #21's rename (which fixed the entry script's own
sidebar label). Streamlit's native multi-page convention, same as
pages/1_Observability_Dashboard.py: filename MVP_Layered_Health.py ->
sidebar label "MVP Layered Health" (leading digit + underscore stripped,
remaining underscores -> spaces — see that file's own docstring for the
source_util.py mechanism this relies on).

Reuses api_client.call_json rather than a new HTTP helper — same
component-reuse discipline #17 established after an earlier version of
this app duplicated that logic instead of sharing it.
"""
import streamlit as st

from api_client import call_json
from ui_theme import apply_custom_css
from ui_widgets import base_url_sidebar_widget

st.set_page_config(page_title="MVP Layered Health", layout="wide")
apply_custom_css()
st.title("MVP Layered Health")
st.caption("A quick, no-cost check of GET /health — status and which Claude Code Skills this project's own development used (see p3m3/skills-and-mcp-inventory.md).")

base_url = base_url_sidebar_widget()

status, data = call_json("GET", f"{base_url.rstrip('/')}/health")

if status != 200 or not isinstance(data, dict):
    st.error("Request failed" if status == 0 else f"HTTP {status}")
    st.json(data)
    st.stop()

st.success(f"API status: {data.get('status', '(missing)')}")
skills_used = data.get("skills_used", [])
if skills_used:
    st.metric("Skills used building this project", len(skills_used))
    for skill in skills_used:
        st.caption(f"• {skill}")
    st.caption(
        "Disambiguation: this is which Claude Code Skills the *developers* used while "
        "**building** this app's code — not something the deployed app invokes to handle "
        "any given question. It never runs inside a Claude Code session itself (it just "
        "calls OpenAI/Groq/etc. directly), so this list is identical on every call, not "
        "computed per-request."
    )
else:
    st.caption("No skills_used field in the response — check the API base URL, or this may be an older deployed build.")

with st.expander("Raw response"):
    st.json(data)
