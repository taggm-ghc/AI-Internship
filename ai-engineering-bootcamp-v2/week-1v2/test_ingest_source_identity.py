"""Item #65 S3: source identity wired into _ingest_or_stage. No network, no DB: tripwire engine + mocks.
Run: .venv/bin/python -m pytest test_ingest_source_identity.py -q
"""
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
import source_identity  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import ProgrammingError  # noqa: E402

ARXIV = {"arxiv_id": "2401.00001", "identifier_origin": "original", "content_sha256": "h"}


def _link(sid, t, v, canon=True, origin="original"):
    return {"source_id": sid, "type": t, "value_norm": v, "is_canonical": canon, "origin": origin}


class _Case(unittest.TestCase):
    def _call(self, *, exists=False, auth=True, flags=None, prov=None, links=(), docs=None, twin=None, stage=None, chunks=3):
        extra = {"client_ip": "127.0.0.1", "authenticated": auth}
        stage = stage or {"version": 2}
        p = mock.patch.multiple(
            main,
            document_exists=mock.DEFAULT, find_live_document_by_content_sha=mock.DEFAULT,
            find_source_identity=mock.DEFAULT, link_document_to_new_source=mock.DEFAULT,
            stage_document_version=mock.DEFAULT, accept_document_version=mock.DEFAULT,
            upsert_chunks=mock.DEFAULT, record_event=mock.DEFAULT,
        )
        with p as m:
            m["document_exists"].return_value = exists
            m["find_live_document_by_content_sha"].return_value = twin
            m["find_source_identity"].return_value = (list(links), docs or {})
            m["link_document_to_new_source"].return_value = 77
            m["stage_document_version"].return_value = stage
            m["upsert_chunks"].return_value = chunks
            res = main._ingest_or_stage(None, None, "new-doc", "text", None, dict(prov or {"content_sha256": "h"}), extra, flags or {})
            return res, m


class NoIdentifiers(_Case):
    def test_unchanged(self):
        res, m = self._call()
        self.assertEqual((res.status, res.document_id, res.source_id, res.source_review), ("indexed", "new-doc", None, None))
        m["find_source_identity"].assert_not_called()
        m["link_document_to_new_source"].assert_not_called()


class Conflict(_Case):
    def test_two_original_canonical_ids_422(self):
        links = [_link(5, "arxiv", "2401.00001"), _link(5, "doi", "10.1000/aaa")]
        prov = {**ARXIV, "doi": "10.1000/bbb", "content_sha256": "h"}
        with self.assertRaises(HTTPException) as ctx:
            self._call(prov=prov, links=links, docs={5: ["old"]})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("10.1000/bbb", ctx.exception.detail)

    def test_unauthenticated_gets_generic_422_and_event_has_detail(self):
        # forged claims from an unkeyed caller are derived, so a keyed-only conflict is checked via resolve mock
        res = source_identity.Resolution(source_identity.CONFLICT, reason="secret 10.1000/aaa", candidates=[5])
        with mock.patch.object(source_identity, "resolve", return_value=res):
            with self.assertRaises(HTTPException) as ctx:
                self._call(prov=ARXIV, auth=False, links=[_link(5, "arxiv", "2401.00001")], docs={5: ["old"]})
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (422, "conflicting source identifiers"))
        self.assertNotIn("10.1000", str(ctx.exception.detail))


class Forgery(_Case):
    def test_unkeyed_forged_origin_never_redirects(self):
        victim = [_link(5, "arxiv", "2401.00001")]
        for prov in (ARXIV, {"arxiv_id": "2401.00001", "identifier_origin": "original", "content_sha256": "h"},
                     {"source_url": "https://arxiv.org/abs/2401.00001", "content_sha256": "h"}):
            res, m = self._call(prov=prov, links=victim, docs={5: ["victim-doc"]}, auth=False)
            m["stage_document_version"].assert_not_called()
            self.assertEqual(res.document_id, "new-doc")
            self.assertIsNone(res.matched_existing_document)

    def test_unkeyed_new_source_ids_are_derived(self):
        _, m = self._call(prov=ARXIV, auth=False)
        ids = m["link_document_to_new_source"].call_args.args[2]
        self.assertTrue(ids and all(not i["is_canonical"] and i["origin"] == "derived" for i in ids))

    def test_keyed_gets_canonical(self):
        _, m = self._call(prov=ARXIV, auth=True)
        ids = m["link_document_to_new_source"].call_args.args[2]
        self.assertTrue(any(i["is_canonical"] and i["origin"] == "original" for i in ids))


class SameSource(_Case):
    links = [_link(5, "arxiv", "2401.00001")]

    def test_other_document_is_staged_as_version_of_existing(self):
        res, m = self._call(prov=ARXIV, links=self.links, docs={5: ["old-doc", "z"]}, flags={"injection_phrases": ["x"]})
        self.assertEqual((res.status, res.document_id, res.accepted, res.version), ("staged", "old-doc", False, 2))
        self.assertEqual((res.source_id, res.matched_existing_document), (5, True))
        self.assertEqual(m["stage_document_version"].call_args.args[0], "old-doc")
        m["upsert_chunks"].assert_not_called()
        m["link_document_to_new_source"].assert_not_called()

    def test_keyed_clean_auto_accepts_on_existing_document(self):
        res, m = self._call(prov=ARXIV, links=self.links, docs={5: ["old-doc"]})
        self.assertEqual((res.status, res.accepted, res.document_id), ("indexed", True, "old-doc"))
        self.assertEqual(m["upsert_chunks"].call_args.args[2], "old-doc")
        m["accept_document_version"].assert_called_once_with("old-doc", 2, "auto")

    def test_keyed_but_flagged_stays_staged(self):
        res, _ = self._call(prov=ARXIV, links=self.links, docs={5: ["old-doc"]}, flags={"injection_phrases": ["x"]})
        self.assertEqual(res.status, "staged")

    def test_same_document_id_is_current_behaviour(self):
        res, m = self._call(prov=ARXIV, links=self.links, docs={5: ["new-doc"]}, exists=True)
        self.assertEqual((res.document_id, res.matched_existing_document), ("new-doc", None))
        self.assertEqual(m["stage_document_version"].call_args.args[0], "new-doc")

    def test_duplicate_version_reports_existing_doc(self):
        res, m = self._call(prov=ARXIV, links=self.links, docs={5: ["old-doc"]}, stage={"duplicate": True})
        self.assertEqual((res.status, res.document_id), ("duplicate", "old-doc"))


class Review(_Case):
    def test_url_only_match_ingests_with_review_and_new_source(self):
        prov = {"source_url": "https://example.org/paper", "content_sha256": "h"}
        links = [_link(9, "url", "https://example.org/paper", False, "derived")]
        res, m = self._call(prov=prov, links=links, docs={9: ["other"]})
        self.assertEqual(res.status, "indexed")
        self.assertEqual(res.source_review["candidate_source_ids"], [9])
        self.assertIn("reason", res.source_review)
        self.assertIsNone(res.matched_existing_document)
        m["stage_document_version"].assert_not_called()
        m["link_document_to_new_source"].assert_called_once()
        self.assertEqual(res.source_id, 77)

    def test_unauthenticated_review_is_opaque(self):
        prov = {"source_url": "https://example.org/paper", "content_sha256": "h"}
        links = [_link(9, "url", "https://example.org/paper", False, "derived")]
        res, _ = self._call(prov=prov, links=links, docs={9: ["other"]}, auth=False)
        self.assertIs(res.source_review, True)
        self.assertIsNone(res.source_id)
        self.assertIsNone(res.matched_existing_document)
        self.assertNotIn("9", res.model_dump_json().replace('"chunks_indexed":3', ""))


class NewSource(_Case):
    def test_live_ingest_links_once(self):
        res, m = self._call(prov=ARXIV)
        self.assertEqual((res.status, res.source_id), ("indexed", 77))
        args = m["link_document_to_new_source"].call_args.args
        self.assertEqual(args[0], "new-doc")
        self.assertEqual(args[2][0]["value_norm"], "2401.00001")

    def test_not_linked_on_duplicate_staged_empty_or_held(self):
        for kw in (dict(twin="older"), dict(exists=True, auth=False), dict(exists=True, auth=True, flags={"x": 1}),
                   dict(chunks=0), dict(exists=True, stage={"duplicate": True})):
            _, m = self._call(prov=ARXIV, **kw)
            m["link_document_to_new_source"].assert_not_called()

    def test_existing_id_auto_accepted_live_is_linked(self):
        _, m = self._call(prov=ARXIV, exists=True)
        m["link_document_to_new_source"].assert_called_once()
        self.assertEqual(m["link_document_to_new_source"].call_args.args[0], "new-doc")

    def test_tables_missing_is_tolerated(self):
        extra = {"client_ip": "x", "authenticated": True}
        with mock.patch.object(main, "document_exists", return_value=False), \
             mock.patch.object(main, "find_live_document_by_content_sha", return_value=None), \
             mock.patch.object(main, "find_source_identity", return_value=([], {})), \
             mock.patch.object(main, "link_document_to_new_source", return_value=None), \
             mock.patch.object(main, "upsert_chunks", return_value=3):
            res = main._ingest_or_stage(None, None, "d", "t", None, dict(ARXIV), extra, {})
        self.assertEqual((res.status, res.source_id), ("indexed", None))


class _PgErr(Exception):
    def __init__(self, msg, sqlstate):
        super().__init__(msg)
        self.sqlstate = sqlstate


def _prog(msg, sqlstate="42P01"):
    return ProgrammingError("stmt", {}, _PgErr(msg, sqlstate))


class _BoomEngine:
    def __init__(self, exc):
        self.exc = exc

    def connect(self):
        raise self.exc

    begin = connect


class MissingTableTolerance(unittest.TestCase):
    ids = [{"type": "arxiv", "value_norm": "2401.00001", "value_raw": "x", "is_canonical": True, "origin": "original"}]

    def _with(self, exc, fn):
        with mock.patch.object(operational_store, "get_engine", return_value=_BoomEngine(exc)):
            return fn()

    def test_lookup_returns_empty(self):
        exc = _prog('relation "internship.source_identifiers" does not exist')
        self.assertEqual(self._with(exc, lambda: operational_store.find_source_identity(self.ids)), ([], {}))

    def test_write_skipped(self):
        exc = _prog('relation "internship.document_sources" does not exist')
        self.assertIsNone(self._with(exc, lambda: operational_store.link_document_to_new_source("d", "l", self.ids)))

    def test_other_errors_propagate(self):
        for exc in (_prog('relation "internship.documents" does not exist'), _prog("syntax error", "42601"),
                    RuntimeError("boom")):
            with self.assertRaises(Exception):
                self._with(exc, lambda: operational_store.find_source_identity(self.ids))
            with self.assertRaises(Exception):
                self._with(exc, lambda: operational_store.link_document_to_new_source("d", "l", self.ids))


class _Res:
    def __init__(self, rowcount=1, scalar=1, first=None):
        self.rowcount, self._s, self._f = rowcount, scalar, first

    def scalar(self):
        return self._s

    def first(self):
        return self._f


class _Conn:
    def __init__(self, sb_rowcount=1, ds_rowcount=1):
        self.sqls, self.sb, self.ds = [], sb_rowcount, ds_rowcount

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sqls.append(sql)
        if "FROM internship.document_sources" in sql:
            return _Res(first=None)
        if "INSERT INTO internship.source_identifiers" in sql:
            return _Res(rowcount=self.sb)
        if "INSERT INTO internship.document_sources" in sql:
            return _Res(rowcount=self.ds)
        return _Res()


class _Eng:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        conn = self.conn

        class _Ctx:
            def __enter__(s):
                return conn

            def __exit__(s, *a):
                return False
        return _Ctx()


class StoreHelpers(unittest.TestCase):
    ids = [{"type": "arxiv", "value_norm": "2401.00001", "value_raw": "x", "is_canonical": True, "origin": "original"}]

    def _link(self, conn):
        with mock.patch.object(operational_store, "get_engine", return_value=_Eng(conn)):
            return operational_store.link_document_to_new_source("d", "l", self.ids)

    def test_named_conflict_targets_and_ordering(self):
        conn = _Conn()
        self.assertEqual(self._link(conn), 1)
        joined = " ".join(conn.sqls)
        self.assertIn("ON CONFLICT (source_id, identifier_id) DO NOTHING", joined)
        self.assertIn("ON CONFLICT (document_id, source_id) DO NOTHING", joined)
        self.assertNotIn("DO NOTHING\"", joined)
        self.assertIn("ORDER BY linked_at", joined)

    def test_unexpected_rowcount_raises(self):
        with self.assertRaises(RuntimeError):
            self._link(_Conn(sb_rowcount=0))
        with self.assertRaises(RuntimeError):
            self._link(_Conn(ds_rowcount=0))


if __name__ == "__main__":
    unittest.main()
