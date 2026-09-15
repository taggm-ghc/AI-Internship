"""Minimal Streamlit UI for the Week 1 v2 `/ask` demo.

Run:
  streamlit run demo_page.py
"""

import json
import os
from pathlib import Path

import httpx
import streamlit as st

from pricing_config import load_model_selection

WORKDIR_CMD = "ai-engineering-bootcamp-v2/week-1v2"

# Read from config/model-selection.json (the same file main.py reads)
# instead of a separately hardcoded list, so this dropdown can't drift out
# of sync with what the API actually accepts — added 2026-09-13 alongside
# the same fix in main.py (see ModelSelection.supported_models). These are
# the explicit-override choices only; "Auto" (below, always first) is a
# separate, UI-only option that sends no override at all.
_model_selection = load_model_selection()
MODELS = _model_selection.supported_models if _model_selection else ["gpt-4o-mini"]

# run.sh records its actual host:port here on every start, so the default
# below tracks whichever port is currently active instead of a hardcoded one
# that drifts whenever the API is started on a non-default port. Ported
# 2026-09-13 from ../../ai-eng-bootcamp.vera/streamlit_app.py's equivalent.
_LOCAL_URL_FILE = Path(__file__).resolve().parent / ".faststream-local-url"


def _default_api_base_url() -> str:
    # Set as a Render dashboard env var on the deployed Streamlit service
    # (never commit the real value) so the sidebar auto-fills instead of
    # requiring a manual paste on every visit.
    env_url = os.getenv("API_BASE_URL", "").strip()
    if env_url:
        return env_url
    try:
        return _LOCAL_URL_FILE.read_text().strip() or "http://127.0.0.1:8000"
    except FileNotFoundError:
        return "http://127.0.0.1:8000"


def build_payload(question: str, model: str | None, force_bad: bool, provider: str | None = None) -> dict:
    return {
        "question": question,
        "model": model,
        "force_bad": force_bad,
        "provider": provider,
    }


def build_stream_payload(question: str, model: str | None, provider: str | None = None) -> dict:
    # AskStreamRequest has no force_bad field — the guardrail retry demo
    # only applies to /ask, since a streaming response can't un-send bytes
    # already delivered to the caller (see ask_service.stream_answer).
    return {"question": question, "model": model, "provider": provider}


def render_curl(base_url: str, path: str, payload: dict) -> str:
    body = json.dumps(payload)
    return (
        f'curl -s -X POST {base_url.rstrip("/")}{path} '
        f'-H "Content-Type: application/json" '
        f"-d '{body}'"
    )


def _unreachable_message(url: str) -> str:
    """ConnectError means nothing answered at all — the fix differs by
    whether base_url is local (nobody started uvicorn) or a deployed
    instance (wrong URL, or the service is actually down), so the old
    one-size-fits-all "Start the API server first." was actively wrong
    advice for a visitor to a deployed Streamlit page — they can't start
    someone else's Render service. Bug found 2026-09-15 against a real
    deployed instance."""
    host = httpx.URL(url).host
    if host in ("127.0.0.1", "localhost"):
        return f"Cannot reach {url}. Start the API server first."
    return (
        f"Cannot reach {url} — nothing answered at all. Double-check the "
        "URL, and confirm the service is actually deployed and running in "
        "the Render dashboard (a slow *response*, as opposed to no "
        "response, usually means a free-tier cold start instead — see the "
        "timeout message)."
    )


def _timeout_message(url: str) -> str:
    return (
        f"Timed out waiting for {url}. If this is a Render free-tier "
        "deployment, it may be waking from a cold start after a period of "
        "inactivity — that can take up to a minute (see the README's "
        "Test With Curl section) — try again in a moment."
    )


def call_stream(url: str, payload: dict) -> tuple[int, str, str | None]:
    """Like call_json, but for /ask/stream: the body is plain text, not
    JSON, and the only structured metadata is the X-Served-By header (see
    main.py's ask_stream docstring for why a streamed response has nowhere
    else to report which provider/model actually answered)."""
    try:
        response = httpx.post(url, json=payload, timeout=120.0)
        served_by = response.headers.get("X-Served-By")
        if response.status_code >= 400:
            try:
                return response.status_code, json.dumps(response.json(), indent=2), served_by
            except json.JSONDecodeError:
                return response.status_code, response.text, served_by
        return response.status_code, response.text, served_by
    except httpx.ConnectError:
        return 0, _unreachable_message(url), None
    except httpx.TimeoutException:
        return 0, _timeout_message(url), None
    except httpx.HTTPError as exc:
        return 0, str(exc), None


def call_json(method: str, url: str, payload: dict | None = None) -> tuple[int, dict | str]:
    try:
        if method == "POST":
            response = httpx.post(url, json=payload, timeout=120.0)
        else:
            # 65s, not a snappy few seconds: this path also serves /health
            # and /providers/status, and Render's free tier can take up to
            # a minute to wake a cold-started deployment — a short timeout
            # here misreported that wakeup delay as a generic HTTPError
            # instead of ever reaching the clearer cold-start message below.
            response = httpx.get(url, timeout=65.0)

        try:
            return response.status_code, response.json()
        except json.JSONDecodeError:
            return response.status_code, response.text
    except httpx.ConnectError:
        return 0, {"error": _unreachable_message(url)}
    except httpx.TimeoutException:
        return 0, {"error": _timeout_message(url)}
    except httpx.HTTPError as exc:
        return 0, {"error": str(exc)}


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
    per-roundtrip display already has, not just the individual call."""
    costs = st.session_state.setdefault("running_costs", {})
    bucket = costs.setdefault(
        served_by,
        {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_usd": 0.0, "cost_known": True, "free_tier_note": None,
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


st.set_page_config(page_title="Week 1 v2 /ask Demo", layout="wide")
st.title("Week 1 v2: Minimal `/ask` Demo")
st.caption(
    "One final demo endpoint. The separate `stages/` files show how this grows step by step."
)

base_url = st.sidebar.text_input("API base URL", _default_api_base_url())
st.sidebar.markdown("### Start the API")
st.sidebar.code(
    f"cd {WORKDIR_CMD}\n"
    "source .venv/bin/activate\n"
    "uvicorn main:app --host 127.0.0.1 --port 8000 --reload",
    language="bash",
)
st.sidebar.markdown("### Start this page")
st.sidebar.code(
    f"cd {WORKDIR_CMD}\nsource .venv/bin/activate\nstreamlit run demo_page.py",
    language="bash",
)

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
        submitted = st.form_submit_button("Ask", type="primary")

    forced_provider = provider_choice if _is_specific_provider else None
    payload = (
        build_stream_payload(question, model, forced_provider)
        if is_stream
        else build_payload(question, model, force_bad, forced_provider)
    )

    st.markdown("### Request")
    st.code(render_curl(base_url, endpoint_path, payload), language="bash")

    health_col, _ = st.columns(2)
    with health_col:
        if st.button("Check API health"):
            status, health_data = call_json("GET", f"{base_url.rstrip('/')}/health")
            st.markdown(f"**HTTP {status}**" if status else "**Not connected**")
            st.json(health_data)

    if submitted:
        if is_stream:
            with st.spinner("Calling /ask/stream..."):
                status, text, served_by = call_stream(f"{base_url.rstrip('/')}{endpoint_path}", payload)
            st.markdown("### Response")
            st.markdown(f"**HTTP {status}**" if status else "**Request failed**")
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
            st.json(data)

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
if st.sidebar.button("Reset session totals"):
    st.session_state["running_costs"] = {}
    st.rerun()
