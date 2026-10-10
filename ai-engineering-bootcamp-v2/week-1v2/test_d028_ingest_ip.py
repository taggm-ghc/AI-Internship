"""D-028 / U13: the ingest guard and the accept endpoint must not persist the raw
client IP (event fields or the document_versions.client_ip column). Sentinel
203.0.113.77. No network, no DB: tripwire engine + stubs.
Run: .venv/bin/python -m pytest test_d028_ingest_ip.py -q
"""
import json
import unittest
from unittest import mock

import db


class _DbTripwire:
    def __getattr__(self, name):
        raise AssertionError(f"test touched the database (engine.{name}); the local DB is production")


db._engine = _DbTripwire()

import operational_store  # noqa: E402

operational_store.get_admin_engine = lambda *a, **k: _DbTripwire()

import main  # noqa: E402

SENTINEL = "203.0.113.77"
DIGEST = b"\x01" * 32


class _Req:
    headers = {}


class NoRawIpInIngestPaths(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(main, "get_remote_address", return_value=SENTINEL)
        p.start()
        self.addCleanup(p.stop)
        k = mock.patch.object(main, "_DEBUG_KEY_DIGEST", DIGEST)
        k.start()
        self.addCleanup(k.stop)

    def test_guard_event_fields_hold_tag_not_ip(self):
        with mock.patch.object(main, "_ingest_authenticated", return_value=True):
            extra, _ = main._guard_ingest(_Req(), "ordinary text")
        self.assertNotIn("client_ip", extra)
        self.assertEqual(extra["ip_version"], 4)
        self.assertTrue(extra["source_tag"])
        self.assertNotIn(SENTINEL, json.dumps(extra))
        self.assertNotIn("203.0.113", json.dumps(extra))

    def test_guard_without_key_records_class_only(self):
        with mock.patch.object(main, "_DEBUG_KEY_DIGEST", None), mock.patch.object(main, "_ingest_authenticated", return_value=False):
            extra, _ = main._guard_ingest(_Req(), "ordinary text")
        self.assertNotIn("source_tag", extra)
        self.assertNotIn(SENTINEL, json.dumps(extra))

    def test_stage_receives_tag_not_ip(self):
        with mock.patch.object(main, "_ingest_authenticated", return_value=False):
            extra, flags = main._guard_ingest(_Req(), "ordinary text")
        with mock.patch.object(main, "document_exists", return_value=True), \
             mock.patch.object(main, "find_live_document_by_content_sha", return_value=None), \
             mock.patch.object(main, "stage_document_version", return_value={"version": 2}) as stage, \
             mock.patch.object(main, "upsert_chunks", return_value=3):
            main._ingest_or_stage(None, None, "d", "ordinary text", None, {"content_sha256": "h"}, extra, flags)
        self.assertNotIn(SENTINEL, json.dumps(stage.call_args.args, default=str))
        self.assertEqual(stage.call_args.args[6], extra["source_tag"])

    def test_accept_endpoint_event_fields_have_no_ip(self):
        row = {"text_content": "ordinary text", "metadata": None, "provenance": {}}
        with mock.patch.object(main, "_require_valid_key"), \
             mock.patch.object(main, "_ingest_authenticated", return_value=True), \
             mock.patch.object(main, "get_document_version", return_value=row), \
             mock.patch.object(main, "_accept_version_now", return_value=2) as acc:
            main.post_accept_ingest_version.__wrapped__(_Req(), main.AcceptVersionRequest(document_id="d", version=2))
        extra = acc.call_args.args[5]
        self.assertNotIn("client_ip", extra)
        self.assertNotIn(SENTINEL, json.dumps(extra))
        self.assertIn("source_tag", extra)

    def test_no_log_record_carries_ip(self):
        with self.assertLogs(level="DEBUG") as cm, mock.patch.object(main, "_ingest_authenticated", return_value=True):
            main.logger.warning("probe")
            main._guard_ingest(_Req(), "ordinary text")
        self.assertNotIn(SENTINEL, "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
