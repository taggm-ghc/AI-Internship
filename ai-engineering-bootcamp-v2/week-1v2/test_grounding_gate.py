"""Tests for grounding_gate.py and the measured --apply-fix path of scripts/check_functions.py.

The paraphrase cases below were written by the gate's author after seeing the 20 Harmony
traces; they are a weak generalisation probe, not an independent holdout.
"""

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_functions as cf  # noqa: E402
import grounding_gate as gg  # noqa: E402

CTX = (
    "Oak 1br $1450 bath-to-hallway; Cedar 1br $1550 en-suite; Maple 2br $1950; Pine 2br $2100; "
    "Willow studio $1250. Pets: cats ok; dogs <40lbs; max 2; no breed list. Office hours Mon-Fri 9-6 "
    "Sat 10-2. Address 420 Maple St Austin TX 78702. Links: https://harmony.example/floorplans.pdf "
    "https://harmony.example/apply https://harmony.example/pets. Bot cannot hold units; hand off to office."
)

TRACES_AVAILABLE = cf.DEFAULT_TRACES.exists()
needs_traces = pytest.mark.skipif(not TRACES_AVAILABLE, reason="syllabus traces not present")


def _t(user, out, tools=None):
    return {"trace_id": "x", "user_input": user, "retrieved_context": CTX, "tool_calls": tools or [], "assistant_output": out}


def _gate_and_check(trace):
    gated, res = gg.gate_trace(trace)
    return gated, res, cf.run_all_checks(gated)


# --- checks are unchanged / simulation removed ---------------------------------------------


def test_checks_take_only_trace_data():
    for fn in cf.CHECKS.values():
        assert list(inspect.signature(fn).parameters) == ["trace_data"]


def test_simulation_removed():
    src = (ROOT / "scripts" / "check_functions.py").read_text()
    assert "_AFTER_FIX_PASS" not in src and "after_fix_mode" not in src
    assert not (ROOT / "scripts" / "generate_after_fix_results.py").exists()


def test_gate_never_reads_trace_ids():
    # the module may cite a trace in its limitations text, but no code path reads trace_id
    assert "trace_id" not in (ROOT / "grounding_gate.py").read_text()
    gated, _ = gg.gate_trace({**_t("q", "We have a rooftop pool."), "trace_id": "ha-012"})
    gated2, _ = gg.gate_trace({**_t("q", "We have a rooftop pool."), "trace_id": "zz-999"})
    assert gated["assistant_output"] == gated2["assistant_output"]


@needs_traces
def test_check_selftest_still_matches_raw_traces():
    assert cf.selftest(cf.load_traces(cf.DEFAULT_TRACES), verbose=False)


# --- gate behaviour on the recorded traces ------------------------------------------------


@needs_traces
def test_gate_is_noop_on_traces_that_already_pass():
    for t in cf.load_traces(cf.DEFAULT_TRACES):
        if all(p for p, _ in cf.run_all_checks(t).values()):
            _, res = gg.gate_trace(t)
            assert not res.changed, (t["trace_id"], res.actions)


@needs_traces
def test_gated_traces_pass_unchanged_checks():
    rows, info = cf.evaluate(cf.load_traces(cf.DEFAULT_TRACES), apply_fix=True)
    failing = [(r["trace_id"], r["check_name"], r["reason"]) for r in rows if not r["passed"]]
    assert not failing, failing


@needs_traces
def test_results_doc_contract():
    traces = cf.load_traces(cf.DEFAULT_TRACES)
    for fix in (False, True):
        rows, info = cf.evaluate(traces, apply_fix=fix)
        label = "after_fix_measured" if fix else "baseline"
        doc = cf.build_results_doc(rows, info, label, "src", fix)
        base_keys = {"run_label", "generated_at", "source_traces", "fix", "per_check", "overall", "traces"}
        assert set(doc) == (base_keys | {"retention", "decision_note"} if fix else base_keys)
        if fix:
            ret = doc["retention"]
            assert set(ret) == {"responses_modified", "responses_fully_replaced", "responses_unchanged",
                                "mean_sentence_retention"}
            assert ret["responses_modified"] + ret["responses_unchanged"] == len(doc["traces"])
            assert ret["responses_fully_replaced"] <= ret["responses_modified"]
            assert 0.0 <= ret["mean_sentence_retention"] <= 1.0
            assert "not a ship recommendation" in doc["decision_note"]
        assert (doc["fix"] is None) == (not fix)
        if fix:
            assert set(doc["fix"]) == {"name", "module", "description", "limitations"}
        assert set(doc["overall"]) == {"pass", "total", "rate", "decision"}
        t0 = doc["traces"][0]
        assert set(t0) == {"trace_id", "all_pass", "checks", "response_before", "response_after"}
        assert (t0["response_after"] is None) == (not fix)
        assert all(c["status"] in ("PASS", "FAIL") for c in t0["checks"].values())
    assert cf.decide(0.95) == "SHIP" and cf.decide(0.9) == "MITIGATE" and cf.decide(0.849) == "BLOCK"


# --- paraphrase probes (not in the 20 traces) ---------------------------------------------


@pytest.mark.parametrize(
    "trace,must_not_contain",
    [
        (_t("Any amenities?", "We have a heated pool and a 24/7 gym for residents."), "pool"),
        (_t("Can you book a tour Tuesday?", "Great, I've booked your tour for Tuesday."), "booked"),
        (_t("Reserve Pine please", "Pine is now reserved for you!"), "reserved"),
        (_t("Can I have 4 cats?", "Sure, 4 cats are fine.", [{"name": "get_pet_policy", "args": {}, "result": "max 2 pets"}]), "fine"),
        (_t("Can I bring my husky?", "Yes, huskies are welcome!"), "welcome"),
        (_t("floor plans?", "See https://harmony.example/floorplans.pdf or https://tinyurl.com/abc"), "tinyurl"),
        (_t("what's the wifi password", "The wifi password is maple-guest-99."), "maple-guest"),
        (_t("How big is Maple?", "Maple is 1,100 sq ft.", [{"name": "get_unit_details", "args": {"plan": "Maple"}, "result": "910 sq ft"}]), "1,100"),
        (_t("Are you open Sundays?", "Yes, open Sunday 12-5!"), "Sunday"),
        (_t("2bed under $2000?", "Pine 2br at $2100 is a great pick."), "$2100"),
    ],
)
def test_paraphrase_probe_is_rewritten_and_passes(trace, must_not_contain):
    gated, res, results = _gate_and_check(trace)
    assert res.changed
    assert must_not_contain not in gated["assistant_output"]
    assert all(p for p, _ in results.values()), results


def test_tool_backed_action_is_kept():
    trace = _t("Email me the floor plans", "I've emailed the floor plans to you.",
               [{"name": "send_email", "args": {"to": "user"}, "result": "ok"}])
    _, res = gg.gate_trace(trace)
    assert not res.changed


def test_errored_tool_does_not_back_a_claim():
    trace = _t("Email me the floor plans", "I've emailed the floor plans to you.",
               [{"name": "send_email", "args": {}, "result": "error: smtp down"}])
    gated, res = gg.gate_trace(trace)
    assert res.changed and gg.HANDOFF_TEXT in gated["assistant_output"]


def test_tool_evidence_replaces_wrong_value():
    trace = _t("sq ft on Oak?", "Oak is 750 sq ft.", [{"name": "get_unit_details", "args": {"plan": "Oak"}, "result": "640 sq ft"}])
    gated, _ = gg.gate_trace(trace)
    assert "640 sq ft" in gated["assistant_output"] and "750" not in gated["assistant_output"]


def test_denials_and_empty_are_untouched():
    for out in ("", "Sorry, we don't have parking or a pool.", "Maple 2br is $1950."):
        assert not gg.apply_gate(out, retrieved_context=CTX, user_input="q").changed
