"""Pure helpers for pages/5_Trace_Eval.py (no streamlit/DB imports; unit-testable).

Contract: eval_results/trace_eval_{baseline,after_fix_measured}.json (see page docstring).
Retracted simulated runs ('after_fix', anything containing 'simulated') are never shown.
"""
from __future__ import annotations

import json
from pathlib import Path

ALLOWED_DB_LABELS = ("baseline", "after_fix_measured")
BASELINE_FILE = "trace_eval_baseline.json"
AFTER_FILE = "trace_eval_after_fix_measured.json"


def is_forbidden_label(label) -> bool:
    s = str(label or "").strip().lower()
    return s == "after_fix" or "simulated" in s


def load_run(path):
    """Load a results JSON; return None if absent, unreadable, or a forbidden label."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or is_forbidden_label(data.get("run_label")):
        return None
    return data


def load_runs(results_dir):
    d = Path(results_dir)
    return load_run(d / BASELINE_FILE), load_run(d / AFTER_FILE)


def decision_for_rate(rate: float) -> str:
    """rate in percent."""
    if rate >= 95:
        return "SHIP"
    if rate >= 85:
        return "MITIGATE"
    return "BLOCK"


def overall_rate_pct(run) -> float:
    """Percent pass rate; accepts rate as fraction (<=1) or percent."""
    o = run["overall"]
    if o.get("total"):
        return 100.0 * o["pass"] / o["total"]
    r = float(o.get("rate") or 0)
    return r * 100 if r <= 1 else r


def overall_delta(base, after):
    b, a = overall_rate_pct(base), overall_rate_pct(after)
    return {"before": b, "after": a, "delta_pp": a - b,
            "pass_delta": after["overall"]["pass"] - base["overall"]["pass"]}


def per_check_rows(base, after=None):
    checks = list(base.get("per_check", {}))
    for c in (after or {}).get("per_check", {}):
        if c not in checks:
            checks.append(c)
    rows = []
    for c in checks:
        def rate(run):
            v = (run or {}).get("per_check", {}).get(c)
            return 100.0 * v["pass"] / v["total"] if v and v.get("total") else None
        rb, ra = rate(base), rate(after) if after else None
        rows.append({"check": c, "before": rb, "after": ra,
                     "delta_pp": (ra - rb) if rb is not None and ra is not None else None})
    return rows


def _by_id(run):
    return {t["trace_id"]: t for t in run.get("traces", [])}


def trace_flips(base, after):
    """Return {'fixed': [...], 'regressed': [...], 'still_failing': [...]} lists of trace dicts
    merged as {trace_id, before, after, response_before, response_after, checks_before, checks_after}."""
    b, a = _by_id(base), _by_id(after)
    out = {"fixed": [], "regressed": [], "still_failing": []}
    for tid in b:
        if tid not in a:
            continue
        tb, ta = b[tid], a[tid]
        rec = {"trace_id": tid, "before": tb["all_pass"], "after": ta["all_pass"],
               "response_before": ta.get("response_before") or tb.get("response_before"),
               "response_after": ta.get("response_after"),
               "checks_before": tb.get("checks", {}), "checks_after": ta.get("checks", {})}
        if not tb["all_pass"] and ta["all_pass"]:
            out["fixed"].append(rec)
        elif tb["all_pass"] and not ta["all_pass"]:
            out["regressed"].append(rec)
        elif not tb["all_pass"] and not ta["all_pass"]:
            out["still_failing"].append(rec)
    return out


def run_from_db_rows(rows, run_label):
    """Build a contract-shaped run from DB rows (iterable of dicts with trace_id, check_name,
    pass, reason). Returns None if forbidden label or no rows."""
    if is_forbidden_label(run_label):
        return None
    traces, per_check = {}, {}
    for r in rows:
        t = traces.setdefault(r["trace_id"], {"trace_id": r["trace_id"], "all_pass": True,
                                              "checks": {}, "response_before": None,
                                              "response_after": None})
        ok = bool(r["pass"])
        t["checks"][r["check_name"]] = {"status": "PASS" if ok else "FAIL", "reason": r.get("reason")}
        t["all_pass"] = t["all_pass"] and ok
        pc = per_check.setdefault(r["check_name"], {"pass": 0, "total": 0})
        pc["total"] += 1
        pc["pass"] += int(ok)
    if not traces:
        return None
    n, p = len(traces), sum(t["all_pass"] for t in traces.values())
    rate = 100.0 * p / n
    return {"run_label": run_label, "generated_at": None, "source_traces": "db", "fix": None,
            "per_check": per_check,
            "overall": {"pass": p, "total": n, "rate": rate / 100, "decision": decision_for_rate(rate)},
            "traces": sorted(traces.values(), key=lambda x: str(x["trace_id"]))}


def decision_label(decision: str, run=None, after: bool = False) -> str:
    """Display label. A measured after-fix SHIP is qualified; never a bare SHIP."""
    if after and decision == "SHIP":
        return "SHIP by checks - see caveats"
    return decision


def retention_summary(run):
    """Retention cost of the gate from a run's optional 'retention' object, or None."""
    r = (run or {}).get("retention")
    if not isinstance(r, dict) or "responses_fully_replaced" not in r:
        return None
    total = (run.get("overall") or {}).get("total") or (
        r.get("responses_modified", 0) + r.get("responses_unchanged", 0))
    msr = r.get("mean_sentence_retention")
    return {"replaced_text": f"{r['responses_fully_replaced']}/{total}",
            "detail": (f"{r['responses_fully_replaced']}/{total} replies fully replaced, "
                       f"{r.get('responses_modified')}/{total} modified, "
                       f"{r.get('responses_unchanged')}/{total} unchanged; mean sentence retention "
                       f"{'n/a' if msr is None else f'{msr:.0%}'}")}
