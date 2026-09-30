"""Phase 4 (Enforce): Trace Evaluation Results and Ship Decision Matrix.

Displays code-based check results for all 20 Harmony traces with before/after
comparison, severity breakdown, and ship decision thresholds.

Run: streamlit run MVP_Layered_Ask.py
"""

import streamlit as st
import pandas as pd
from pathlib import Path
import sys

# Setup
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
from ui_theme import apply_custom_css
from ui_widgets import base_url_sidebar_widget

st.set_page_config(page_title="Trace Eval", layout="wide", initial_sidebar_state="expanded")
apply_custom_css()


# ============================================================================
# Helper Functions
# ============================================================================

@st.cache_data(ttl=60)
def load_traces():
    """Load all 20 traces from database."""
    session = db.get_session()
    query = """
        SELECT id, user_input, response::text, retrieved_context::text,
               source, channel, created_at
        FROM internship.traces
        ORDER BY id
    """
    df = pd.read_sql(query, session.bind)
    session.close()
    return df


@st.cache_data(ttl=60)
def load_annotations():
    """Load trace annotations including category and open-code notes."""
    session = db.get_session()
    query = """
        SELECT trace_id, open_code_notes, failure_category, pass_fail, reason_if_fail
        FROM internship.trace_annotations
        ORDER BY trace_id
    """
    df = pd.read_sql(query, session.bind)
    session.close()
    return df


@st.cache_data(ttl=60)
def load_check_results(run_label=None):
    """Load check results, optionally filtered by run_label."""
    session = db.get_session()
    if run_label:
        query = f"""
            SELECT trace_id, check_name, check_type, pass, reason, latency_ms, run_label, created_at
            FROM internship.eval_check_results
            WHERE run_label = %s
            ORDER BY trace_id, check_name
        """
        df = pd.read_sql(query, session.bind, params=[run_label])
    else:
        query = """
            SELECT trace_id, check_name, check_type, pass, reason, latency_ms, run_label, created_at
            FROM internship.eval_check_results
            ORDER BY trace_id, check_name, run_label
        """
        df = pd.read_sql(query, session.bind)
    session.close()
    return df


@st.cache_data(ttl=60)
def load_categories():
    """Load failure categories with severity."""
    session = db.get_session()
    query = """
        SELECT category, description, severity, count_failing, count_total
        FROM internship.failure_categories
        ORDER BY category
    """
    df = pd.read_sql(query, session.bind)
    session.close()
    return df


@st.cache_data(ttl=60)
def get_available_run_labels():
    """Get list of unique run_labels from eval_check_results."""
    session = db.get_session()
    query = """
        SELECT DISTINCT run_label FROM internship.eval_check_results
        ORDER BY run_label DESC
    """
    df = pd.read_sql(query, session.bind)
    session.close()
    return df['run_label'].tolist() if not df.empty else []


def compute_metrics(results_df, run_label=None):
    """Compute pass/fail metrics for the given run."""
    if run_label:
        results_df = results_df[results_df['run_label'] == run_label]

    if results_df.empty:
        return {
            'total_traces': 0, 'passed_traces': 0, 'pass_rate': 0.0,
            'by_check': {}, 'failures_by_severity': {}
        }

    # Overall
    traces = results_df['trace_id'].unique()
    total_traces = len(traces)

    # Count traces that pass ALL checks
    passed_traces = 0
    for trace in traces:
        trace_results = results_df[results_df['trace_id'] == trace]
        if trace_results['pass'].all():
            passed_traces += 1

    pass_rate = 100.0 * passed_traces / total_traces if total_traces > 0 else 0.0

    # By check
    by_check = {}
    for check in results_df['check_name'].unique():
        check_results = results_df[results_df['check_name'] == check]
        n_pass = check_results['pass'].sum()
        n_total = len(check_results)
        by_check[check] = {'pass': n_pass, 'total': n_total, 'rate': 100.0 * n_pass / n_total}

    return {
        'total_traces': total_traces, 'passed_traces': passed_traces, 'pass_rate': pass_rate,
        'by_check': by_check
    }


def ship_decision(metrics):
    """Determine ship recommendation based on metrics."""
    pass_rate = metrics['pass_rate']

    if pass_rate >= 95:
        return '🚀 SHIP', 'green', 'Pass rate ≥95%: ready for production'
    elif pass_rate >= 85:
        return '🟡 MITIGATE', 'orange', 'Pass rate 85-95%: apply targeted fixes before ship'
    else:
        return '🛑 BLOCK', 'red', 'Pass rate <85%: too many failures to ship'


# ============================================================================
# Main Page
# ============================================================================

st.title("🔍 Trace Evaluation: Phase 4 (Enforce)")
st.markdown("**Harmony SMS bot evaluation results** — 20 traces, 3 code-based checks, before/after metrics")

# Load data
traces_df = load_traces()
annotations_df = load_annotations()
check_results_df = load_check_results()
categories_df = load_categories()
run_labels = get_available_run_labels()

if check_results_df.empty:
    st.warning("⚠️  No check results yet. Run `python3 scripts/check_functions.py --save` first.")
    st.stop()

# ============================================================================
# Control Panel
# ============================================================================
st.divider()
col1, col2, col3 = st.columns(3)

with col1:
    selected_trace = st.selectbox(
        "Select Trace",
        ["All 20 Traces"] + list(traces_df['id'].values),
        key="trace_selector"
    )

with col2:
    if run_labels:
        selected_run = st.selectbox(
            "Run Label",
            run_labels,
            key="run_selector"
        )
    else:
        st.info("No runs found")
        selected_run = None

with col3:
    st.write("")  # spacer
    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

st.divider()

# ============================================================================
# Metrics Summary
# ============================================================================

if selected_run:
    metrics = compute_metrics(check_results_df, run_label=selected_run)

    if metrics['total_traces'] > 0:
        st.subheader(f"📊 Metrics: {selected_run}")

        cols = st.columns(4)
        with cols[0]:
            st.metric("Pass Rate", f"{metrics['pass_rate']:.1f}%",
                     delta=f"{metrics['passed_traces']}/{metrics['total_traces']} traces")

        for i, (check_name, check_stats) in enumerate(metrics['by_check'].items(), 1):
            if i < len(cols):
                with cols[i]:
                    st.metric(
                        check_name.replace('check_', ''),
                        f"{check_stats['rate']:.0f}%",
                        delta=f"{check_stats['pass']}/{check_stats['total']}"
                    )

        # Ship decision banner
        st.divider()
        decision, color, reason = ship_decision(metrics)
        st.markdown(f"### :{color}[{decision}]")
        st.markdown(f"**Reason:** {reason}")
        st.markdown(f"**Thresholds:** ≥95% ship · 85-95% mitigate · <85% block")

# ============================================================================
# Detailed Results Table
# ============================================================================

st.divider()
st.subheader("📋 Detailed Check Results")

if selected_run:
    # Filter by trace and run
    results = check_results_df[check_results_df['run_label'] == selected_run].copy()

    if selected_trace != "All 20 Traces":
        results = results[results['trace_id'] == selected_trace]

    if not results.empty:
        # Pivot for readability: rows=traces, cols=checks
        pivot = results.pivot_table(
            index='trace_id',
            columns='check_name',
            values='pass',
            aggfunc='first'
        )

        # Color coding
        def color_pass(val):
            if val is True:
                return 'background-color: #90EE90'  # light green
            elif val is False:
                return 'background-color: #FFB6C6'  # light red
            return ''

        st.dataframe(
            pivot.style.applymap(color_pass),
            use_container_width=True
        )

        # Reasons for failures
        st.subheader("Failure Reasons")
        failures = results[~results['pass']].sort_values('trace_id')
        if not failures.empty:
            for _, row in failures.iterrows():
                st.markdown(f"**{row['trace_id']} — {row['check_name']}**")
                st.markdown(f"> {row['reason']}")
        else:
            st.success("✅ All checks passed!")
    else:
        st.info("No results for selected filters")

# ============================================================================
# Before/After Comparison (if both runs exist)
# ============================================================================

if len(run_labels) >= 2:
    st.divider()
    st.subheader("📈 Before/After Comparison")

    baseline_label = [r for r in run_labels if 'baseline' in r.lower()]
    baseline_label = baseline_label[0] if baseline_label else run_labels[0]

    after_label = [r for r in run_labels if r != baseline_label][0] if len(run_labels) > 1 else None

    if after_label:
        metrics_before = compute_metrics(check_results_df, baseline_label)
        metrics_after = compute_metrics(check_results_df, after_label)

        cols = st.columns(3)
        with cols[0]:
            st.metric(
                f"Pass Rate ({baseline_label})",
                f"{metrics_before['pass_rate']:.1f}%",
                delta=None
            )
        with cols[1]:
            delta = metrics_after['pass_rate'] - metrics_before['pass_rate']
            st.metric(
                f"Pass Rate ({after_label})",
                f"{metrics_after['pass_rate']:.1f}%",
                delta=f"{delta:+.1f}pp"
            )
        with cols[2]:
            st.metric(
                "Improvement",
                f"{metrics_after['passed_traces'] - metrics_before['passed_traces']}",
                delta=f"{metrics_after['passed_traces']}/{metrics_after['total_traces']} traces"
            )

# ============================================================================
# Trace Details (expandable)
# ============================================================================

st.divider()
st.subheader("📝 Trace Details")

if selected_trace != "All 20 Traces":
    trace = traces_df[traces_df['id'] == selected_trace].iloc[0]
    anno = annotations_df[annotations_df['trace_id'] == selected_trace].iloc[0] if not annotations_df[annotations_df['trace_id'] == selected_trace].empty else None

    with st.expander(f"Trace {selected_trace} — Full Details"):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**User Input**")
            st.text(trace['user_input'][:500] if trace['user_input'] else "(none)")

            if anno:
                st.markdown("**Open-Code Notes**")
                st.text(anno['open_code_notes'] if anno['open_code_notes'] else "(none)")

        with col2:
            st.markdown("**Bot Response**")
            st.text(str(trace['response'])[:500] if trace['response'] else "(none)")

            if anno:
                st.markdown("**Category**")
                st.text(f"{anno['failure_category']} — {anno['pass_fail']}")
else:
    st.info("Select a single trace to view details")

st.divider()
st.caption(f"Render: {base_url_sidebar_widget()}")
