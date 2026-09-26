"""Unit tests for db.get_admin_engine (p3m3 item #47): try DB_ADMIN_ROLE,
retry DB_ADMIN_ROLE_RETRY only when that login is rejected; and db._database_url
built from DB_ACCOUNT/DB_PASSWORD/DB_HOST/DB_HOST_EXTERNAL_SUFFIX/DB_NAME. No network, no DB."""
import os
import unittest
from unittest import mock

from sqlalchemy.exc import OperationalError

import db

PARTS = {"DB_HOST": "dpg-x-a", "DB_HOST_EXTERNAL_SUFFIX": ".region.example.com", "DB_NAME": "dbname", "DB_ADMIN_PASSWORD": "pw"}


class FakeEngine:
    def __init__(self, url, error=None):
        self.url, self.error, self.disposed = url, error, False

    def connect(self):
        if self.error:
            raise OperationalError("connect", {}, Exception(self.error))
        return mock.MagicMock()

    def dispose(self):
        self.disposed = True


def run(errors_by_user, env):
    made = []

    def fake_create_engine(url, **kwargs):
        engine = FakeEngine(url, errors_by_user.get(url.username))
        made.append(engine)
        return engine

    db._admin_engine = None
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(db, "create_engine", fake_create_engine):
        try:
            return db.get_admin_engine(), made
        finally:
            db._admin_engine = None


class AdminFallback(unittest.TestCase):
    env = {**PARTS, "DB_ADMIN_ROLE": "old_admin", "DB_ADMIN_ROLE_RETRY": "new_admin"}

    def test_first_role_works(self):
        engine, made = run({}, self.env)
        self.assertEqual(engine.url.username, "old_admin")
        self.assertEqual(len(made), 1)

    def test_retry_after_rejected_login(self):
        engine, made = run({"old_admin": 'FATAL:  password authentication failed for user "old_admin"'}, self.env)
        self.assertEqual(engine.url.username, "new_admin")
        self.assertEqual(engine.url.password, "pw")  # same password, only the name changes
        self.assertTrue(made[0].disposed)

    def test_role_does_not_exist_also_retries(self):
        engine, _ = run({"old_admin": 'FATAL:  role "old_admin" does not exist'}, self.env)
        self.assertEqual(engine.url.username, "new_admin")

    def test_network_error_is_not_retried(self):
        with self.assertRaises(OperationalError):
            run({"old_admin": "could not translate host name"}, self.env)

    def test_both_rejected_raises(self):
        bad = 'FATAL:  password authentication failed'
        with self.assertRaises(OperationalError):
            run({"old_admin": bad, "new_admin": bad}, self.env)

    def test_only_first_role_configured(self):
        engine, made = run({}, {**PARTS, "DB_ADMIN_ROLE": "old_admin"})
        self.assertEqual((engine.url.username, len(made)), ("old_admin", 1))

    def test_missing_admin_settings_are_explicit(self):
        with self.assertRaises(RuntimeError):
            run({}, {})
        with self.assertRaises(RuntimeError):
            run({}, {"DB_ADMIN_ROLE": "old_admin", "DB_ADMIN_PASSWORD": "pw"})  # no host/db


class UrlFromParts(unittest.TestCase):
    def url(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return db._database_url()

    def test_external_uses_fqdn(self):
        u = self.url({**PARTS, "DB_ACCOUNT": "app_rw", "DB_PASSWORD": "p@ss/w:rd"})
        self.assertEqual(u.host, "dpg-x-a.region.example.com")
        self.assertEqual((u.username, u.password, u.database, u.port), ("app_rw", "p@ss/w:rd", "dbname", None))
        self.assertIn("p%40ss%2Fw%3Ard@dpg-x-a.region.example.com/dbname", u.render_as_string(hide_password=False))

    def test_internal_uses_bare_host(self):
        self.assertEqual(self.url({**PARTS, "RENDER": "true", "DB_ACCOUNT": "app_rw", "DB_PASSWORD": "x"}).host, "dpg-x-a")

    def test_falls_back_to_full_url_without_db_account(self):
        self.assertEqual(self.url({"EXTERNAL_DB_URL": "postgresql://a:b@h/d"}), "postgresql://a:b@h/d")
        self.assertEqual(self.url({"RENDER": "true", "INTERNAL_DB_URL": "postgresql://a:b@i/d"}), "postgresql://a:b@i/d")

    def test_nothing_set_is_explicit(self):
        with self.assertRaises(RuntimeError):
            self.url({})


if __name__ == "__main__":
    unittest.main()
