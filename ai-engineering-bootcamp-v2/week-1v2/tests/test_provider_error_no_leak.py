"""Provider (OpenAI SDK) error text must never reach an API client: only a
coarse category and a reference id, with the detail in the server log,
redacted (p3m3 item #69, D-027). Only providers.ProviderUnavailableError,
whose text this project writes, is shown as-is."""
import logging
import re
import sys
from pathlib import Path
from unittest import mock

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402
from providers import ProviderUnavailableError  # noqa: E402

ORG = "org-SENTINELorg123"
PROJ = "proj_SENTINELproj456"
HOST = "sentinel-host-do-not-show.example"
KEY = "sk-proj-SENTINELKEY0123456789abcdef"
LEAKY = f"Project {PROJ} does not exist in {ORG} at https://{HOST}/v1 (key {KEY})\r\nINJECTED log line"
REQ = httpx.Request("POST", f"https://{HOST}/v1/chat/completions")


def _status(cls, code):
    return cls(LEAKY, response=httpx.Response(code, request=REQ), body=None)


LEAKY_ERRORS = [
    pytest.param(_status(openai.PermissionDeniedError, 403), "rejected the request", id="403"),
    pytest.param(_status(openai.NotFoundError, 404), "rejected the request", id="404"),
    pytest.param(_status(openai.BadRequestError, 400), "rejected the request", id="400"),
    pytest.param(_status(openai.InternalServerError, 500), "internal error", id="500"),
    pytest.param(openai.APIConnectionError(message=LEAKY, request=REQ), "could not reach", id="connection"),
    pytest.param(openai.APITimeoutError(request=REQ), "could not reach", id="timeout"),
    pytest.param(openai.OpenAIError(LEAKY), "request failed", id="sdk-base-class"),
]


@pytest.mark.parametrize("exc,category", LEAKY_ERRORS)
def test_client_detail_hides_provider_text(exc, category, caplog):
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        http = main._map_openai_error(exc)
    detail = http.detail
    assert http.status_code == 502
    for secret in (ORG, PROJ, HOST, KEY, "INJECTED"):
        assert secret not in detail
    assert category in detail
    ref = re.search(r"Reference: ([0-9a-f]{12})", detail).group(1)
    logged = [r.getMessage() for r in caplog.records if ref in r.getMessage()]
    assert len(logged) == 1, "exactly one server log line carries the reference"
    line = logged[0]
    assert KEY not in line and "\n" not in line and "\r" not in line


def test_log_keeps_diagnostics(caplog):
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        main._map_openai_error(_status(openai.PermissionDeniedError, 403))
    line = caplog.records[-1].getMessage()
    assert "PermissionDeniedError" in line and "403" in line and ORG in line


def test_own_provider_message_shown_unchanged():
    msg = "No configured provider was available to serve this request."
    http = main._map_openai_error(ProviderUnavailableError(msg))
    assert http.status_code == 502 and http.detail == f"OpenAI request failed: {msg}"


@pytest.mark.parametrize("exc,code", [
    (_status(openai.AuthenticationError, 401), 401),
    (_status(openai.RateLimitError, 429), 429),
])
def test_known_mappings_unchanged(exc, code):
    http = main._map_openai_error(exc)
    assert http.status_code == code and ORG not in http.detail


def test_agent_endpoint_hides_provider_text():
    import agent_service  # /agent imports run_agent from here at call time
    # Local DB config is production: never let a test write audit events.
    with mock.patch("main.record_event"), mock.patch("operational_audit.record_event"), \
            mock.patch.object(agent_service, "run_agent", side_effect=_status(openai.PermissionDeniedError, 403)):
        r = TestClient(main.app).post("/agent", json={"question": "What is the refund window?"})
    assert r.status_code == 502
    assert ORG not in r.text and HOST not in r.text and "Reference:" in r.text


def test_no_inline_provider_echo_left():
    """Only _provider_failure_detail may format exception text for a caller,
    and only for ProviderUnavailableError."""
    src = Path(main.__file__).read_text()
    assert src.count('OpenAI request failed: {exc}') == 1
    assert 'detail=f"OpenAI request failed' not in src
