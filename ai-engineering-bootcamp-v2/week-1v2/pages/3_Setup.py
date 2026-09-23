"""Local setup instructions, moved out of MVP_Layered_Ask.py's sidebar --
p3m3 permanent item #25. User feedback on the main page's sidebar:
"jumbled, repetitive" -- it had explicit page_link() buttons duplicating
Streamlit's own auto-generated nav (plus a doubled-icon bug), and these
one-time setup code blocks taking up space on every page load even
though they're reference material, not something needed while actually
using the app. Split: the redundant links were removed outright (the
auto-nav already covers that); this setup content got its own page
instead of being deleted, since it's still genuinely useful reference.
"""
import streamlit as st

from ui_theme import apply_custom_css

st.set_page_config(page_title="Setup", layout="wide")
apply_custom_css()
st.title("Setup")
st.caption("One-time local setup commands — not needed once the API and this UI are already running.")

WORKDIR_CMD = "ai-engineering-bootcamp-v2/week-1v2"

st.markdown("### Start the API")
st.code(
    f"cd {WORKDIR_CMD}\n"
    "source .venv/bin/activate\n"
    "uvicorn main:app --host 127.0.0.1 --port 8000 --reload",
    language="bash",
)

st.markdown("### Start this UI")
st.code(
    f"cd {WORKDIR_CMD}\nsource .venv/bin/activate\nstreamlit run MVP_Layered_Ask.py",
    language="bash",
)
