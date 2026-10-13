"""Shared pytest fixtures for week-1v2.

`base` serves test_all_stages.py, a live-server script (real, billable calls)
that pytest also collects. Live tests run only on explicit opt-in:

    API_BASE_URL=... pytest --run-live-stages

DEPRECATED alias: API_BASE_URL set without the flag still enables live tests
through 2026-10-25, with a DeprecationWarning; after that date it is ignored.
Without either, live tests skip with a stated reason. The URL is never printed.
"""

import datetime
import os
import warnings

import httpx
import pytest

ALIAS_LAST_DAY = datetime.date(2026, 10, 25)


def pytest_addoption(parser):
    parser.addoption("--run-live-stages", action="store_true", default=False,
                     help="run live-server tests (also needs API_BASE_URL in the environment)")


def live_decision(flag: bool, env, today=None):
    """Return (enabled, skip_reason, warn_message). Never includes the URL value."""
    today = today or datetime.date.today()
    has_url = bool(str(env.get("API_BASE_URL", "")).strip())
    if flag:
        if has_url:
            return True, None, None
        return False, "live test skipped: API_BASE_URL not set", None
    if has_url and today <= ALIAS_LAST_DAY:
        return True, None, ("API_BASE_URL alone enabling live tests is a deprecated alias for "
                            "--run-live-stages and stops working after 2026-10-25; "
                            "pass --run-live-stages instead.")
    return False, "live test skipped: --run-live-stages not passed", None


@pytest.fixture(scope="session")
def base(request) -> str:
    ok, reason, warn = live_decision(request.config.getoption("--run-live-stages"), os.environ)
    if not ok:
        pytest.skip(reason)
    if warn:
        warnings.warn(warn, DeprecationWarning, stacklevel=1)
    url = os.environ["API_BASE_URL"].strip().rstrip("/")
    try:
        up = httpx.get(f"{url}/docs", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        up = False
    if not up:
        pytest.skip("live server unreachable; live-server test skipped")
    return url
