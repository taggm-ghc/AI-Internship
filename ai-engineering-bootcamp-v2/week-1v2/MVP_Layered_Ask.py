"""Minimal Streamlit UI for the Week 1 v2 `/ask` demo.

Run:
  streamlit run MVP_Layered_Ask.py
"""

import pandas as pd
import altair as alt
import streamlit as st

from api_client import (
    build_payload,
    build_stream_payload,
    call_json,
    call_stream,
    render_curl,
)
from ui_theme import apply_custom_css
from ui_widgets import base_url_sidebar_widget, references_widget
from pricing_config import load_model_selection

# Read from config/model-selection.json (the same file main.py reads)
# instead of a separately hardcoded list, so this dropdown can't drift out
# of sync with what the API actually accepts — added 2026-09-13 alongside
# the same fix in main.py (see ModelSelection.supported_models). These are
# the explicit-override choices only; "Auto" (below, always first) is a
# separate, UI-only option that sends no override at all.
_model_selection = load_model_selection()
MODELS = _model_selection.supported_models if _model_selection else ["gpt-4o-mini"]

# The HTTP-client layer (default_api_base_url, build_payload/
# build_stream_payload, render_curl, call_json/call_stream, and the
# cold-start-retry helpers underneath them) moved to api_client.py on
# 2026-09-22 — see that module's docstring — so pages/1_Observability_
# Dashboard.py and this file share one implementation instead of two
# drifting copies. This file now only defines Streamlit UI rendering.


def render_attempts(data: dict | str) -> None:
    if not isinstance(data, dict):
        return

    attempts = data.get("attempts", [])
    if not attempts:
        return

    st.markdown("### Attempts")
    for attempt in attempts:
        status = "passed" if attempt.get("ok") else "failed"
        title = f"Attempt {attempt.get('attempt')}: {attempt.get('step')} ({status})"
        with st.expander(title, expanded=True):
            st.write(attempt.get("message"))
            if attempt.get("raw_output"):
                st.markdown("**Raw model output**")
                st.code(attempt["raw_output"], language="json")
            if attempt.get("validation_error"):
                st.markdown("**Validation error**")
                st.code(attempt["validation_error"], language="text")


def format_cost(value: float | None) -> str:
    """Fixed-point, never scientific notation — added 2026-09-14 after a
    real response showed "$1.7e-05" in the UI. Interpolating a raw Python
    float into an f-string uses str()'s default formatting, which switches
    to scientific notation below 1e-4 — a real per-token cost is routinely
    smaller than that (this project's own cost_usd values are rounded to 6
    decimal places server-side, so 6 here matches, not an arbitrary
    choice). Also handles the value being genuinely absent (no pricing
    record — see main.py's compute_cost_breakdown) as "-", not "$None"."""
    if value is None:
        return "-"
    return f"{value:.6f}"


def render_response_summary(data: dict | str) -> None:
    if not isinstance(data, dict) or "error" in data:
        return

    answer = data.get("answer")
    if isinstance(answer, dict):
        st.markdown("### Answer")
        st.write(answer.get("answer", ""))
        st.caption(
            f"confidence: {answer.get('confidence')} | "
            f"sources_needed: {answer.get('sources_needed')}"
        )

    # Two rows: overview, then the prompt/completion + input/output split
    # (added 2026-09-14 — AskResponse now returns these separately, not
    # just the pre-combined tokens_used/cost_usd).
    row1 = st.columns(3)
    row1[0].metric("Model", str(data.get("model", "-")))
    row1[1].metric("Latency", f"{data.get('latency_ms', '-')} ms")
    row1[2].metric("Total cost", f"${format_cost(data.get('cost_usd'))}")

    row2 = st.columns(4)
    row2[0].metric("Prompt tokens", str(data.get("prompt_tokens", "-")))
    row2[1].metric("Completion tokens", str(data.get("completion_tokens", "-")))
    row2[2].metric("Input cost", f"${format_cost(data.get('input_cost_usd'))}")
    row2[3].metric("Output cost", f"${format_cost(data.get('output_cost_usd'))}")

    # None unless the serving provider has a free-tier allowance (see
    # main.py's free_tier_note()) — added 2026-09-14 after a real Groq
    # free-tier call showed a nonzero cost with nothing signaling it
    # likely wasn't actually billed money.
    if data.get("free_tier_note"):
        st.caption(f"ℹ️ {data['free_tier_note']}")

    # RAG fields, added Week 2 (2026-09-17) — always present on every /ask
    # response now (see main.py's ask(), 2.21's always-on-retrieval
    # design), not just when a grounded answer actually resulted.
    rag_status = data.get("status")
    if rag_status == "supported":
        st.success(f"Grounded in retrieved context — {len(data.get('citations', []))} citation(s)")
        st.caption("Citations (chunk IDs): " + ", ".join(data.get("citations", [])))
        references_widget(data.get("references", []))
    elif rag_status == "insufficient":
        st.warning("Question was topically relevant, but the retrieved context didn't cover it — refused rather than guessed.")
    elif rag_status == "not_applicable":
        st.caption("ℹ️ status: not_applicable — retrieval judged this question unrelated to the ingested corpus; answered directly.")
    st.caption(f"embedding_cost_usd: ${format_cost(data.get('embedding_cost_usd'))}")

    # Static project metadata, added 2026-09-22 — same value on every
    # response (see main.py's SKILLS_USED), not something this specific
    # request caused; surfaced here so it's visible in the main Response
    # view, not just the Raw JSON panel.
    skills_used = data.get("skills_used")
    if skills_used:
        st.caption(f"Skills used (this project's own development): {', '.join(skills_used)}")


# LOCAL_INFERENCE_PROVIDER_NAME's value, duplicated here rather than
# importing providers.py — this UI module only ever talks to the API over
# HTTP (see call_json/call_stream), never imports server-side modules
# directly, so it can be pointed at a remote deployment too.
_LOCAL_PROVIDER_PREFIX = "local-inference@"


def record_roundtrip(
    served_by: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: float | None,
    free_tier_note: str | None = None,
    latency_ms: float | None = None,
) -> None:
    """Accumulates running totals keyed by the exact "provider:model" (or
    LAN-local "provider@host:port:model") string a response was served by
    — added 2026-09-14 per request, tracking per provider/model *and* a
    grand total, since which provider/model actually serves the no-override
    default path can change call to call. Session-only (st.session_state):
    resets on a page reload, same lifetime as the rest of this demo's state.

    prompt_tokens/completion_tokens/cost_usd are all None for /ask/stream
    (that endpoint returns no usage data at all — see call_stream) except
    LAN-local's cost, which is always exactly $0.0, known, not missing. A
    bucket whose cost_usd ever went in as None (no pricing record, or an
    un-priced stream call) is flagged cost_known=False, so the sidebar can
    show its total as a lower bound instead of silently understating it as
    complete.

    free_tier_note (added same day as the per-roundtrip one on
    render_response_summary): once a bucket has ever received one, it's
    kept — a provider's free-entitlement status doesn't change response to
    response, so the first non-None value seen is as good as any later
    one. Surfaced in render_running_costs() so the *aggregate* total
    carries the same "not necessarily money actually billed" caveat the
    per-roundtrip display already has, not just the individual call.

    latency_ms (added 2026-09-15 for the sidebar's latency boxplot): kept
    as a raw per-call list, not just a running sum, since a distribution
    needs every sample — None for /ask/stream (see call_stream, same as
    prompt/completion tokens above)."""
    costs = st.session_state.setdefault("running_costs", {})
    bucket = costs.setdefault(
        served_by,
        {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_usd": 0.0, "cost_known": True, "free_tier_note": None,
            "latencies_ms": [],
        },
    )
    bucket["calls"] += 1
    if prompt_tokens is not None:
        bucket["prompt_tokens"] += prompt_tokens
    if completion_tokens is not None:
        bucket["completion_tokens"] += completion_tokens
    if cost_usd is not None:
        bucket["cost_usd"] += cost_usd
    else:
        bucket["cost_known"] = False
    if free_tier_note and not bucket["free_tier_note"]:
        bucket["free_tier_note"] = free_tier_note
    if latency_ms is not None:
        bucket["latencies_ms"].append(latency_ms)


def render_running_costs() -> None:
    costs = st.session_state.get("running_costs", {})
    if not costs:
        st.sidebar.caption("No calls recorded yet this session.")
        return

    grand_calls = sum(b["calls"] for b in costs.values())
    grand_tokens = sum(b["prompt_tokens"] + b["completion_tokens"] for b in costs.values())
    grand_cost = sum(b["cost_usd"] for b in costs.values())
    any_unknown = any(not b["cost_known"] for b in costs.values())
    free_tier_providers = [served_by for served_by, b in costs.items() if b["free_tier_note"]]

    st.sidebar.metric(
        "Grand total cost" + (" (partial)" if any_unknown else ""),
        f"${grand_cost:.6f}",
    )
    st.sidebar.caption(
        f"{grand_calls} call(s), {grand_tokens} token(s) across {len(costs)} provider/model pair(s)"
    )
    if free_tier_providers:
        st.sidebar.caption(
            f"ℹ️ Includes free-tier usage ({', '.join(free_tier_providers)}) — "
            "this total is real per-token value, not necessarily money "
            "actually billed. See each entry below for details."
        )
    for served_by, bucket in sorted(costs.items()):
        note = "" if bucket["cost_known"] else " (partial — some calls had no pricing data)"
        st.sidebar.caption(
            f"**{served_by}**\n\n"
            f"{bucket['calls']} call(s) · {bucket['prompt_tokens']}+{bucket['completion_tokens']} tok "
            f"· ${bucket['cost_usd']:.6f}{note}"
        )


def render_latency_boxplot() -> None:
    """One box per provider/model, showing this session's /ask latency
    spread — /ask/stream contributes no samples (see record_roundtrip),
    so a session with only stream calls shows the empty-state caption
    below instead of a chart. altair/pandas are already transitive
    Streamlit dependencies (st.*_chart uses altair internally), pinned
    explicitly in requirements.txt once this became a direct import."""
    costs = st.session_state.get("running_costs", {})
    rows = [
        {"served_by": served_by, "latency_ms": value}
        for served_by, bucket in costs.items()
        for value in bucket["latencies_ms"]
    ]
    if not rows:
        st.sidebar.caption("No /ask latency samples yet this session.")
        return

    chart = (
        alt.Chart(pd.DataFrame(rows))
        .mark_boxplot(extent="min-max")
        .encode(
            x=alt.X("served_by:N", title=None, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("latency_ms:Q", title="latency (ms)"),
            color=alt.Color("served_by:N", legend=None),
        )
        .properties(height=220)
    )
    st.sidebar.altair_chart(chart, width="stretch")


st.set_page_config(page_title="Agentic AI Engineering Bootcamp: Layered MVP", layout="wide")
apply_custom_css()
st.title("Agentic AI Engineering Bootcamp: Layered MVP")
st.caption(
    "One final demo endpoint. The separate `stages/` files show how this grows step by step."
)

base_url = base_url_sidebar_widget()

# p3m3 permanent item #20 — corpus hint, directly motivated by #19: a
# question the corpus was never going to answer (e.g. "apple pie") can get
# a confidently fabricated response through rag_mode "auto" rather than a
# visible refusal (see week2-priority-checklist.md's D-N+1 section). This
# doesn't fix that path; it helps a user avoid triggering it by showing
# roughly what's actually in there before they ask. Cached in session
# state so it's fetched once per session, not on every rerun (this
# doesn't change while the app is running, unlike base_url/rag_mode).
if "corpus_summary" not in st.session_state:
    _cs_status, _cs_data = call_json("GET", f"{base_url.rstrip('/')}/debug/corpus-summary")
    st.session_state["corpus_summary"] = _cs_data if _cs_status == 200 else None
_corpus_summary = st.session_state["corpus_summary"]
with st.sidebar.expander("📚 What's in the corpus?", expanded=False):
    if isinstance(_corpus_summary, dict) and "document_count" in _corpus_summary:
        st.caption(f"{_corpus_summary['document_count']} documents. A random sample of titles:")
        for _title in _corpus_summary.get("sample_titles", []):
            st.caption(f"• {_title}")
    else:
        st.caption("Couldn't load a corpus summary — check the API base URL above.")

# p3m3 permanent item #25 — explicit st.sidebar.page_link() calls to the
# other pages used to live here (added in #17/#22), but Streamlit's own
# multi-page nav (auto-generated from pages/, confirmed working in #17's
# D-N section) already lists every page at the top of every sidebar —
# these were pure duplication, plus a visible bug (icon="..." doubled the
# emoji already in the label text: "📊📊 Observability Dashboard"). Removed
# rather than fixed in place, per user feedback ("jumbled, repetitive").
# The "Start the API"/"Start this page" setup instructions that used to
# follow also moved out — see pages/3_Setup.py — so this sidebar only
# shows what's relevant to actually using the app, not one-time setup.

# Cached in session_state (not re-fetched every rerun) so the provider
# selector below can use it immediately on first page load, not just after
# a manual "Refresh" click — added 2026-09-14 alongside that selector.
# Local entries include a live discovery probe, so this can take a couple
# seconds the first time; an unreachable API leaves it as [], which both
# this panel and the selector already handle (selector falls back to
# "Auto"/"openai" only).
if "provider_status" not in st.session_state:
    _, _initial_status = call_json("GET", f"{base_url.rstrip('/')}/providers/status")
    st.session_state["provider_status"] = _initial_status if isinstance(_initial_status, list) else []

# Surfaces which provider(s) the no-override default path would actually
# hit right now, including LAN-local's host:port identity — added
# 2026-09-14 after a real bug (LOCAL_INFERENCE_HOST/PORT frozen at
# providers.py's import time, before main.py's load_dotenv() ran) made
# LAN-local silently never activate even when correctly configured in
# .env. GET /providers/status costs nothing and never returns key values.
st.sidebar.markdown("### Provider status")
if st.sidebar.button("Refresh provider status"):
    status, providers_data = call_json("GET", f"{base_url.rstrip('/')}/providers/status")
    if not isinstance(providers_data, list):
        st.sidebar.error(f"HTTP {status}" if status else "Not connected")
    else:
        st.session_state["provider_status"] = providers_data
        for entry in providers_data:
            label = entry.get("provider", "?")
            state = entry.get("status", "?")
            # state can now be "configured: key_expiring_YYYY-MM-DD" or
            # "unavailable: key_expired_YYYY-MM-DD" (see
            # providers.key_expiry_status) — startswith, not ==, so an
            # expiring-soon key still reads as "working but needs
            # attention" (🟡) rather than falling into the same gray
            # bucket as a genuinely unconfigured provider.
            if state.startswith("configured") and "expiring" in state:
                icon = "🟡"
            elif state == "configured":
                icon = "🟢"
            else:
                icon = "⚪"
            # base_url/compatible_with added 2026-09-14 to GET
            # /providers/status — None here means "OpenAI SDK's own
            # default endpoint" (only the terminal openai entry), not
            # unknown.
            served_url = entry.get("base_url") or "(OpenAI SDK default)"
            compat = entry.get("compatible_with", "?")
            line = (
                f"{icon} **{label}** — {entry.get('model', '?')} ({state})\n\n"
                f"{served_url} · {compat}-compatible"
            )
            # None unless this entry has a free_entitlement_* allowance —
            # added 2026-09-14, same information as main.py's
            # free_tier_note, shown here too (not just attached to a live
            # response) since this panel is the one place meant to answer
            # "what would using this provider actually cost me."
            free_amount = entry.get("free_entitlement_amount")
            if free_amount is not None:
                line += (
                    f"\n\nℹ️ {free_amount:g} {entry.get('free_entitlement_unit')}/"
                    f"{entry.get('free_entitlement_period')} free — its cost_usd "
                    "won't necessarily reflect money actually billed."
                )
            st.sidebar.caption(line)

# Week 2: ingest a document into the same chroma_store/ collection /ask's
# retrieval step reads from. Collapsed by default — most demo visits are
# asking questions against the pre-loaded 50-doc baseline corpus (see
# rag_ingest.py), not adding new documents — but this is what proves
# POST /ingest is a real, live pipeline and not just a deploy-time
# hard-coded corpus (see p3m3/week2-priority-checklist.md's "Hard-coding
# docs at deploy is not a pipeline" guardrail note).
with st.expander("Ingest a document (POST /ingest)", expanded=False):
    with st.form("ingest_form"):
        ingest_document_id = st.text_input("document_id", "streamlit-demo-doc")
        ingest_text = st.text_area(
            "text", "Paste or type the document text to ingest here.", height=120
        )
        # p3m3 item #30/#33 -- optional, matches INGEST_API_KEY's own
        # fail-open design: blank means the same unauthenticated behavior
        # as before this field existed (new docs go live, re-ingests of an
        # existing document_id always stage). A real key here authenticates
        # the call -- auto-accept on a clean re-ingest, and the phrase-scan
        # bypass on a brand-new document. type="password" masks it the same
        # way base_url_sidebar_widget's own field is hidden, so a
        # screenshot of this form can't leak it either.
        ingest_key = st.text_input(
            "X-Ingest-Key (optional)",
            type="password",
            placeholder="(leave blank to ingest as an unauthenticated caller)",
            help="Leave blank to ingest as an unauthenticated caller (new documents still go live; "
            "re-ingesting an existing document_id always stages a version for review). A valid key "
            "authenticates the call for auto-accept and the unauthenticated phrase-scan bypass.",
        )
        ingest_submitted = st.form_submit_button("Ingest")

    ingest_payload = {"text": ingest_text, "document_id": ingest_document_id, "metadata": None}
    st.code(render_curl("/ingest", ingest_payload), language="bash")
    st.caption("$API_BASE_URL is a placeholder -- swap in your own API's URL to actually run this.")

    if ingest_submitted:
        ingest_headers = {"X-Ingest-Key": ingest_key} if ingest_key else None
        with st.spinner("Calling /ingest..."):
            ingest_status, ingest_data = call_json(
                "POST", f"{base_url.rstrip('/')}/ingest", ingest_payload, headers=ingest_headers
            )
        st.markdown(f"**HTTP {ingest_status}**" if ingest_status else "**Request failed**")
        st.json(ingest_data)

# Outside the form (not batched) so switching it reruns immediately and
# the form below can show/hide the force_bad checkbox accordingly — /ask
# supports the guardrail retry demo, /ask/stream does not (see
# build_stream_payload).
endpoint_choice = st.radio(
    "Endpoint",
    ["/ask (structured, guardrail retry)", "/ask/stream (freeform, no retry)"],
    horizontal=True,
)
is_stream = endpoint_choice.startswith("/ask/stream")
endpoint_path = "/ask/stream" if is_stream else "/ask"

# Two columns spanning everything below Endpoint — Provider, the form,
# Request, and Response all live in main_col; raw_col holds Raw JSON as a
# persistent peer of main_col's own container, not nested three levels
# deep under Response further down. Repositioned 2026-09-14: Raw JSON
# previously lived in its own st.columns() created fresh down near
# Response, which made it a peer of Response, not of Provider — a column
# can't itself collapse in Streamlit, so the expander inside raw_col is
# what actually gives "collapsible."
main_col, raw_col = st.columns([2, 1])

with main_col:
    # Also outside the form, same reason as Endpoint above — the Model
    # selectbox inside the form needs to see this value at render time to
    # show the right options, which only happens if picking a provider
    # reruns the page immediately rather than waiting for form submission.
    # Options come from the cached GET /providers/status fetch (see
    # above), not a hardcoded list, so this can never drift out of sync
    # with what's actually configured — added 2026-09-14.
    _provider_status = st.session_state.get("provider_status", [])
    # Only offer a provider that's actually usable right now — status
    # "configured" (🟢) or "configured: key_expiring_..." (🟡, still works,
    # just due for a new key soon) — not "unavailable: ..." (⚪, e.g.
    # credential_missing) or "retired: ...". Refined 2026-09-14: the first
    # cut of this listed every provider_chain entry regardless of whether
    # it could actually serve a request, which meant picking one like
    # gemini or openrouter (no key set) would only ever fail.
    _provider_names = sorted({
        e["provider"] for e in _provider_status
        if e.get("provider") != "openai" and str(e.get("status", "")).startswith("configured")
    })
    provider_choice = st.selectbox(
        "Provider",
        ["Auto (free-tier-first)", "openai"] + _provider_names,
        help=(
            "Forces one specific provider on the no-override chain instead of "
            "letting the frontier ranking pick. 'openai' is handled via the "
            "existing explicit Model override below, same as always."
        ),
    )
    _is_openai_selected = provider_choice == "openai"
    _is_specific_provider = provider_choice not in ("Auto (free-tier-first)", "openai")
    if _is_specific_provider and not is_stream:
        _entry = next((e for e in _provider_status if e["provider"] == provider_choice), None)
        if _entry and _entry.get("structured_output") != "strict":
            st.caption(
                f"⚠️ {provider_choice} isn't confirmed for structured output — "
                "/ask will reject this with a 400. Switch to /ask/stream, or "
                "pick Auto/openai instead."
            )

    with st.form("ask_form"):
        question = st.text_area(
            "Question",
            "What is Retrieval-Augmented Generation in one sentence?",
            height=100,
        )
        if _is_specific_provider:
            # Every current cloud provider_chain entry has exactly one
            # configured model, so this is usually a single,
            # effectively-fixed option — LAN-local can genuinely offer
            # more than one (each discovered model becomes its own
            # GET /providers/status row under the same provider name), so
            # this stays a real selectbox rather than a static label
            # either way. Filtered by status too, not just provider name
            # (refined 2026-09-14, same reasoning as the Provider
            # dropdown's own filter above) — this only matters for
            # LAN-local today, where is_retired() is evaluated per
            # *model*, so one discovered model can be stale ("retired:
            # not seen in 180+ days") while another under the same server
            # is fine; every cloud provider still has exactly one model
            # row either way.
            _provider_models = sorted({
                e["model"] for e in _provider_status
                if e["provider"] == provider_choice and str(e.get("status", "")).startswith("configured")
            })
            model_choice = st.selectbox("Model", _provider_models or ["(no available model)"])
            model = None  # non-OpenAI models can't go through AskRequest.model — see AskRequest.provider's comment
        elif _is_openai_selected:
            model_choice = st.selectbox("Model", MODELS, index=0)
            model = model_choice
        else:
            st.selectbox("Model", ["Auto (free-tier-first)"], index=0, disabled=True)
            model = None
        if model is not None:
            st.caption(
                "An explicit model always requires a valid OpenAI key, on both "
                "endpoints — it bypasses the free-tier/LAN-local fallback chain "
                "by design. Pick Auto to exercise that chain instead."
            )
        force_bad = False
        if not is_stream:
            force_bad = st.checkbox(
                "Force a bad first response to demo validation + retry",
                value=False,
            )
        # Both endpoints retrieve now (added to /ask/stream 2026-09-22, see
        # main.py's ask_stream docstring). "Auto" is the existing
        # RAG_RELEVANCE_THRESHOLD gate; "Force RAG" bypasses it (grounds on
        # whatever was retrieved regardless of distance — useful to demo
        # retrieval on a question the gate would otherwise call
        # off-topic, at the risk of grounding on irrelevant chunks); "No
        # RAG" skips retrieval entirely for a direct, ungrounded answer.
        rag_mode_choice = st.radio(
            "RAG mode",
            ["Auto", "Force RAG", "No RAG"],
            horizontal=True,
            help=(
                "Auto: the relevance gate decides whether to ground. "
                "Force RAG: always ground on the top retrieved chunks, "
                "even if the gate would normally judge them too far "
                "off-topic. No RAG: skip retrieval, answer directly."
            ),
        )
        rag_mode = {"Auto": "auto", "Force RAG": "force_rag", "No RAG": "no_rag"}[rag_mode_choice]
        submitted = st.form_submit_button("Ask", type="primary")

    forced_provider = provider_choice if _is_specific_provider else None
    payload = (
        build_stream_payload(question, model, forced_provider, rag_mode)
        if is_stream
        else build_payload(question, model, force_bad, forced_provider, rag_mode)
    )

    st.markdown("### Request")
    st.code(render_curl(endpoint_path, payload), language="bash")
    st.caption("$API_BASE_URL is a placeholder -- swap in your own API's URL to actually run this.")

    health_col, _ = st.columns(2)
    with health_col:
        if st.button("Check API health"):
            status, health_data = call_json("GET", f"{base_url.rstrip('/')}/health")
            st.markdown(f"**HTTP {status}**" if status else "**Not connected**")
            st.json(health_data)

    if submitted:
        if is_stream:
            with st.spinner("Calling /ask/stream..."):
                status, text, served_by, grounded, citations, skills_used = call_stream(
                    f"{base_url.rstrip('/')}{endpoint_path}", payload
                )
            st.markdown("### Response")
            st.markdown(f"**HTTP {status}**" if status else "**Request failed**")
            if status == 200 and grounded is not None:
                st.caption(
                    f"RAG grounded: {grounded}"
                    + (f" — citations: {', '.join(citations)}" if citations else "")
                )
            if skills_used:
                st.caption(f"Skills used (this project's own development): {', '.join(skills_used)}")
            if served_by:
                st.caption(f"Served by: {served_by}")
                if status == 200:
                    # /ask/stream returns no usage data at all (see
                    # call_stream's docstring) — LAN-local's cost is
                    # nonetheless exactly known ($0.0, not missing),
                    # everything else is genuinely unknown.
                    is_local = served_by.startswith(_LOCAL_PROVIDER_PREFIX)
                    record_roundtrip(served_by, None, None, 0.0 if is_local else None)
            st.markdown("### Answer")
            st.write(text)
        else:
            with st.spinner("Calling /ask..."):
                status, data = call_json("POST", f"{base_url.rstrip('/')}{endpoint_path}", payload)
            if status == 200 and isinstance(data, dict) and "error" not in data:
                record_roundtrip(
                    str(data.get("model", "unknown")),
                    data.get("prompt_tokens"),
                    data.get("completion_tokens"),
                    data.get("cost_usd"),
                    data.get("free_tier_note"),
                    data.get("latency_ms"),
                )
            st.markdown("### Response")
            st.markdown(f"**HTTP {status}**" if status else "**Request failed**")
            render_response_summary(data)
            render_attempts(data)

# Raw JSON: a peer of main_col (and so of Provider's own container), not
# nested under Response — see the comment above main_col/raw_col.
if submitted and not is_stream:
    with raw_col:
        with st.expander("Raw JSON", expanded=False):
            # st.json expects an actual JSON-able object — handing it a
            # plain string (call_json's fallback for a non-JSON body, e.g.
            # Render's bare-text error responses) makes it try to
            # client-side JSON.parse() that string and surface a raw
            # "Json Parse Error" instead of just showing the text. Found
            # live 2026-09-15 against a Render edge bounce during cold
            # start.
            if isinstance(data, (dict, list)):
                st.json(data)
            else:
                st.code(str(data), language="text")

# Running (session) costs — left pane: shows what a call *would* cost
# cumulatively as you keep testing, broken out per provider/model since
# that can change call to call (see record_roundtrip). Deliberately placed
# here, AFTER the submission-handling block above rather than near the
# other sidebar setup near the top of this file — Streamlit reruns the
# whole script top-to-bottom on every interaction, and st.sidebar calls
# render into the sidebar regardless of where in the script they're made
# (sidebar layout order follows st.sidebar call order, not file position
# relative to main-body code). Rendering this before record_roundtrip()
# ran (this session's original position) meant the sidebar always showed
# the PREVIOUS submission's totals, one rerun stale — confirmed 2026-09-14
# from two real screenshots where the just-submitted call's own real
# tokens/cost never appeared in the grand total. Moving it here (same
# bottom-of-sidebar visual position, since it was already the last
# sidebar section) fixes that without changing the layout.
st.sidebar.markdown("### Running costs (this session)")
render_running_costs()

st.sidebar.markdown("### Latency (this session)")
render_latency_boxplot()

if st.sidebar.button("Reset session totals"):
    st.session_state["running_costs"] = {}
    st.rerun()
