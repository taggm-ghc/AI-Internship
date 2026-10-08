"""p3m3 item #81: sidebar API triage stops at the earliest failing step, and never shows the host."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_client as ac

HOST = "sentinel-host-do-not-show.example"
BASE = f"https://{HOST}"


class Resp:
    def __init__(self, code, body=None, text="{}"):
        self.status_code, self._body, self.text = code, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _raise(exc):
    def f(url):
        raise exc
    return f


def _routes(health, target=None):
    def f(url):
        return health if url.endswith("/health") else target
    return f


@pytest.mark.parametrize("base,get,earliest", [
    ("", None, "1. API address configured"),
    (BASE, _raise(httpx.ConnectError(f"connect failed to {BASE}")), "2. API reachable"),
    (BASE, _raise(httpx.ReadTimeout(f"timed out reading {BASE}")), "3. API awake"),
    (BASE, _routes(Resp(429, text="Too Many Requests")), "3. API awake"),
    (BASE, _routes(Resp(500, {"status": "error"})), "4. App healthy"),
    (BASE, _routes(Resp(200, {"status": "ok"}), Resp(502, {})), "5. /providers/status retried"),
])
def test_earliest_failure(base, get, earliest):
    steps = ac.triage_api(base, "/providers/status", http_get=get)
    assert ac.earliest_failure(steps)["step"] == earliest
    assert HOST not in " ".join(s["detail"] for s in steps)


def test_404_points_at_an_older_deployment():
    steps = ac.triage_api(BASE, "/corpus-summary", http_get=_routes(Resp(200, {"status": "ok"}), Resp(404, {})))
    assert "older than this UI" in ac.earliest_failure(steps)["detail"]


def test_all_pass_when_it_works_now():
    steps = ac.triage_api(BASE, "/providers/status", http_get=_routes(Resp(200, {"status": "ok"}), Resp(200, [])))
    assert ac.earliest_failure(steps) is None and steps[-1]["detail"].startswith("works now")


def test_failure_reason_is_specific_and_host_free():
    assert ac.failure_reason(0, {"error": "Timed out waiting for the configured API (/x)."}).startswith("Timed out")
    assert ac.failure_reason(503, {"detail": "busy"}) == "HTTP 503: busy"
    assert ac.failure_reason(0, "junk") == "No response from the configured API."


def test_sidebar_uses_triage_not_bare_not_connected():
    src = (Path(__file__).resolve().parents[1] / "MVP_Layered_Ask.py").read_text()
    assert 'triage_api(base_url, "/providers/status")' in src and 'triage_api(base_url, "/health")' in src
    assert "Not connected" not in src


def test_provider_status_carries_credential_url_from_config(monkeypatch):
    """p3m3 item #82: each configured provider has its key page in config, and /providers/status passes it on."""
    import providers
    monkeypatch.setattr(providers, "is_retired", lambda *a, **k: False)  # no DB: the local DB is production
    monkeypatch.setattr(providers, "_rank_by_frontier", lambda entries: entries)
    monkeypatch.setattr(providers, "_local_provider_entries", lambda: [])
    for p in providers.load_provider_chain():
        assert p.credential_url and p.credential_url.startswith("https://"), p.provider
        monkeypatch.delenv(p.api_key_env, raising=False)
    rows = {r["provider"]: r for r in providers.get_provider_status()}
    groq = rows["groq"]
    assert groq["status"].startswith("unavailable") and groq["credential_url"] == "https://console.groq.com/keys"


def test_sidebar_links_missing_credentials():
    src = (Path(__file__).resolve().parents[1] / "MVP_Layered_Ask.py").read_text()
    assert 'f"[{state}]({_cred_url}) — get a key"' in src and '_cred_url.startswith("https://")' in src
