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
                # Added same session per user request -- mean, not p50/p90/
                # p99 like Panel 2 (avg-hides-the-tail research finding
                # still applies in principle, but per-outcome-group n here
                # is small, currently 1-29 -- percentiles would be noisy,
                # not informative, at this granularity).
                "latency_ms": p.get("latency_ms"),
                "tokens_used": response.get("tokens_used"),
            }
        )
    outcome_df = pd.DataFrame(outcome_rows).dropna(subset=["confidence"])

    if outcome_df.empty:
        st.caption("No joinable /ask outcomes (with both a status and a matching retrieval event) in this window.")
    else:
        # cost_uscents, not cost_usd, for the per-row table -- these are
        # sub-cent amounts (~$0.00015-$0.0003), so dollars means several
        # leading zeros before any meaningful digit; cents removes two of
        # them. The total-spend caption below stays in dollars -- that's a
        # single, larger, human-facing summary number where dollars still
        # read naturally, unlike a table of many sub-cent per-row means.
        outcome_df["cost_uscents"] = outcome_df["cost_usd"] * 100
        summary = (
            outcome_df.groupby(["status", "rag_mode"])
            .agg(
                n=("status", "size"),
                confidence_mean=("confidence", "mean"),
                distance_mean=("distance", "mean"),
                cost_uscents_mean=("cost_uscents", "mean"),
                cost_uscents_total=("cost_uscents", "sum"),
                latency_ms_mean=("latency_ms", "mean"),
                tokens_used_mean=("tokens_used", "mean"),
            )
            .round(6)
            .reset_index()
        )
        # Explicit column_config, added 2026-09-24 -- user-reported real bug:
        # st.dataframe's default float display doesn't show enough decimal
        # places to distinguish these sub-cent cost values (e.g. 0.000151,
        # 0.000182, 0.000191, 0.000194 USD all rounded to the same "$0.0002"
        # at default precision, even though .round(6) upstream already
        # keeps the real distinct values in the data itself -- a display
        # bug, not a data bug, confirmed by inspecting the DataFrame's own
        # values directly before this fix).
        # Row background color by status, added 2026-09-24 -- user-caught
        # real bug: the guidance text below says "(green)"/"(amber)"/
        # "(gray)" but the table itself had no color at all, only the
        # boxplots further down did. Reuses _STATUS_COLOR_BY_OUTCOME (same
        # mapping the boxplots already use) rather than a second palette,
        # at ~20% opacity ("33" hex alpha) so text stays readable over a
        # saturated fill — a background tint, not colored text (dataviz
        # convention: text carries text tokens, a colored fill carries
        # identity/status, never the reverse).
        def _row_color(row):
            color = _STATUS_COLOR_BY_OUTCOME.get(row["status"], "")
            return [f"background-color: {color}33" if color else ""] * len(row)

        st.dataframe(
            summary.style.apply(_row_color, axis=1),
            width="stretch",
            hide_index=True,
            column_config={
                "cost_uscents_mean": st.column_config.NumberColumn("cost_uscents_mean", format="%.4f¢"),
                "cost_uscents_total": st.column_config.NumberColumn("cost_uscents_total", format="%.4f¢"),
                "confidence_mean": st.column_config.NumberColumn("confidence_mean", format="%.3f"),
                "distance_mean": st.column_config.NumberColumn("distance_mean", format="%.3f"),
                "latency_ms_mean": st.column_config.NumberColumn("latency_ms_mean", format="%d ms"),
                "tokens_used_mean": st.column_config.NumberColumn("tokens_used_mean", format="%d"),
            },
        )
        with st.expander("How to read this table"):
            st.markdown(
                "- **`supported`** (green): high confidence + low distance is the healthy case — the "
                "pipeline worked as designed.\n"
                "- **`insufficient`** (amber): the gate judged the question topically plausible (distance "
                "near/under the threshold), but confidence should be low/zero — the model correctly "
                "declined rather than guessed. Low confidence here is *correct* behavior, not a defect.\n"
                "- **`not_applicable`** (gray): the gate never attempted retrieval (distance well over "
                "threshold), so any confidence shown comes from the model's own pretrained knowledge, "
                "**not** the corpus — a *high* confidence here is the confident-and-ungrounded risk (see "
                "permanent item #19), not a sign of quality.\n"
                "- **`rag_mode` split**: `force_rag` rows are deliberate demo overrides that bypass the "
                "relevance gate — don't average them into `auto`'s real production numbers (this is exactly "
                "the confound found while building this panel, see the caption above it).\n"
                "- **Cost/latency/tokens**: useful for spend and performance profile per outcome — e.g. "
                "whether declining is cheaper than answering, not for judging correctness on their own."
            )
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

st.markdown("## Panel 5b — retrieval degradation signals")
st.caption(
    "Soft retrieval failures: when the tool runs but finds nothing relevant, or when source freshness "
    "is declining. Early warning signs that the corpus is stale or retrieval quality is silently degrading."
)
if not df[df["kind"] == "agent_run"].empty:
    agent_runs = df[df["kind"] == "agent_run"].copy()
    agent_runs["created_at"] = pd.to_datetime(agent_runs["created_at"])

    # Count tool_found_nothing outcomes (soft failures)
    found_nothing = agent_runs["payload"].apply(lambda p: p.get("grounding") == "tool_found_nothing").sum()
    total_agent_runs = len(agent_runs)
    found_nothing_rate = (found_nothing / total_agent_runs * 100) if total_agent_runs else 0

    # Status: amber if >15%, red if >25%
    status_color = _STATUS_GOOD
    status_text = "✅ Healthy"
    if found_nothing_rate > 25:
        status_color = _STATUS_CRITICAL
        status_text = "🔴 Critical"
    elif found_nothing_rate > 15:
        status_color = _STATUS_WARNING
        status_text = "⚠️ Warning"

    cols = st.columns(3)
    cols[0].metric("Tool found nothing rate", f"{found_nothing_rate:.1f}%")
    cols[1].metric("Status", status_text)
    cols[2].metric("Sample size (agent runs)", total_agent_runs)

    if total_agent_runs > 0:
        # Trend over time
        found_nothing_by_hour = agent_runs.set_index("created_at").resample("1h").apply(
            lambda window: (window["payload"].apply(lambda p: p.get("grounding") == "tool_found_nothing").sum() / len(window) * 100)
            if len(window) > 0 else 0
        )
        trend_df = found_nothing_by_hour.reset_index()
        trend_df.columns = ["time", "found_nothing_rate"]

        if not trend_df.empty:
            trend_chart = (
                alt.Chart(trend_df)
                .mark_line(point=True, color=_STATUS_WARNING)
                .encode(
                    x=alt.X("time:T", title="time"),
                    y=alt.Y("found_nothing_rate:Q", title="tool_found_nothing rate (%)"),
                    tooltip=[alt.Tooltip("time:T", title="time"), alt.Tooltip("found_nothing_rate:Q", title="rate (%)", format=".1f")],
                )
                .properties(height=200)
            )
            threshold_line = alt.Chart(pd.DataFrame({"threshold": [15]})).mark_rule(color=_STATUS_WARNING, strokeDash=[4, 4]).encode(y="threshold:Q")
            st.altair_chart(trend_chart + threshold_line, width="stretch")
            st.caption("Dashed line: 15% threshold (amber alert). Sustained >25% triggers critical alert.")
else:
    st.caption("No agent_run events in this window.")

st.markdown("## Panel 5c — cost forecasting")
st.caption(
    "Project current spend forward. Detects cost drift early and enables capacity planning. Based on "
    "per-run token costs from the past hour/day."
)
if not ask_responses or not completed.empty:
    # Compute cost trajectory
    recent_cost = sum(r.get("cost_usd") or 0 for r in ask_responses) if ask_responses else 0
    recent_count = len(ask_responses) if ask_responses else 0
    cost_per_call = (recent_cost / recent_count) if recent_count > 0 else 0

    # Project forward
    calls_per_day_estimate = (recent_count / (limit / 200)) if limit else 0  # Rough: scale by event window
    projected_daily = cost_per_call * calls_per_day_estimate * 100 if calls_per_day_estimate else 0  # Scaled up
    projected_weekly = projected_daily * 7
    projected_monthly = projected_daily * 30

    cols = st.columns(4)
    cols[0].metric("Cost per call (mean)", f"${cost_per_call:.6f}")
    cols[1].metric("Projected daily", f"${projected_daily:.4f}")
    cols[2].metric("Projected weekly", f"${projected_weekly:.3f}")
    cols[3].metric("Projected monthly", f"${projected_monthly:.2f}")

    st.caption(
        f"⚠️ Projection caveat: based on {limit} recent events. Accuracy depends on whether recent "
        "traffic is representative. Use the raw Panel 5 totals, not this forecast, as ground truth."
    )
else:
    st.caption("No cost data in this window.")

st.markdown("## Panel 5d — SLO tracking")
st.caption(
    "Are we meeting our service-level objectives? Target latency p95 < 3000ms, error rate < 2%, "
    "availability 99.0% (hourly windows account for cold-start bounces)."
)
if completed.empty:
    st.caption("No http_completed events in this window.")
else:
    # SLO targets (configurable)
    SLO_P95_LATENCY_MS = 3000
    SLO_ERROR_RATE = 0.02
    SLO_AVAILABILITY_PCT = 99.0

    # Compute actual metrics
    latencies = completed["payload"].apply(lambda p: p.get("latency_ms")).dropna()
    actual_p95_latency = latencies.quantile(0.95) if len(latencies) > 0 else None

    errors = completed["payload"].apply(
        lambda p: p.get("http_status", 200) >= 400 or p.get("error") is not None
    )
    error_count = errors.sum()
    total_count = len(completed)
    actual_error_rate = (error_count / total_count) if total_count > 0 else 0

    # Availability (hourly granularity: any hour with >1 error counts as a "failed hour")
    completed_copy = completed.copy()
    completed_copy["hour"] = pd.to_datetime(completed_copy["created_at"]).dt.floor("1h")
    hours_with_errors = (
        completed_copy.groupby("hour")
        .apply(lambda g: (g["payload"].apply(lambda p: p.get("http_status", 200) >= 400 or p.get("error") is not None).sum() > 0))
        .sum()
    )
    total_hours = completed_copy["hour"].nunique()
    actual_availability = ((total_hours - hours_with_errors) / total_hours * 100) if total_hours > 0 else 100

    # Status indicators
    slo_cols = st.columns(3)

    lat_status = "✅" if actual_p95_latency and actual_p95_latency < SLO_P95_LATENCY_MS else "🔴"
    slo_cols[0].metric(
        f"{lat_status} Latency (p95)",
        f"{actual_p95_latency:.0f}ms" if actual_p95_latency else "N/A",
        delta=f"target: {SLO_P95_LATENCY_MS}ms",
        delta_color="inverse"
    )

    err_status = "✅" if actual_error_rate < SLO_ERROR_RATE else "🔴"
    slo_cols[1].metric(
        f"{err_status} Error rate",
        f"{actual_error_rate:.1%}",
        delta=f"target: <{SLO_ERROR_RATE:.1%}",
        delta_color="inverse"
    )

    avail_status = "✅" if actual_availability >= SLO_AVAILABILITY_PCT else "🔴"
    slo_cols[2].metric(
        f"{avail_status} Availability (hourly)",
        f"{actual_availability:.1f}%",
        delta=f"target: ≥{SLO_AVAILABILITY_PCT}%",
        delta_color="inverse"
    )

    st.caption(
        "Latency: p95 (not average) captures tail risk. Availability: hourly windows "
        "(hours with ≥1 error are \"failed\") to account for cold-start bounces. "
        "Error rate includes any HTTP ≥400 or logged error."
    )

st.markdown("## Panel 7 — raw events (forensics)")
st.dataframe(
    df[["created_at", "kind", "id"]].sort_values("created_at", ascending=False),
    width="stretch",
    hide_index=True,
)
with st.expander("Inspect one event's full payload"):
    selected_id = st.selectbox("Event id", df["id"].tolist())
    st.json(df.loc[df["id"] == selected_id, "payload"].iloc[0])
st.caption("No credentials are ever captured in this log — see operational_audit.py's own docstring.")
