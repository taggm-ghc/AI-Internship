"""Tests for p3m3 item #62 W1: X-Debug-Key on /debug/*, keyed /agent trace,
public aggregate-only GET /stats/summary, and the route inventory (D7).

No network, no DB: the local DB is production, so a tripwire engine is
installed on db._engine BEFORE main is imported (any DB touch raises), and
record_event / query_events / store calls are replaced with in-process stubs.
Run: .venv/bin/python -m pytest test_introspection_access.py -q
"""
import hashlib
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import db


class _DbTripwire:
    def __getattr__(self, name):
        raise AssertionError(f"test touched the database (engine.{name}); the local DB is production")


db._engine = _DbTripwire()

import operational_store  # noqa: E402

operational_store.get_admin_engine = lambda *a, **k: _DbTripwire()

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import stats_service  # noqa: E402

TEST_KEY = "unit-test-debug-key-7f3a9c"  # not a real credential
WRONG_KEY = "unit-test-wrong-key"
DIGEST = hashlib.sha256(TEST_KEY.encode()).digest()

# --- D7 route inventory: every route must be listed here, or the build fails.
PUBLIC_ROUTES = {
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/health", "/providers/status", "/summarize", "/analyze-sentiment",
    "/ask", "/ask/stream", "/agent", "/stats/summary",
    "/ingest", "/ingest/batch", "/ingest-pdf",
    "/ingest/versions", "/ingest/versions/diff",
    # Public route with its own fail-closed X-Ingest-Key check (not the debug key).
    "/ingest/versions/accept",
}
DEBUG_KEYED_ROUTES = {"/debug/retrieve", "/debug/similar-documents", "/debug/events", "/debug/corpus-summary"}


def _iter_routes(routes):
    """Flattens app.routes, including FastAPI 0.141's lazily included routers."""
    for route in routes:
        if callable(getattr(route, "effective_candidates", None)):
            yield from _iter_routes(route.effective_candidates())
        else:
            yield route


def _dependency_calls(dependant):
    for dep in getattr(dependant, "dependencies", []) or []:
        yield dep.call
        yield from _dependency_calls(dep)


class _Base(unittest.TestCase):
    def setUp(self):
        main.limiter.reset()
        stats_service.clear_cache()
        self.events = []
        recorder = lambda kind, payload, **kw: self.events.append((kind, payload))  # noqa: E731
        for target in ("main.record_event", "operational_audit.record_event"):
            patcher = mock.patch(target, side_effect=recorder)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def use_key(self, digest):
        patcher = mock.patch.object(main, "_DEBUG_KEY_DIGEST", digest)
        patcher.start()
        self.addCleanup(patcher.stop)


class RouteInventory(unittest.TestCase):
    def test_every_route_is_classified(self):
        paths = {route.path for route in _iter_routes(main.app.routes)}
        self.assertFalse(PUBLIC_ROUTES & DEBUG_KEYED_ROUTES)
        unclassified = paths - PUBLIC_ROUTES - DEBUG_KEYED_ROUTES
        self.assertEqual(unclassified, set(), f"classify these routes as public or keyed: {sorted(unclassified)}")
        self.assertEqual((PUBLIC_ROUTES | DEBUG_KEYED_ROUTES) - paths, set(), "inventory lists routes that no longer exist")

    def test_keyed_routes_carry_the_dependency_and_public_ones_do_not(self):
        for route in _iter_routes(main.app.routes):
            calls = set(_dependency_calls(getattr(route, "dependant", None)))
            if route.path in DEBUG_KEYED_ROUTES or route.path.startswith("/debug"):
                self.assertIn(main.require_debug_key, calls, route.path)
            else:
                self.assertNotIn(main.require_debug_key, calls, route.path)


def _stub_debug_backends(test):
    retrieved = {"ids": ["doc-a::0"], "documents": ["chunk text"], "metadatas": [{"document_id": "doc-a"}], "distances": [0.2]}
    patches = [
        mock.patch.object(main, "_require_valid_key", lambda: None),
        mock.patch.object(main, "_get_embedding_client", lambda: object()),
        mock.patch.object(main, "embed_query", lambda client, q: ([0.0] * 3, 1)),
        mock.patch.object(main, "get_collection", lambda: object()),
        mock.patch.object(main, "query_store", lambda *a, **k: retrieved),
        mock.patch.object(main, "build_candidates", lambda r: []),
        mock.patch.object(main, "get_document_collection", lambda: object()),
        mock.patch.object(main, "find_similar_documents", lambda *a, **k: {"ids": ["doc-b"], "distances": [0.3]}),
        mock.patch.object(main, "query_events", lambda kind=None, limit=200: [
            {"id": "e1", "kind": "http_completed", "payload": {"path": "/ask"}, "created_at": "2026-10-01T00:00:00+00:00"}]),
        mock.patch.object(main, "corpus_summary", lambda sample_size=10: {"document_count": 3, "sample_titles": ["T"]}),
    ]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)


DEBUG_REQUESTS = [
    "/debug/retrieve?q=agents",
    "/debug/similar-documents?document_id=doc-a",
    "/debug/events?limit=5",
    "/debug/corpus-summary?sample_size=2",
]


class DebugAccess(_Base):
    def setUp(self):
        super().setUp()
        _stub_debug_backends(self)

    def test_missing_key_is_401_generic(self):
        self.use_key(DIGEST)
        for url in DEBUG_REQUESTS:
            main.limiter.reset()
            r = self.client.get(url)
            self.assertEqual(r.status_code, 401, url)
            self.assertEqual(r.json(), {"detail": "restricted"})

    def test_wrong_key_is_401(self):
        self.use_key(DIGEST)
        for url in DEBUG_REQUESTS:
            main.limiter.reset()
            r = self.client.get(url, headers={"X-Debug-Key": WRONG_KEY})
            self.assertEqual(r.status_code, 401, url)
            self.assertEqual(r.json(), {"detail": "restricted"})

    def test_unset_env_var_fails_closed_even_with_a_key(self):
        self.use_key(None)
        for url in DEBUG_REQUESTS:
            main.limiter.reset()
            r = self.client.get(url, headers={"X-Debug-Key": TEST_KEY})
            self.assertEqual(r.status_code, 401, url)
            self.assertEqual(r.json(), {"detail": "restricted"})

    def test_right_key_is_200_with_unchanged_shapes(self):
        self.use_key(DIGEST)
        bodies = {url: self.client.get(url, headers={"X-Debug-Key": TEST_KEY}) for url in DEBUG_REQUESTS}
        for url, r in bodies.items():
            self.assertEqual(r.status_code, 200, (url, r.text))
        self.assertEqual(set(bodies[DEBUG_REQUESTS[0]].json()[0]), {"chunk_id", "document_id", "text", "distance", "historical_score"})
        self.assertEqual(bodies[DEBUG_REQUESTS[1]].json(), [{"document_id": "doc-b", "distance": 0.3}])
        self.assertEqual(set(bodies[DEBUG_REQUESTS[2]].json()[0]), {"id", "kind", "payload", "created_at"})
        self.assertEqual(bodies[DEBUG_REQUESTS[3]].json(), {"document_count": 3, "sample_titles": ["T"]})
        # ?query= alias still works too.
        self.assertEqual(self.client.get("/debug/retrieve?query=x", headers={"X-Debug-Key": TEST_KEY}).status_code, 200)

    def test_denial_event_has_path_and_ip_only(self):
        self.use_key(DIGEST)
        self.client.get("/debug/events", headers={"X-Debug-Key": WRONG_KEY})
        denied = [p for k, p in self.events if k == "debug_denied"]
        self.assertEqual(denied, [{"path": "/debug/events", "client_ip": "testclient"}])

    def test_failed_attempts_are_rate_limited_and_lock_out_the_right_key(self):
        self.use_key(DIGEST)
        codes = [self.client.get("/debug/events", headers={"X-Debug-Key": WRONG_KEY}).status_code for _ in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])
        self.assertEqual(self.client.get("/debug/events", headers={"X-Debug-Key": TEST_KEY}).status_code, 429)
        main.limiter.reset()
        self.assertEqual(self.client.get("/debug/events", headers={"X-Debug-Key": TEST_KEY}).status_code, 200)

    def test_successful_calls_do_not_consume_the_failure_budget(self):
        self.use_key(DIGEST)
        for _ in range(8):
            self.assertEqual(self.client.get("/debug/events", headers={"X-Debug-Key": TEST_KEY}).status_code, 200)


AGENT_RESULT = {
    "answer": "Agents call tools (Doe, 2024).",
    "trace": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "search_corpus", "args": {"query": "q"}}]},
        {"role": "tool", "content": "[1] passage text", "tool_call_id": "c1"},
        {"role": "assistant", "content": "Agents call tools (Doe, 2024)."},
    ],
    "grounding": "tool_sources", "sources": ["doc-a::0"], "references": ["Doe, J. (2024). Title."],
    "tool_calls": [], "model_turns": 2, "duration_ms": 10,
}


class AgentTrace(_Base):
    def setUp(self):
        super().setUp()
        import agent_service
        patcher = mock.patch.object(agent_service, "run_agent", lambda q: json.loads(json.dumps(AGENT_RESULT)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, headers=None):
        r = self.client.post("/agent", json={"question": "what do agents do?"}, headers=headers or {})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_trace_empty_without_key_full_with_key_rest_identical(self):
        self.use_key(DIGEST)
        public = self.ask()
        keyed = self.ask({"X-Debug-Key": TEST_KEY})
        self.assertEqual(public["trace"], [])
        self.assertEqual(len(keyed["trace"]), 4)
        for field in ("answer", "grounding", "sources", "references"):
            self.assertEqual(public[field], keyed[field], field)

    def test_wrong_or_unconfigured_key_gives_empty_trace(self):
        self.use_key(DIGEST)
        self.assertEqual(self.ask({"X-Debug-Key": WRONG_KEY})["trace"], [])
        self.assertIn(("debug_denied", {"path": "/agent", "client_ip": "testclient"}), self.events)
        self.use_key(None)
        self.assertEqual(self.ask({"X-Debug-Key": TEST_KEY})["trace"], [])

    def test_anonymous_call_is_not_a_failed_attempt(self):
        self.use_key(DIGEST)
        self.ask()
        self.assertFalse([k for k, _ in self.events if k == "debug_denied"])


class KeyNeverLeaks(_Base):
    def test_key_absent_from_bodies_logs_and_events(self):
        _stub_debug_backends(self)
        import agent_service
        p = mock.patch.object(agent_service, "run_agent", lambda q: json.loads(json.dumps(AGENT_RESULT)))
        p.start()
        self.addCleanup(p.stop)
        self.use_key(DIGEST)
        records = []
        handler = logging.Handler(level=logging.DEBUG)
        handler.emit = lambda record: records.append(record.getMessage() + " " + repr(record.args))
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            bodies = []
            for key in (TEST_KEY, WRONG_KEY, None):
                headers = {"X-Debug-Key": key} if key else {}
                for url in DEBUG_REQUESTS:
                    main.limiter.reset()
                    bodies.append(self.client.get(url, headers=headers).text)
                bodies.append(self.client.post("/agent", json={"question": "q"}, headers=headers).text)
            with mock.patch.object(stats_service, "query_events", lambda kind=None, limit=200: []):
                bodies.append(self.client.get("/stats/summary").text)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        events_blob = json.dumps(self.events, default=str)
        for secret in (TEST_KEY, WRONG_KEY):
            self.assertFalse(any(secret in b for b in bodies), "key echoed in a response body")
            self.assertFalse(any(secret in r for r in records), "key written to a log record")
            self.assertNotIn(secret, events_blob, "key written to an event payload")
        self.assertNotIn("x-debug-key", events_blob.lower())


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _http(path, i, status=200, response=None, error=None, minutes_ago=5):
    return {"id": f"req-{path}-{i}", "kind": "http_completed", "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "payload": {"request_id": f"req-{path}-{i}", "path": path, "http_status": status, "latency_ms": 100.0 + i,
                        "response": response, "error": error, "client_ip": "203.0.113.9"}}


def _ask_response(i, status="supported"):
    return {"status": status, "cost_usd": 0.001, "answer": {"answer": f"SECRET ANSWER TEXT {i}"},
            "references": ["Secret Title (2024)"], "citations": ["doc-a::0"]}


def _agent(i, grounding="tool_sources"):
    return {"id": f"ag-{i}", "kind": "agent_run", "created_at": (NOW - timedelta(minutes=3)).isoformat(),
            "payload": {"request_id": f"ag-{i}", "grounding": grounding, "duration_ms": 900 + i, "question": "SECRET QUESTION"}}


class StatsSummary(unittest.TestCase):
    def summary(self, http, agent=()):
        return stats_service.compute_summary({"http_completed": list(http), "agent_run": list(agent)}, 24, NOW)

    def test_contract_shape_and_no_forbidden_fields(self):
        http = [_http("/ask", i, response=_ask_response(i, "supported" if i % 4 else "insufficient")) for i in range(40)]
        agent = [_agent(i, "tool_found_nothing" if i < 3 else "tool_sources") for i in range(12)]
        s = self.summary(http, agent)
        self.assertEqual(set(s), {"generated_at", "window_hours", "min_count", "totals", "latency_ms_by_path", "error_rate",
                                  "grounded_answer_hit_rate", "cost_usd", "agent_found_nothing_rate", "suppressed"})
        self.assertEqual(set(s["totals"]), {"requests", "ask_calls", "agent_runs", "ingests", "errors"})
        self.assertEqual(s["totals"], {"requests": 40, "ask_calls": 40, "agent_runs": 12, "ingests": 0, "errors": 0})
        self.assertEqual(s["grounded_answer_hit_rate"], 0.75)
        self.assertEqual(s["agent_found_nothing_rate"], 0.25)
        self.assertEqual(s["cost_usd"], {"total": 0.04, "per_ask": 0.001})
        self.assertEqual(s["latency_ms_by_path"]["/ask"]["n"], 40)
        self.assertAlmostEqual(s["latency_ms_by_path"]["/ask"]["p50"], 119.5)
        self.assertEqual(s["latency_ms_by_path"]["/agent"]["n"], 12)
        blob = json.dumps(s)
        for forbidden in ("payload", "client_ip", "203.0.113.9", "req-", "ag-", "request_id", "SECRET", "Secret Title", "doc-a"):
            self.assertNotIn(forbidden, blob)

    def test_small_cells_suppressed(self):
        http = [_http("/ask", i, response=_ask_response(i)) for i in range(4)]
        agent = [_agent(i) for i in range(3)]
        s = self.summary(http, agent)
        self.assertIsNone(s["totals"]["requests"])
        self.assertIsNone(s["totals"]["agent_runs"])
        self.assertEqual(s["latency_ms_by_path"]["/ask"], {"n": None, "p50": None, "p95": None})
        for rate in ("error_rate", "grounded_answer_hit_rate", "agent_found_nothing_rate"):
            self.assertIsNone(s[rate])
            self.assertIn(rate, s["suppressed"])
        self.assertEqual(s["cost_usd"], {"total": None, "per_ask": None})
        self.assertIn("totals.requests", s["suppressed"])
        self.assertIn("latency_ms_by_path./ask", s["suppressed"])

    def test_complementary_suppression_blocks_back_calculation(self):
        http = [_http("/ask", i) for i in range(50)] + [_http("/ask/stream", i) for i in range(3)]
        http += [_http("/summarize", i) for i in range(20)]
        s = self.summary(http)
        cells = s["latency_ms_by_path"]
        self.assertIsNone(cells["/ask/stream"]["n"])  # primary: 3 < 10
        self.assertIsNone(cells["/ask"]["n"])  # complementary: ask_calls - /ask would reveal it
        self.assertEqual(s["totals"]["ask_calls"], 53)
        self.assertEqual(s["totals"]["requests"], 73)
        self.assertEqual(cells["/summarize"]["n"], 20)
        # Generic check: no published total minus published cells isolates one hidden cell.
        groups = {"requests": ["/ask", "/ask/stream", "/summarize"], "ask_calls": ["/ask", "/ask/stream"]}
        for total, members in groups.items():
            hidden = [m for m in members if cells[m]["n"] is None]
            self.assertNotEqual(len(hidden), 1, total)

    def test_hidden_error_count_hides_error_rate(self):
        http = [_http("/ask", i) for i in range(30)] + [_http("/ask", 100 + i, status=500, error="X") for i in range(2)]
        s = self.summary(http)
        self.assertIsNone(s["totals"]["errors"])
        self.assertIsNone(s["error_rate"])
        self.assertEqual(s["totals"]["requests"], 32)

    def test_events_outside_window_and_unknown_paths_ignored(self):
        http = [_http("/ask", i, minutes_ago=60 * 25) for i in range(20)] + [_http("/secret/path", i) for i in range(20)]
        s = self.summary(http)
        self.assertEqual(s["totals"]["requests"], 0)
        self.assertNotIn("/secret/path", json.dumps(s))


class StatsEndpoint(_Base):
    def test_public_cached_and_validated(self):
        calls = []

        def fake_query(kind=None, limit=200):
            calls.append(kind)
            return [_http("/ask", i) for i in range(12)] if kind == "http_completed" else []

        p = mock.patch.object(stats_service, "query_events", fake_query)
        p.start()
        self.addCleanup(p.stop)
        self.use_key(None)  # public: works with the debug key unset
        r1 = self.client.get("/stats/summary")
        r2 = self.client.get("/stats/summary?window_hours=24")
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.json(), r2.json())
        self.assertEqual(len(calls), 2, "second call within TTL must hit the cache")
        self.assertEqual(r1.json()["min_count"], 10)
        self.assertEqual(self.client.get("/stats/summary?window_hours=0").status_code, 400)
        self.assertEqual(self.client.get("/stats/summary?window_hours=10000").status_code, 400)

    def test_rate_limited(self):
        p = mock.patch.object(stats_service, "query_events", lambda kind=None, limit=200: [])
        p.start()
        self.addCleanup(p.stop)
        codes = [self.client.get("/stats/summary").status_code for _ in range(31)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertEqual(codes[30], 429)


class UnchangedPublicRoutes(_Base):
    def test_health_and_providers_status_need_no_key(self):
        self.use_key(DIGEST)
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(set(r.json()), {"status", "skills_used"})
        with mock.patch.object(main.providers, "get_provider_status", lambda: []):
            self.assertEqual(self.client.get("/providers/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
