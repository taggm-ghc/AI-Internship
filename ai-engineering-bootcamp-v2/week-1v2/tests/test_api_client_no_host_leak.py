"""api_client error messages are shown in the UI (st.json/st.write/st.code), so
they must never contain the API host -- on Render that is the live URL
(2026-10-04 sanitization audit; same leak class as todo-digest item #26)."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_client

HOST = "sentinel-host-do-not-show.example"
URL = f"https://{HOST}/health"


class _Bounce:
    status_code = 429
    text = "Too Many Requests"
    headers = {}


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


@pytest.mark.parametrize("fake", [
    _raise(httpx.ConnectError(f"connect failed to {URL}")),
    _raise(httpx.ReadTimeout(f"timed out reading {URL}")),
    _raise(httpx.RemoteProtocolError(f"protocol error talking to {URL}")),
    lambda *a, **k: _Bounce(),
])
def test_errors_never_show_host(monkeypatch, fake):
    monkeypatch.setattr(api_client.time, "sleep", lambda s: None)
    monkeypatch.setattr(api_client.httpx, "get", fake)
    monkeypatch.setattr(api_client.httpx, "post", fake)
    for method in ("GET", "POST"):
        status, body = api_client.call_json(method, URL, {})
        assert status == 0 and HOST not in str(body) and "/health" in str(body)
    status, text, *_ = api_client.call_stream(URL, {})
    assert status == 0 and HOST not in text and "/health" in text


def test_local_advice_kept():
    assert "Start the API server first" in api_client.unreachable_message("http://127.0.0.1:8000/ask")
