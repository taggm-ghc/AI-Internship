"""Agent — Streamlit UI for the Session 3 capstone-as-agent assignment
(p3m3 item #38, W3/S3). Streamlit's native multi-page convention, same as
pages/1-3: filename Agent.py -> sidebar label "Agent".

Reuses api_client.call_json rather than a new HTTP helper, and
ui_theme/ui_widgets, same component-reuse discipline as every other page.
Demos the real Think -> Act -> Observe loop by rendering agent_service.py's
own trace verbatim, not a manufactured summary of it.
"""
import streamlit as st

from api_client import call_json
from ui_theme import apply_custom_css
from ui_widgets import base_url_sidebar_widget, references_widget

st.set_page_config(page_title="Agent", layout="wide")
apply_custom_css()
st.title("Agent")
st.caption(
    "A LangGraph agent built on this project's existing RAG corpus (POST /agent). "
    "Unlike /ask, which always retrieves on a fixed distance threshold, this agent "
    "decides for itself whether to call the search_corpus tool at all -- the real "
    "agent-vs-workflow distinction, not a relabeled pipeline."
)

base_url = base_url_sidebar_widget()

question = st.text_input(
    "Ask the agent something",
    placeholder="e.g. What is the memory wall problem for mixture-of-experts models on SSDs?",
)
run_clicked = st.button("Run", type="primary", disabled=not question.strip())

if run_clicked:
    with st.spinner("Running the agent loop..."):
        status, data = call_json("POST", f"{base_url.rstrip('/')}/agent", {"question": question})

    if status != 200 or not isinstance(data, dict):
        st.error("Request failed" if status == 0 else f"HTTP {status}")
        st.json(data)
        st.stop()

    st.subheader("Answer")
    st.write(data.get("answer", "(no answer field)"))

    # p3m3 item #47 (OWASP ASI09): say where the answer came from, from the
    # trace itself, so a confident answer isn't mistaken for a grounded one.
    grounding = data.get("grounding")
    sources = data.get("sources") or []
    if grounding == "tool_sources":
        st.caption(
            "Grounding: the corpus was searched and returned passages from "
            + ", ".join(f"`{s}`" for s in sources)
            + ". Whether the answer actually relies on them isn't verified; check the trace."
        )
    elif grounding == "tool_found_nothing":
        st.warning("Grounding: the corpus search found nothing relevant, so this answer is NOT from the corpus.")
    elif grounding == "no_tool_call":
        st.info("Grounding: answered from the model's general knowledge. The corpus was not searched.")
    references_widget(data.get("references", []))

    trace = data.get("trace", [])
    tool_calls_made = sum(1 for step in trace if step.get("role") == "assistant_tool_call")
    st.caption(
        f"Agent-vs-workflow: this run made {tool_calls_made} tool call(s) — "
        + ("it chose to search the corpus." if tool_calls_made else "it answered directly, without searching the corpus.")
    )

    with st.expander("Think -> Act -> Observe trace", expanded=True):
        for step in trace:
            role = step.get("role")
            if role == "user":
                st.markdown(f"**User:** {step.get('content')}")
            elif role == "assistant_tool_call":
                for call in step.get("tool_calls") or []:
                    st.markdown(f"**Think -> Act:** calls `{call['name']}({call['args']})`")
            elif role == "tool_result":
                st.markdown("**Observe:**")
                st.text((step.get("content") or "")[:2000])
            elif role == "assistant":
                st.markdown(f"**Decide again (final answer):** {step.get('content')}")

    with st.expander("Raw response"):
        st.json(data)
