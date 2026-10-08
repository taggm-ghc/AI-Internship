"""Shared HTTP-client layer for every Streamlit page in this app —
MVP_Layered_Ask.py (the entry script) and pages/1_Observability_Dashboard.py.

Extracted 2026-09-22 from MVP_Layered_Ask.py, where these functions originally
lived: the dashboard page initially duplicated a minimal copy of the
cold-start-retry logic rather than sharing it (each Streamlit multi-page
file runs standalone, so importing a sibling *page* script as a module
would re-execute its top-level UI code) — duplication is exactly the
drift risk a shared, non-page module avoids. Pure Python, no Streamlit
imports, safe for any page to import without side effects.
"""
import json
import os
import time
from pathlib import Path

import httpx

# run.sh records its actual host:port here on every start, so the default
# below tracks whichever port is currently active instead of a hardcoded one
# that drifts whenever the API is started on a non-default port. Ported
# 2026-09-13 from ../../ai-eng-bootcamp.vera/streamlit_app.py's equivalent.
_LOCAL_URL_FILE = Path(__file__).resolve().parent / ".faststream-local-url"


def default_api_base_url() -> str:
    # Set as a Render dashboard env var on the deployed Streamlit service
    # (never commit the real value) so the sidebar auto-fills instead of
    # requiring a manual paste on every visit.
    env_url = os.getenv("API_BASE_URL", "").strip()
    if env_url:
        return env_url
    try:
        return _LOCAL_URL_FILE.read_text().strip() or "http://127.0.0.1:8000"
    except FileNotFoundError:
        return "http://127.0.0.1:8000"


def debug_headers(key: str | None) -> dict | None:
    """{"X-Debug-Key": key} only when key is non-empty, else None (so no
    header is sent at all). Pure helper, no Streamlit imports."""
    key = (key or "").strip()
    return {"X-Debug-Key": key} if key else None


def build_payload(
    question: str, model: str | None, force_bad: bool, provider: str | None = None, rag_mode: str = "auto"
) -> dict:
    return {
        "question": question,
        "model": model,
        "force_bad": force_bad,
        "provider": provider,
        "rag_mode": rag_mode,
    }


def build_stream_payload(question: str, model: str | None, provider: str | None = None, rag_mode: str = "auto") -> dict:
    # AskStreamRequest has no force_bad field — the guardrail retry demo
    # only applies to /ask, since a streaming response can't un-send bytes
    # already delivered to the caller (see ask_service.stream_answer).
    # rag_mode added 2026-09-22 — /ask/stream now retrieves too.
    return {"question": question, "model": model, "provider": provider, "rag_mode": rag_mode}


def render_curl(path: str, payload: dict) -> str:
    """Renders a curl command for display only -- never called to make an
    actual request (real calls go through call_json/call_stream with the
    resolved base_url directly). The host is always the literal placeholder
    $API_BASE_URL, never the real base_url: unlike
    ui_widgets.base_url_sidebar_widget's blank input field, the st.code(...)
    block this feeds is always visible with nothing gating it, so taking a
    base_url parameter here at all would just move the same leak that
    widget's docstring describes (a screenshot of the page) from the input
    field to the "Request" curl preview below it.
    """
    body = json.dumps(payload)
    return (
        f'curl -s -X POST $API_BASE_URL{path} '
        f'-H "Content-Type: application/json" '
        f"-d '{body}'"
    )


def _where(url: str) -> str:
    """Path only, never the host: these messages are shown in the UI, and the
    host is the live deployment URL that must not appear in screenshots (same
    leak class as todo-digest item #26 and the Trace Eval footer, 2026-10-04)."""
    return httpx.URL(url).path or "/"


def unreachable_message(url: str) -> str:
    """ConnectError means nothing answered at all — the fix differs by
    whether base_url is local (nobody started uvicorn) or a deployed
    instance (wrong URL, or the service is actually down), so the old
    one-size-fits-all "Start the API server first." was actively wrong
    advice for a visitor to a deployed Streamlit page — they can't start
    someone else's Render service. Bug found 2026-09-15 against a real
    deployed instance."""
    host = httpx.URL(url).host
    if host in ("127.0.0.1", "localhost"):
        return f"Cannot reach the local API ({_where(url)}). Start the API server first."
    return (
        f"Cannot reach the configured API ({_where(url)}) — nothing answered at all. Double-check the "
        "URL, and confirm the service is actually deployed and running in "
        "the Render dashboard (a slow *response*, as opposed to no "
        "response, usually means a free-tier cold start instead — see the "
        "timeout message)."
    )


def timeout_message(url: str) -> str:
    return (
        f"Timed out waiting for the configured API ({_where(url)}). If this is a Render free-tier "
        "deployment, it may be waking from a cold start after a period of "
        "inactivity — that can take up to a minute (see the README's "
        "Test With Curl section) — try again in a moment."
    )


# Render's own edge can bounce a non-browser request (like every httpx call
# this UI makes) with a bare, plain-text 429 while a free-tier instance is
# still cold-booting — confirmed live 2026-09-15: a browser tab hitting the
# same URL got Render's "WAKING UP" interstitial instead, but this UI's POST
# just got bounced, because there's no HTML page to hand back to a
# non-navigational request. Retrying with backoff covers exactly that
# transient window; these delays sum to ~55s, matching Render's own
# documented up-to-a-minute cold start.
COLD_START_RETRY_DELAYS_S = (3.0, 7.0, 15.0, 30.0)


def is_cold_start_bounce(status_code: int, raw_text: str) -> bool:
    """True only for Render's edge-bounce pattern, never for this app's own
    slowapi rate limiter — that one always replies with a JSON object shaped
    like {"error": "Rate limit exceeded: ..."} (see main.py's
    _rate_limit_exceeded_handler), so a real rate-limit response is left
    alone for the caller to show as-is rather than retried."""
    return status_code == 429 and not raw_text.strip().startswith("{")


def cold_start_bounce_message(url: str) -> str:
    total_wait = sum(COLD_START_RETRY_DELAYS_S)
    return (
        f"The configured API ({_where(url)}) kept bouncing this request with a bare 429 across "
        f"{len(COLD_START_RETRY_DELAYS_S) + 1} attempts over ~{total_wait:.0f}s — "
        "consistent with Render's free tier still cold-starting (a plain "
        "browser tab hitting the same URL would show Render's own "
        "\"waking up\" page instead, which this UI's request can't). Open "
        "the URL directly in a browser tab to let it finish waking up, "
        "then try again here."
    )


def call_stream(url: str, payload: dict) -> tuple[int, str, str | None, bool | None, list[str], list[str]]:
    """Like call_json, but for /ask/stream: the body is plain text, not
    JSON, and the only structured metadata comes back via headers — see
    main.py's ask_stream docstring for why a streamed response has nowhere
    else to report which provider/model answered (X-Served-By) or whether
    RAG grounding actually happened (X-RAG-Grounded/X-RAG-Citations, added
    2026-09-22 alongside this endpoint's own rag_mode). X-Skills-Used
    (same date) mirrors AskResponse.skills_used — static project metadata,
    not per-request behavior, see p3m3/skills-and-mcp-inventory.md."""
    response = None
    for delay in (0.0,) + COLD_START_RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            response = httpx.post(url, json=payload, timeout=120.0)
        except httpx.ConnectError:
            return 0, unreachable_message(url), None, None, [], []
        except httpx.TimeoutException:
            return 0, timeout_message(url), None, None, [], []
        except httpx.HTTPError as exc:
            return 0, f"Request to the configured API ({_where(url)}) failed ({type(exc).__name__}).", None, None, [], []
        if not is_cold_start_bounce(response.status_code, response.text):
            break

    if is_cold_start_bounce(response.status_code, response.text):
        return 0, cold_start_bounce_message(url), None, None, [], []

    served_by = response.headers.get("X-Served-By")
    grounded_header = response.headers.get("X-RAG-Grounded")
    grounded = None if grounded_header is None else grounded_header == "true"
    citations_header = response.headers.get("X-RAG-Citations", "")
    citations = [c for c in citations_header.split(",") if c]
    skills_header = response.headers.get("X-Skills-Used", "")
    skills_used = [s for s in skills_header.split(",") if s]
    if response.status_code >= 400:
        try:
            return response.status_code, json.dumps(response.json(), indent=2), served_by, grounded, citations, skills_used
        except json.JSONDecodeError:
            return response.status_code, response.text, served_by, grounded, citations, skills_used
    return response.status_code, response.text, served_by, grounded, citations, skills_used


def failure_reason(status: int, data) -> str:
    """The specific reason a call_json result failed, host-free (call_json's own messages use _where)."""
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    if isinstance(data, dict) and isinstance(data.get("detail"), str):
        return f"HTTP {status}: {data['detail']}"
    return f"HTTP {status}" if status else "No response from the configured API."


TRIAGE_TIMEOUT_S = 65.0  # same budget as call_json's GET: a Render free-tier wake can take up to a minute


def triage_api(base_url: str, failing_path: str, http_get=None) -> list[dict]:
    """Walk the request path in order and stop at the earliest failing step (p3m3 item #81, R1 2026-10-07).

    Steps: address configured -> reachable -> awake (not timing out or edge-bounced) -> app healthy (/health)
    -> the failing request retried once. Each step is {step, ok, detail}; details never contain the host."""
    get = http_get or (lambda url: httpx.get(url, timeout=TRIAGE_TIMEOUT_S))
    steps = []

    def add(step, ok, detail):
        steps.append({"step": step, "ok": ok, "detail": detail})
        return ok

    base = (base_url or "").strip().rstrip("/")
    if not add("1. API address configured", base.lower().startswith(("http://", "https://")),
               "set" if base.lower().startswith(("http://", "https://")) else
               "no usable API address: set the API Base URL (local) or API_BASE_URL (deployed UI)"):
        return steps
    try:
        r = get(f"{base}/health")
    except httpx.ConnectError:
        add("2. API reachable", False, "nothing answered at the configured address (wrong address, or the "
            "service is stopped or suspended)")
        return steps
    except httpx.TimeoutException:
        add("2. API reachable", True, "the address accepted the connection")
        add("3. API awake", False, f"no reply within {TRIAGE_TIMEOUT_S:.0f}s; a Render free-tier service may "
            "still be waking up - wait a minute and retry")
        return steps
    except httpx.HTTPError as exc:
        add("2. API reachable", False, f"request failed ({type(exc).__name__})")
        return steps
    add("2. API reachable", True, "the address answered")
    if is_cold_start_bounce(r.status_code, r.text):
        add("3. API awake", False, "Render answered for the service with a bare 429: it is still waking up - "
            "retry in a minute")
        return steps
    add("3. API awake", True, f"replied in {r.elapsed.total_seconds():.1f}s" if getattr(r, "elapsed", None) else "replied")
    try:
        healthy = r.status_code == 200 and r.json().get("status") == "ok"
    except (ValueError, AttributeError):
        healthy = False
    if not add("4. App healthy", healthy, "/health ok" if healthy else f"/health returned HTTP {r.status_code}; "
               "the app is running but unhealthy - its log will say why"):
        return steps
    try:
        again = get(f"{base}{failing_path}")
        ok = again.status_code < 400
        detail = ("works now (it was most likely waking up)" if ok else
                  f"HTTP 404 while the app is healthy: the deployed API has no {failing_path} - it is probably "
                  "older than this UI; redeploy the API" if again.status_code == 404 else
                  f"still fails with HTTP {again.status_code} while the app is healthy - a problem in that endpoint")
        add(f"5. {failing_path} retried", ok, detail)
    except httpx.HTTPError as exc:
        add(f"5. {failing_path} retried", False, f"failed again ({type(exc).__name__})")
    return steps


def earliest_failure(steps: list[dict]) -> dict | None:
    return next((s for s in steps if not s["ok"]), None)


def call_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    response = None
    for delay in (0.0,) + COLD_START_RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            if method == "POST":
                response = httpx.post(url, json=payload, headers=headers, timeout=120.0)
            else:
                # 65s, not a snappy few seconds: this path also serves
                # /health and /providers/status, and Render's free tier can
                # take up to a minute to wake a cold-started deployment — a
                # short timeout here misreported that wakeup delay as a
                # generic HTTPError instead of ever reaching the clearer
                # cold-start message below.
                response = httpx.get(url, headers=headers, timeout=65.0)
        except httpx.ConnectError:
            return 0, {"error": unreachable_message(url)}
        except httpx.TimeoutException:
            return 0, {"error": timeout_message(url)}
        except httpx.HTTPError as exc:
            return 0, {"error": f"Request to the configured API ({_where(url)}) failed ({type(exc).__name__})."}
        if not is_cold_start_bounce(response.status_code, response.text):
            break

    if is_cold_start_bounce(response.status_code, response.text):
        return 0, {"error": cold_start_bounce_message(url)}

    try:
        return response.status_code, response.json()
    except json.JSONDecodeError:
        return response.status_code, response.text
