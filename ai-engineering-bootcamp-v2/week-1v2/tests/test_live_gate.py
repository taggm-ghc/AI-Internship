"""Live-test gate (owner decision #7): flag primary, API_BASE_URL alias deprecated."""
import datetime

from conftest import ALIAS_LAST_DAY, live_decision

URL = {"API_BASE_URL": "http://example.invalid"}
DAY = ALIAS_LAST_DAY


def test_flag_enables_without_warning():
    assert live_decision(True, URL, DAY) == (True, None, None)


def test_flag_without_url_skips_with_reason():
    ok, reason, warn = live_decision(True, {}, DAY)
    assert not ok and "API_BASE_URL" in reason and warn is None


def test_alias_enables_with_deprecation_message():
    ok, reason, warn = live_decision(False, URL, DAY)
    assert ok and reason is None and "--run-live-stages" in warn and "2026-10-25" in warn


def test_alias_ignored_after_cutoff():
    ok, reason, _ = live_decision(False, URL, DAY + datetime.timedelta(days=1))
    assert not ok and "--run-live-stages" in reason


def test_neither_skips_with_clear_reason_and_no_url_leak():
    ok, reason, warn = live_decision(False, {}, DAY)
    assert not ok and warn is None and "--run-live-stages" in reason
    assert "example.invalid" not in live_decision(False, URL, DAY + datetime.timedelta(days=1))[1]
