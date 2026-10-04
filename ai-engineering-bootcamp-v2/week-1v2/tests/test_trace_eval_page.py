import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trace_eval_view as v


def _run(label, passes, fix=None):
    traces = [{"trace_id": f"t{i}", "all_pass": ok,
               "checks": {"c1": {"status": "PASS" if ok else "FAIL", "reason": "r"}},
               "response_before": f"b{i}", "response_after": f"a{i}"} for i, ok in enumerate(passes)]
    p = sum(passes)
    return {"run_label": label, "generated_at": "x", "source_traces": "s", "fix": fix,
            "per_check": {"c1": {"pass": p, "total": len(passes)}},
            "overall": {"pass": p, "total": len(passes), "rate": p / len(passes),
                        "decision": v.decision_for_rate(100 * p / len(passes))},
            "traces": traces}


def _write(tmp_path):
    b = _run("baseline", [True, False, False, True])
    a = _run("after_fix_measured", [True, True, False, False],
             fix={"name": "n", "module": "m", "description": "d", "limitations": "l"})
    (tmp_path / v.BASELINE_FILE).write_text(json.dumps(b))
    (tmp_path / v.AFTER_FILE).write_text(json.dumps(a))


def test_load_and_missing(tmp_path):
    assert v.load_runs(tmp_path) == (None, None)
    _write(tmp_path)
    b, a = v.load_runs(tmp_path)
    assert b["run_label"] == "baseline" and a["fix"]["limitations"] == "l"
    (tmp_path / v.AFTER_FILE).unlink()
    assert v.load_runs(tmp_path)[1] is None


def test_forbidden_labels(tmp_path):
    for lab in ("after_fix", "baseline_simulated"):
        p = tmp_path / "x.json"
        p.write_text(json.dumps(_run(lab, [True])))
        assert v.load_run(p) is None
    assert v.run_from_db_rows([{"trace_id": 1, "check_name": "c", "pass": True, "reason": ""}], "after_fix") is None


def test_delta_and_decision(tmp_path):
    _write(tmp_path)
    b, a = v.load_runs(tmp_path)
    d = v.overall_delta(b, a)
    assert d["before"] == 50.0 and d["after"] == 50.0 and d["delta_pp"] == 0.0
    assert [v.decision_for_rate(x) for x in (95, 94.9, 85, 84.9)] == ["SHIP", "MITIGATE", "MITIGATE", "BLOCK"]
    rows = v.per_check_rows(b, a)
    assert rows[0]["check"] == "c1" and rows[0]["delta_pp"] == 0.0
    assert v.per_check_rows(b)[0]["after"] is None


def test_flips(tmp_path):
    _write(tmp_path)
    b, a = v.load_runs(tmp_path)
    f = v.trace_flips(b, a)
    assert [r["trace_id"] for r in f["fixed"]] == ["t1"]
    assert [r["trace_id"] for r in f["regressed"]] == ["t3"]
    assert [r["trace_id"] for r in f["still_failing"]] == ["t2"]


def test_db_rows():
    rows = [{"trace_id": 1, "check_name": "c", "pass": True, "reason": ""},
            {"trace_id": 2, "check_name": "c", "pass": False, "reason": "bad"}]
    r = v.run_from_db_rows(rows, "baseline")
    assert r["overall"]["pass"] == 1 and r["overall"]["decision"] == "BLOCK"


def test_retention_and_decision_label():
    assert v.decision_label("SHIP", None, after=True) == "SHIP by checks - see caveats"
    assert v.decision_label("SHIP", None, after=False) == "SHIP"
    assert v.decision_label("BLOCK", None, after=True) == "BLOCK"
    assert v.retention_summary({"overall": {"total": 20}}) is None
    r = {"overall": {"total": 20}, "retention": {"responses_modified": 14, "responses_fully_replaced": 9,
                                                  "responses_unchanged": 6, "mean_sentence_retention": 0.4357}}
    s = v.retention_summary(r)
    assert s["replaced_text"] == "9/20" and "9/20 replies fully replaced" in s["detail"] and "44%" in s["detail"]


def test_page_renders_real_results():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "pages" / "5_Trace_Eval.py"), default_timeout=60)
    at.run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert any("Replies fully replaced" in x for x in labels)
    assert any("SHIP by checks - see caveats" in m.value for m in at.markdown)
