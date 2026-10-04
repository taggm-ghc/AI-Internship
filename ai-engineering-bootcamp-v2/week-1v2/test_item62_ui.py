"""p3m3 item #62 W2: api_client header plumbing and key-gated Streamlit pages.
All HTTP is mocked; nothing touches a live API or the database."""
from unittest.mock import MagicMock, patch

import pytest
from streamlit.testing.v1 import AppTest

import api_client

KEY = "s3cret-debug-key-xyz"

STATS = {
    "generated_at": "2026-10-01T00:00:00Z", "window_hours": 24, "min_count": 5,
    "totals": {"requests": 40, "ask_calls": 20, "agent_runs": 3, "ingests": 1, "errors": 2},
    "latency_ms_by_path": {"/ask": {"n": 20, "p50": 400, "p95": 1200}, "/agent": {"n": 3, "p50": None, "p95": None}},
    "error_rate": 0.05, "grounded_answer_hit_rate": None,
    "cost_usd": {"total": 0.12, "per_ask": 0.006}, "agent_found_nothing_rate": None,
    "suppressed": ["grounded_answer_hit_rate", "agent_found_nothing_rate"],
}
EVENTS = [{
    "id": "evt-unique-1", "kind": "http_completed", "created_at": "2026-10-01T00:00:00Z",
    "payload": {"path": "/ask", "latency_ms": 100, "http_status": 200},
}]


def _resp(code, body):
    r = MagicMock()
    r.status_code = code
    r.text = "{}"
    r.json.return_value = body
    return r


def test_debug_headers_helper():
    assert api_client.debug_headers("") is None
    assert api_client.debug_headers(None) is None
    assert api_client.debug_headers("   ") is None
    assert api_client.debug_headers("k") == {"X-Debug-Key": "k"}


@pytest.mark.parametrize("headers", [None, {"X-Debug-Key": "k"}])
def test_call_json_get_passes_headers(headers):
    with patch("api_client.httpx.get", return_value=_resp(200, {"ok": 1})) as get:
        assert api_client.call_json("GET", "http://x/y", headers=headers) == (200, {"ok": 1})
    assert get.call_args.kwargs["headers"] == headers


def _fake_call_json(events_status=200):
    calls = []

    def fake(method, url, payload=None, headers=None):
        calls.append((url, headers))
        if "/stats/summary" in url:
            return 200, STATS
        if "/debug/events" in url:
            if not headers:
                return 401, {"detail": "unauthorized"}
            if headers.get("X-Debug-Key") != KEY:
                return 401, {"detail": "unauthorized"}
            return events_status, EVENTS
        return 200, {}
    return fake, calls


def _all_text(at):
    parts = [str(e.value) for e in at.markdown] + [str(e.value) for e in at.caption] + [str(m.value) for m in at.metric]
    parts += [str(e.value) for e in at.error] + [str(e.value) for e in at.info]
    return "\n".join(parts)


def _dash():
    return AppTest.from_file("pages/1_Observability_Dashboard.py", default_timeout=30)


def test_dashboard_no_key_shows_aggregates_and_no_raw():
    fake, calls = _fake_call_json()
    with patch("api_client.call_json", fake):
        at = _dash().run()
    assert not at.exception
    text = _all_text(at)
    assert "suppressed (too few events)" in text  # null values degrade gracefully
    assert "restricted: enter the debug key" in text
    assert len(at.dataframe) == 1  # only the aggregate latency table
    assert "Panel 7" not in text
    assert not any("/debug/events" in u for u, _ in calls)


def test_dashboard_with_key_shows_raw_table():
    fake, calls = _fake_call_json()
    with patch("api_client.call_json", fake):
        at = _dash().run()
        at.sidebar.text_input(key="debug_key_input").input(KEY).run()
    assert not at.exception
    assert any("/debug/events" in u and h == {"X-Debug-Key": KEY} for u, h in calls)
    assert "Panel 7" in _all_text(at)
    assert len(at.dataframe) >= 2
    # the key never reaches rendered output
    assert KEY not in _all_text(at)
    assert KEY not in str(at.json) and KEY not in repr([e.value for e in at.get("json")])
    assert not any(KEY in u for u, _ in calls)


def test_dashboard_invalid_key_shows_invalid_and_no_data():
    fake, _ = _fake_call_json()
    with patch("api_client.call_json", fake):
        at = _dash().run()
        at.sidebar.text_input(key="debug_key_input").input("wrong").run()
    text = _all_text(at)
    assert "invalid key" in text
    assert "Panel 7" not in text
    assert len(at.dataframe) == 1


def _agent_resp(trace):
    return 200, {"answer": "Hello", "grounding": "tool_sources", "sources": ["doc-a"], "references": [], "trace": trace}


def test_agent_page_without_key_says_trace_restricted():
    calls = []

    def fake(method, url, payload=None, headers=None):
        calls.append(headers)
        return _agent_resp([])

    with patch("api_client.call_json", fake):
        at = AppTest.from_file("pages/4_Agent.py", default_timeout=30).run()
        q = [t for t in at.text_input if t.label == "Ask the agent something"][0]
        q.input("what?").run()
        at.button[0].click().run()
    assert not at.exception
    text = _all_text(at)
    assert "Trace restricted" in text
    assert "Hello" in text and "doc-a" in text
    assert calls and calls[0] is None


def test_agent_page_with_key_sends_header_and_shows_trace():
    calls = []
    trace = [{"role": "user", "content": "what?"}]

    def fake(method, url, payload=None, headers=None):
        calls.append(headers)
        return _agent_resp(trace)

    with patch("api_client.call_json", fake):
        at = AppTest.from_file("pages/4_Agent.py", default_timeout=30).run()
        at.sidebar.text_input(key="debug_key_input").input(KEY).run()
        [t for t in at.text_input if t.label == "Ask the agent something"][0].input("what?").run()
        at.button[0].click().run()
    assert calls[0] == {"X-Debug-Key": KEY}
    assert "Trace restricted" not in _all_text(at)
    assert KEY not in _all_text(at)


def test_golden_eval_requires_debug_key(monkeypatch):
    import golden_eval

    monkeypatch.delenv("DEBUG_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        golden_eval.debug_headers_from_env()
    assert "DEBUG_API_KEY" in str(exc.value)
    monkeypatch.setenv("DEBUG_API_KEY", "abc")
    assert golden_eval.debug_headers_from_env() == {"X-Debug-Key": "abc"}
