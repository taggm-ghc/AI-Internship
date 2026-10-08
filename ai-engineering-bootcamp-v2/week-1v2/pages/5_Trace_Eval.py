"""Phase 4 (Enforce): Trace Evaluation, baseline vs measured after-fix, ship decision.

Primary source: eval_results/trace_eval_baseline.json and
eval_results/trace_eval_after_fix_measured.json (contract in trace_eval_view.py).
Fallback: read-only DB (internship.eval_check_results, run_label in
'baseline','after_fix_measured'). Retracted simulated runs are never displayed.
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import trace_eval_view as tv
from ui_theme import apply_custom_css

RESULTS_DIR = ROOT / "eval_results"

st.set_page_config(page_title="Trace Eval", layout="wide", initial_sidebar_state="expanded")
apply_custom_css()


@st.cache_data(ttl=60)
def load_db_run(label):
    """Read-only fallback from the DB; returns a contract-shaped run or None."""
    try:
        import db
        from sqlalchemy import text
        session = db.get_session()
        try:
            # SQLAlchemy 2 connection: text() + dict params (a bare "%s" + list raised
            # "List argument must consist only of tuples or dictionaries" and hid the DB rows).
            df = pd.read_sql(
                text("SELECT trace_id, check_name, pass, reason FROM internship.eval_check_results "
                     "WHERE run_label = :label ORDER BY trace_id, check_name"),
                session.bind, params={"label": label})
        finally:
            session.close()
        return tv.run_from_db_rows(df.to_dict("records"), label)
    except Exception as exc:  # DB optional; page must still render
        st.caption(f"DB fallback unavailable for {label}: {type(exc).__name__}")
        return None


def pct(x):
    return "n/a" if x is None else f"{x:.1f}%"


DECISION_COLOR = {"SHIP": "green", "MITIGATE": "orange", "BLOCK": "red"}


def decision_badge(rate, run=None, after=False):
    d = tv.decision_for_rate(rate)
    st.markdown(f"**Decision by checks:** :{DECISION_COLOR[d]}[{tv.decision_label(d, run, after)}]")


st.title("Trace Evaluation: Phase 4 (Enforce)")
st.caption("Code-based checks on Harmony traces. Thresholds: >=95% SHIP, 85-95% MITIGATE, <85% BLOCK. "
           "Only measured runs are shown; the earlier simulated after-fix numbers were retracted.")

if st.button("Refresh data"):
    st.cache_data.clear()
    st.rerun()

base, after = tv.load_runs(RESULTS_DIR)
source = "JSON files (eval_results/)"
if base is None:
    base = load_db_run("baseline")
    source = "database fallback (read-only)"
if after is None:
    after = load_db_run("after_fix_measured")
    if after is not None and base is not None and source.startswith("JSON"):
        source = "JSON baseline + database after-fix"

if base is None:
    st.warning("No baseline results found (eval_results/trace_eval_baseline.json or DB run_label 'baseline').")
    st.stop()

st.caption(f"Data source: {source}. Baseline generated: {base.get('generated_at')}; "
           f"traces: {base.get('source_traces')}")

st.info(f"**TL;DR — generated from the measured results**\n\n{tv.generated_tldr(base, after)}")
st.caption("This summary is assembled from the loaded result JSON/DB contract; it is not a new model judgement.")

# ---- Fix description + limitations (prominent) ----
fix = (after or {}).get("fix")
st.divider()
if after is None:
    st.info("After-fix run not yet measured. Showing baseline only.")
elif fix:
    st.subheader(f"Fix under test: {fix.get('name')}")
    st.markdown(f"**Module:** `{fix.get('module')}`")
    st.markdown(fix.get("description") or "")
    st.warning(f"**Limitations (read before trusting the delta):** {fix.get('limitations') or 'none stated'}")
else:
    st.warning("After-fix run has no fix description recorded; treat its delta with caution.")

# ---- Overall metric ----
st.subheader("Overall pass rate (traces passing all checks)")
rb = tv.overall_rate_pct(base)
c1, c2, c3 = st.columns(3)
with c1:
    st.metric(f"Baseline ({base['overall']['pass']}/{base['overall']['total']})", f"{rb:.1f}%")
    decision_badge(rb, base)
if after is not None:
    d = tv.overall_delta(base, after)
    with c2:
        st.metric(f"After fix ({after['overall']['pass']}/{after['overall']['total']})",
                  f"{d['after']:.1f}%", delta=f"{d['delta_pp']:+.1f} pp")
        decision_badge(d["after"], after, after=True)
        ret = tv.retention_summary(after)
        if ret:
            st.metric("Replies fully replaced", ret["replaced_text"])
            st.caption(ret["detail"])
    with c3:
        st.metric("Traces fixed (net)", f"{d['pass_delta']:+d}")
else:
    with c2:
        st.metric("After fix", "not yet measured")

if after is not None and after.get("decision_note"):
    st.warning(f"**Decision note:** {after['decision_note']}")

# ---- Per-check ----
st.subheader("Per-check pass rate")
rows = tv.per_check_rows(base, after)
tbl = pd.DataFrame(rows).rename(columns={"before": "baseline %", "after": "after-fix %", "delta_pp": "delta (pp)"})
st.dataframe(tbl.round(1), use_container_width=True, hide_index=True)
chart = tbl.set_index("check")[[c for c in ("baseline %", "after-fix %") if tbl[c].notna().any()]]
if not chart.empty:
    st.bar_chart(chart)

# ---- Drill-down ----
st.subheader("Per-trace drill-down")


def show_group(title, items):
    st.markdown(f"**{title} ({len(items)})**")
    if not items:
        st.caption("None.")
    for r in items:
        with st.expander(f"Trace {r['trace_id']}"):
            fails_b = [f"{k}: {v.get('reason')}" for k, v in r["checks_before"].items() if v.get("status") != "PASS"]
            fails_a = [f"{k}: {v.get('reason')}" for k, v in r["checks_after"].items() if v.get("status") != "PASS"]
            if fails_b:
                st.markdown("Baseline failures: " + "; ".join(fails_b))
            if fails_a:
                st.markdown("After-fix failures: " + "; ".join(fails_a))
            cl, cr = st.columns(2)
            cl.markdown("**Response before**")
            cl.text(r["response_before"] or "(not recorded)")
            cr.markdown("**Response after**")
            cr.text(r["response_after"] or "(not recorded)")


if after is not None:
    flips = tv.trace_flips(base, after)
    show_group("Fixed (FAIL -> PASS)", flips["fixed"])
    show_group("Regressions (PASS -> FAIL)", flips["regressed"])
    show_group("Still failing", flips["still_failing"])
else:
    failing = [t for t in base["traces"] if not t["all_pass"]]
    st.markdown(f"**Baseline failing traces ({len(failing)})**")
    for t in failing:
        with st.expander(f"Trace {t['trace_id']}"):
            for k, v in t["checks"].items():
                if v.get("status") != "PASS":
                    st.markdown(f"- {k}: {v.get('reason')}")
            st.text(t.get("response_before") or "(response not recorded)")

with st.expander("All traces, baseline check matrix"):
    st.dataframe(pd.DataFrame(
        [{"trace_id": t["trace_id"], **{k: v.get("status") for k, v in t["checks"].items()}}
         for t in base["traces"]]), use_container_width=True, hide_index=True)
