"""Live observability dashboard over internship.events — p3m3 permanent
item #17 (see p3m3/todo-digest.md and week2-priority-checklist.md's
"D-N — observability dashboard" section for the full design rationale,
including the three research sources this page's metric choices are
built against: percentile latency not average, categorized errors not one
rate, a retrieval-quality panel reusing this app's own already-computed
status field).

A standalone Streamlit multi-page file (Streamlit's own convention: a
pages/ directory next to the entry script, MVP_Layered_Ask.py, is auto-
discovered by `streamlit run MVP_Layered_Ask.py`). Does NOT import MVP_Layered_Ask.py
itself as a module — each page in pages/ runs as its own script, and
importing a sibling *page* would re-execute its top-level UI code — but
DOES import the shared, non-page api_client.py module (pure Python, no
Streamlit calls, safe to import from anywhere) for the HTTP-client layer,
so this page and MVP_Layered_Ask.py share one cold-start-retry implementation
instead of two drifting copies (component reuse corrected 2026-09-22,
after an initial version of this file duplicated a minimal copy instead).

This page reads server-side, durable data spanning ALL sessions/users —
distinct from MVP_Layered_Ask.py's session-only sidebar cost/latency metrics.
"""
import altair as alt
import pandas as pd
import streamlit as st

from api_client import call_json
from ui_theme import apply_custom_css
from ui_widgets import base_url_sidebar_widget

# Fixed status-palette hex values (dataviz skill's reference palette,
# `references/palette.md`) — never themed/reused for categorical series,
# so a status color never impersonates a data series.
_STATUS_GOOD = "#0ca30c"
_STATUS_CRITICAL = "#d03b3b"
_STATUS_WARNING = "#fab219"
# Hoisted from a local var in Panel 4 to module level 2026-09-24 (p3m3
# permanent item #27) so Panel 4b can reuse the exact same status->color
# mapping instead of redefining it — same reuse discipline api_client.py/
# ui_theme.py/ui_widgets.py already established at the module level.
_STATUS_COLOR_BY_OUTCOME = {"supported": _STATUS_GOOD, "insufficient": _STATUS_WARNING, "not_applicable": "#8a8a86"}

RAG_RELEVANCE_THRESHOLD = 1.2  # mirrors rag_service.py's own constant — reference line only, not re-imported (that module is server-side, not importable from a Streamlit page)


st.set_page_config(page_title="Observability Dashboard", layout="wide")
apply_custom_css()
st.title("Observability Dashboard")
st.caption(
    "Server-side durable log across ALL sessions/users — internship.events via GET /debug/events. "
    "Distinct from the Ask demo's session-only sidebar cost/latency metrics."
)

base_url = base_url_sidebar_widget()
kind_filter = st.sidebar.selectbox(
    "Event kind", ["(all)", "http_completed", "retrieval", "retrieval_error", "ingest", "http_started", "golden_eval"]
)
limit = st.sidebar.slider("Events to fetch", 50, 500, 200)
# No explicit "back" link: Streamlit's classic pages/-directory mode already
# auto-generates sidebar navigation back to the entrypoint script, and an
# explicit st.page_link("MVP_Layered_Ask.py", ...) here raised
# StreamlitPageNotFoundError — the entrypoint isn't addressable that way
# from inside pages/ in this Streamlit version (1.63.0), only confirmed by
# actually running this page with streamlit.testing.v1.AppTest, not by
# reading the code. Caught and removed 2026-09-22.

url = f"{base_url.rstrip('/')}/debug/events?limit={limit}"
if kind_filter != "(all)":
    url += f"&kind={kind_filter}"
status, events = call_json("GET", url)

if status != 200 or not isinstance(events, list):
    st.error("Request failed" if status == 0 else f"HTTP {status}")
    st.json(events)
    st.stop()

if not events:
    st.info("No events recorded yet — make some /ask, /ingest, or /debug/retrieve calls first.")
    st.stop()

df = pd.DataFrame(events)
df["created_at"] = pd.to_datetime(df["created_at"])
completed = df[df["kind"] == "http_completed"].copy()
retrievals = df[df["kind"] == "retrieval"].copy()

st.markdown("## Panel 1 — request volume over time")
if completed.empty:
    st.caption("No http_completed events in this window.")
else:
    # Project only the column the chart needs — passing the full `completed`
    # frame (its `payload` column holds dicts) into alt.Chart made Streamlit's
    # Arrow serialization fail and silently fall back (caught by actually
    # running this page with AppTest, not visible from the code alone).
    volume_chart = (
        alt.Chart(completed[["created_at"]])
        .mark_bar(color="#2a78d6")  # categorical slot 1 — single series, sequential-style single hue
        .encode(
            x=alt.X("created_at:T", title="time"),
            y=alt.Y("count():Q", title="requests"),
            tooltip=[alt.Tooltip("created_at:T", title="time"), alt.Tooltip("count():Q", title="requests")],
        )
        .properties(height=200)
    )
    st.altair_chart(volume_chart, width="stretch")

st.markdown("## Panel 2 — latency (p50 / p90 / p99, not average)")
st.caption(
    "Average latency hides the long tail — p50 shows what a typical user sees, p99 shows the worst case. "
    "(2026 LLM-observability research finding; see week2-priority-checklist.md's D-N section.)"
)
if completed.empty:
    st.caption("No latency samples yet.")
else:
    latencies = completed["payload"].apply(lambda p: p.get("latency_ms")).dropna()
    if latencies.empty:
        st.caption("No latency samples yet.")
    else:
        p50, p90, p99 = latencies.quantile([0.5, 0.9, 0.99])
        cols = st.columns(3)
        cols[0].metric("p50", f"{p50:.0f} ms")
        cols[1].metric("p90", f"{p90:.0f} ms")
        cols[2].metric("p99", f"{p99:.0f} ms")

        by_path = pd.DataFrame(
            {"path": completed["payload"].apply(lambda p: p.get("path")), "latency_ms": completed["payload"].apply(lambda p: p.get("latency_ms"))}
        ).dropna()
        if not by_path.empty:
            box = (
                alt.Chart(by_path)
                .mark_boxplot(extent="min-max")
                .encode(
                    x=alt.X("path:N", title=None, axis=alt.Axis(labelAngle=-30)),
                    y=alt.Y("latency_ms:Q", title="latency (ms)"),
                    color=alt.Color("path:N", legend=alt.Legend(title="endpoint")),
                )
                .properties(height=220)
            )
            st.altair_chart(box, width="stretch")

st.markdown("## Panel 3 — errors by category")
st.caption(
    "Provider errors (429/500/529), internal errors (timeouts), and logical/client errors need different "
    "fixes and are invisible if bucketed into one rate — categorized here, not summarized as a single number."
)
if completed.empty:
    st.caption("No requests yet.")
else:
    def categorize(p: dict) -> str:
        http_status = p.get("http_status") or 200
        error = p.get("error")
        if error in {"TimeoutException", "ConnectTimeout", "ReadTimeout"}:
            return "internal (timeout)"
        if http_status in {429, 500, 502, 503, 529}:
            return "provider (5xx/429)"
        if error:
            return "internal (other)"
        if 400 <= http_status < 500:
            return "logical (4xx)"
        return "ok"

    categories = completed["payload"].apply(categorize).value_counts().reset_index()
    categories.columns = ["category", "count"]
    total = categories["count"].sum()
    ok_count = categories.loc[categories["category"] == "ok", "count"].sum()
    error_rate = 1 - (ok_count / total) if total else 0

    cols = st.columns(2)
    cols[0].metric("Total requests", int(total))
    cols[1].metric("Error rate", f"{error_rate:.1%}")

    color_for = lambda cat: _STATUS_GOOD if cat == "ok" else _STATUS_CRITICAL
    categories["color"] = categories["category"].apply(color_for)
    err_chart = (
        alt.Chart(categories)
        .mark_bar()
        .encode(
            x=alt.X("category:N", title=None, axis=alt.Axis(labelAngle=-20)),
            y=alt.Y("count:Q", title="requests"),
            color=alt.Color("color:N", scale=None, legend=None),
            tooltip=["category:N", "count:Q"],
        )
        .properties(height=200)
    )
    st.altair_chart(err_chart, width="stretch")
    st.caption("🟢 ok  ·  🔴 error categories — status color never carries meaning alone, see labels above.")

st.markdown("## Panel 4 — retrieval quality")
st.caption(
    "RAG-specific: groundedness/hit-rate is the metric that matters most for a retrieval system, per the "
    "research behind this dashboard. Uses this app's own already-computed `status` field — no new "
    "instrumentation needed."
)
ask_responses = [
    p.get("response") for p in completed["payload"] if isinstance(p.get("response"), dict) and p.get("response", {}).get("status")
] if not completed.empty else []
if not ask_responses:
    st.caption("No /ask responses with a status field in this window.")
else:
    statuses = pd.Series([r["status"] for r in ask_responses]).value_counts().reset_index()
    statuses.columns = ["status", "count"]
    hit_rate = statuses.loc[statuses["status"] == "supported", "count"].sum() / statuses["count"].sum()
    st.metric("Grounded-answer hit rate (supported / total)", f"{hit_rate:.1%}")
    statuses["color"] = statuses["status"].map(_STATUS_COLOR_BY_OUTCOME)
    status_chart = (
        alt.Chart(statuses)
        .mark_bar()
        .encode(
            x=alt.X("status:N", title=None),
            y=alt.Y("count:Q", title="requests"),
            color=alt.Color("color:N", scale=None, legend=None),
            tooltip=["status:N", "count:Q"],
        )
        .properties(height=200)
    )
    st.altair_chart(status_chart, width="stretch")

if not retrievals.empty:
    top1_distances = retrievals["payload"].apply(
        lambda p: p.get("distances", [None])[0] if p.get("distances") else None
    ).dropna()
    if not top1_distances.empty:
        dist_df = pd.DataFrame({"distance": top1_distances})
        dist_chart = (
            alt.Chart(dist_df)
            .mark_bar(color="#2a78d6")
            .encode(x=alt.X("distance:Q", bin=alt.Bin(maxbins=20), title="top-1 retrieval distance (squared L2)"), y=alt.Y("count():Q", title="queries"))
            .properties(height=180)
        )
        rule = alt.Chart(pd.DataFrame({"x": [RAG_RELEVANCE_THRESHOLD]})).mark_rule(color=_STATUS_CRITICAL, strokeDash=[4, 4]).encode(x="x:Q")
        st.altair_chart(dist_chart + rule, width="stretch")
        st.caption(f"Dashed line: RAG_RELEVANCE_THRESHOLD = {RAG_RELEVANCE_THRESHOLD} — queries left of it pass the relevance gate.")

st.markdown("## Panel 4b — confidence & similarity by outcome")
st.caption(
    "p3m3 permanent item #27 — accepted (supported) vs. rejected-against-corpus (insufficient) vs. "
    "not-against-corpus (not_applicable), each broken out by rag_mode so a force_rag test case (which "
    "bypasses the relevance gate) can't silently blend into auto mode's real numbers — a real confound "
    "found while proving this query live before this panel existed."
)
if completed.empty or retrievals.empty:
    st.caption("Needs both http_completed and retrieval events in this window — select Event kind = (all).")
else:
    # Join by request_id, same as the raw SQL query this panel replaces —
    # done here in pandas since the dashboard already has both event kinds
    # as fetched DataFrames, not a second DB round trip.
    distance_by_request = {
        p.get("request_id"): p["distances"][0]
        for p in retrievals["payload"]
        if p.get("request_id") and p.get("distances")
    }
    outcome_rows = []
    for p in completed["payload"]:
        response = p.get("response")
        if not isinstance(response, dict) or not response.get("status"):
            continue
        outcome_rows.append(
            {
                "status": response["status"],
                "rag_mode": response.get("rag_mode", "auto"),
                "confidence": response.get("answer", {}).get("confidence"),
                "distance": distance_by_request.get(p.get("request_id")),
                # Cost tied to outcome, added same session per accounting/
                # financial request — answers "how much are we actually
                # spending on rejected/off-topic questions vs. accepted
                # ones," not just unit cost per call (Panel 5 already
                # covers the aggregate input/output split; this is cost
                # attributed BY outcome specifically).
                "cost_usd": response.get("cost_usd"),
            }
        )
    outcome_df = pd.DataFrame(outcome_rows).dropna(subset=["confidence"])

    if outcome_df.empty:
        st.caption("No joinable /ask outcomes (with both a status and a matching retrieval event) in this window.")
    else:
        summary = (
            outcome_df.groupby(["status", "rag_mode"])
            .agg(
                n=("status", "size"),
                confidence_mean=("confidence", "mean"),
                distance_mean=("distance", "mean"),
                cost_usd_mean=("cost_usd", "mean"),
                cost_usd_total=("cost_usd", "sum"),
            )
            .round(6)
            .reset_index()
        )
        st.dataframe(summary, width="stretch", hide_index=True)
        st.caption(
            f"Total spend across this window's joinable outcomes: ${outcome_df['cost_usd'].sum():.6f} — "
            f"${outcome_df.loc[outcome_df['status'] == 'not_applicable', 'cost_usd'].sum():.6f} of that on "
            "not-against-corpus questions the gate never attempted to ground (accounting/financial framing: "
            "spend that produced no citable answer, not necessarily wasted, but worth knowing)."
        )

        box_cols = st.columns(2)
        conf_box = (
            alt.Chart(outcome_df)
            .mark_boxplot(extent="min-max")
            .encode(
                x=alt.X("status:N", title=None),
                y=alt.Y("confidence:Q", title="confidence"),
                color=alt.Color("status:N", scale=alt.Scale(domain=list(_STATUS_COLOR_BY_OUTCOME.keys()), range=list(_STATUS_COLOR_BY_OUTCOME.values())), legend=None),
            )
            .properties(height=220, title="Confidence by outcome")
        )
        box_cols[0].altair_chart(conf_box, width="stretch")

        dist_by_outcome = outcome_df.dropna(subset=["distance"])
        if not dist_by_outcome.empty:
            dist_box = (
                alt.Chart(dist_by_outcome)
                .mark_boxplot(extent="min-max")
                .encode(
                    x=alt.X("status:N", title=None),
                    y=alt.Y("distance:Q", title="top-1 distance"),
                    color=alt.Color("status:N", scale=alt.Scale(domain=list(_STATUS_COLOR_BY_OUTCOME.keys()), range=list(_STATUS_COLOR_BY_OUTCOME.values())), legend=None),
                )
                .properties(height=220, title="Top-1 distance by outcome")
            )
            box_cols[1].altair_chart(dist_box, width="stretch")

st.markdown("## Panel 5 — cost")
if not ask_responses:
    st.caption("No cost data in this window.")
else:
    total_cost = sum(r.get("cost_usd") or 0 for r in ask_responses)
    input_cost = sum(r.get("input_cost_usd") or 0 for r in ask_responses)
    output_cost = sum(r.get("output_cost_usd") or 0 for r in ask_responses)
    cols = st.columns(3)
    cols[0].metric("Total cost (this window)", f"${total_cost:.6f}")
    cols[1].metric("Input", f"${input_cost:.6f}")
    cols[2].metric("Output", f"${output_cost:.6f}")
    st.caption(
        "Aggregate across ALL sessions/users in this event window, unlike the Ask demo's per-session sidebar "
        "total. Free-tier calls show real per-token value, not necessarily money actually billed."
    )

st.markdown("## Panel 6 — raw events (forensics)")
st.dataframe(
    df[["created_at", "kind", "id"]].sort_values("created_at", ascending=False),
    width="stretch",
    hide_index=True,
)
with st.expander("Inspect one event's full payload"):
    selected_id = st.selectbox("Event id", df["id"].tolist())
    st.json(df.loc[df["id"] == selected_id, "payload"].iloc[0])
st.caption("No credentials are ever captured in this log — see operational_audit.py's own docstring.")
