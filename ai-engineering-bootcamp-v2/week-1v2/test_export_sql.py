"""p3m3 item #99: portable SQL export. Fakes only, no live DB."""
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import export_sql as ex

SENTINEL_HOST = "sentinel-host-do-not-show.example.onrender.com"
SCHEMA = "demo"

CATALOG = {
    "schema": [(1, "owner_role", ['owner_role=UC/owner_role', 'demo_ro=U/owner_role'], "demo schema")],
    "extensions": [("vector", "public", True), ("pgcrypto", "public", False)],
    "types": [],
    "functions": [(10, "forbid_update", "CREATE OR REPLACE FUNCTION demo.forbid_update()\n RETURNS trigger\nAS $$begin raise exception 'no'; end$$ LANGUAGE plpgsql",
                   "owner_role", None, "", "f")],
    "rls": [],
    "sequences": [("parent_id_seq", "bigint", 1, 1, 1, 9223372036854775807, 1, False, "owner_role", None, "a", "parent.id"),
                  ("child_id_seq", "integer", 1, 1, 1, 2147483647, 1, False, "owner_role", None, "i", "child.id")],
    "tables": [(200, "child", "r", "owner_role", ['owner_role=arwdDxtm/owner_role', 'demo_ro=r/owner_role'], None, False),
               (100, "parent", "r", "owner_role", None, "parents", False)],
    ("columns", 100): [("id", "bigint", True, "", "", "nextval('demo.parent_id_seq'::regclass)", None, None),
                       ("name", "text", True, "", "", None, None, "display name"),
                       ("name_len", "integer", False, "", "s", "length(name)", None, None)],
    ("columns", 200): [("id", "integer", True, "a", "", None, None, None),
                       ("parent_id", "bigint", False, "", "", None, None, None)],
    ("constraints", 100): [("parent_pkey", "p", "PRIMARY KEY (id)", None, True)],
    ("constraints", 200): [("child_pkey", "p", "PRIMARY KEY (id)", None, True),
                           ("child_parent_fk", "f", "FOREIGN KEY (parent_id) REFERENCES demo.parent(id)", "demo.parent", True),
                           ("child_chk", "c", "CHECK ((parent_id > 0)) NOT VALID", None, False)],
    ("indexes", 100): [],
    ("indexes", 200): [("CREATE INDEX child_parent_idx ON demo.child USING btree (parent_id)",)],
    ("triggers", 100): [("parent_immutable", "CREATE TRIGGER parent_immutable BEFORE UPDATE ON demo.parent FOR EACH ROW EXECUTE FUNCTION demo.forbid_update()")],
    ("triggers", 200): [],
    "views": [("v_child", "v", " SELECT id FROM demo.child", "owner_role", None, None)],
}


def fake_fetch(key, params):
    if "oid" in params:
        return CATALOG[(key, params["oid"])]
    return CATALOG[key]


def test_ddl_split_pre_post_grants():
    d = ex.build_ddl(fake_fetch, SCHEMA)
    pre, post, grants = "\n".join(d["pre"]), "\n".join(d["post"]), "\n".join(d["grants"])
    assert 'CREATE EXTENSION IF NOT EXISTS "vector" WITH SCHEMA "public";' in pre
    assert 'CREATE SCHEMA IF NOT EXISTS "demo";' in pre
    assert '-- installed on the source database, not used by this schema\'s columns: CREATE EXTENSION IF NOT EXISTS "pgcrypto"' in pre
    assert "forbid_update" in pre and pre.index("forbid_update") < pre.index("CREATE TABLE")
    assert 'CREATE SEQUENCE IF NOT EXISTS "demo"."parent_id_seq" AS bigint' in pre
    assert "child_id_seq" not in pre  # identity sequence comes from the column
    assert '"id" integer GENERATED ALWAYS AS IDENTITY (INCREMENT BY 1' in pre
    assert '"name_len" integer GENERATED ALWAYS AS (length(name)) STORED' in pre
    assert 'ALTER SEQUENCE "demo"."parent_id_seq" OWNED BY "demo"."parent"."id";' in pre
    assert "PRIMARY KEY (id)" in pre and "FOREIGN KEY" not in pre
    assert "COMMENT ON COLUMN" in pre and 'CREATE VIEW "demo"."v_child"' in pre
    # post-data: FK, secondary index, trigger (after data so immutability triggers do not block load)
    assert "FOREIGN KEY" in post and "child_parent_idx" in post and "CREATE TRIGGER parent_immutable" in post
    assert "TRIGGER" not in pre.replace("RETURNS trigger", "")
    assert 'GRANT SELECT ON TABLE "demo"."child" TO "demo_ro";' in grants
    assert 'GRANT USAGE ON SCHEMA "demo" TO "demo_ro";' in grants
    assert 'ALTER SEQUENCE "demo"."parent_id_seq" OWNER' not in grants  # owned-by sequences follow the table
    assert "TO \"owner_role\";" not in grants.replace("OWNER TO \"owner_role\";", "")


def test_generated_columns_not_copied_and_fk_order():
    d = ex.build_ddl(fake_fetch, SCHEMA)
    parent = next(t for t in d["tables"] if t["name"] == "parent")
    assert parent["copy_columns"] == ["id", "name"]
    assert d["order"] == ["parent", "child"]
    assert d["warnings"] == []


def test_not_valid_check_goes_after_data():
    d = ex.build_ddl(fake_fetch, SCHEMA)
    assert "child_chk" not in "\n".join(d["pre"])
    assert any("child_chk" in x and "NOT VALID" in x for x in d["post"])
    assert any("child_chk" in n for n in d["notes"])


def test_fk_cycle_reported():
    order, cyc = ex.fk_order(["a", "b", "c"], {"a": {"b"}, "b": {"a"}, "c": set()})
    assert order[0] == "c" and set(cyc) == {"a", "b"}


def test_cycle_is_a_note_not_a_warning():
    cat = dict(CATALOG)
    cat[("constraints", 100)] = CATALOG[("constraints", 100)] + [("p_child_fk", "f", "FOREIGN KEY (id) REFERENCES demo.child(id)", "demo.child", True)]
    d = ex.build_ddl(lambda k, p: cat[(k, p["oid"])] if "oid" in p else cat[k], SCHEMA)
    assert d["warnings"] == [] and any("cycle" in n for n in d["notes"])


def test_setval_from_owning_column():
    sql = ex.setval_sql(SCHEMA, {"name": "child_id_seq", "identity": True, "owned_by": "child.id"})
    assert sql.startswith("SELECT setval(pg_get_serial_sequence('\"demo\".\"child\"', 'id')")
    assert "max(\"id\") IS NOT NULL" in sql
    assert ex.setval_sql(SCHEMA, {"name": "free", "owned_by": None}) is None


def test_acl_grant_option_and_public():
    g = ex.acl_grants(["=X/own", "r=r*w/own"], "FUNCTION", "f()", "own")
    assert "GRANT EXECUTE ON FUNCTION f() TO PUBLIC;" in g
    assert 'GRANT UPDATE ON FUNCTION f() TO "r";' in g
    assert 'GRANT SELECT ON FUNCTION f() TO "r" WITH GRANT OPTION;' in g


def test_missing_schema_blocks():
    with pytest.raises(ex.ExportBlocked):
        ex.build_ddl(lambda k, p: [] if k == "schema" else fake_fetch(k, p), SCHEMA)


def test_conninfo_blocked_without_ro_login_names_env_not_values(monkeypatch):
    monkeypatch.delenv("DB_BACKUP_RO_USER", raising=False)
    with pytest.raises(ex.ExportBlocked, match="DB_BACKUP_RO_USER is unset"):
        ex.conninfo({"account_env": "DB_BACKUP_RO_USER", "password_env_prefix": "P_", "host_env": "H", "dbname_env": "D"})
    monkeypatch.delenv("X_URL", raising=False)
    with pytest.raises(ex.ExportBlocked, match="X_URL is unset"):
        ex.conninfo({"url_env": "X_URL"})


def test_redact_hides_host():
    e = Exception(f'connection to server at "{SENTINEL_HOST}" (1.2.3.4), port 5432 failed: timeout')
    assert SENTINEL_HOST not in ex.redact(e)
    assert SENTINEL_HOST not in ex.redact(Exception(f"could not translate host name {SENTINEL_HOST}"))


class FakeCursor:
    def __init__(self, write_privs=0, tx=("on", "repeatable read")):
        self.write_privs, self._last, self.tx, self.executed = write_privs, None, tx, []

    def execute(self, sql, params=None):
        self._sql, self._params = sql, params
        self.executed.append(sql)
        if sql == ex.Q["tx_state"]:
            self._last = [self.tx]
            return
        if sql == ex.Q["user_and_version"]:
            self._last = [("demo_ro", "18.6", "180006")]
        elif sql == ex.Q["write_privs"]:
            self._last = [(self.write_privs,)]
        elif sql.startswith("SELECT count(*)"):
            self._last = [(2,) if '"parent"' in sql else (1,)]
        else:
            for k, q in ex.Q.items():
                if q == sql:
                    self._last = fake_fetch(k, params or {})
                    break
            else:
                self._last = []

    def fetchone(self):
        return self._last[0]

    def fetchall(self):
        return self._last

    def copy_expert(self, sql, f):
        if '"parent"' in sql:
            f.write('id,name\n1,"multi\nline"\n2,b\n')
        else:
            f.write("id,parent_id\n1,1\n")


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def rollback(self):
        pass


def test_export_writes_files_modes_manifest(tmp_path):
    out = tmp_path / "demo"
    m = ex.export_schema(FakeConn(FakeCursor()), SCHEMA, out, 0o700, 0o600, 1000)
    for fn in ("schema.sql", "post_data.sql", "grants.sql", "restore_data.psql", "README.md", "manifest.json",
               "data/parent.csv", "data/child.csv"):
        assert (out / fn).is_file(), fn
        assert stat.S_IMODE(os.stat(out / fn).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o700
    assert m["tables"]["parent"] == {**m["tables"]["parent"], "rows": 2, "csv_records": 2}  # multi-line value counted once
    assert m["load_order"] == ["parent", "child"]
    assert m["restore_validated_against_empty_postgres"] is False
    disk = json.loads((out / "manifest.json").read_text())
    assert disk["tables"]["child"]["sha256"] == ex.sha256(out / "data/child.csv")
    psql = (out / "restore_data.psql").read_text()
    assert psql.index('"parent"') < psql.index('"child"') and '("id", "name")' in psql
    assert "setval" in (out / "post_data.sql").read_text()
    blob = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert SENTINEL_HOST not in blob


def test_export_refuses_account_with_write_privileges(tmp_path):
    with pytest.raises(ex.ExportBlocked, match="write privileges"):
        ex.export_schema(FakeConn(FakeCursor(write_privs=3)), SCHEMA, tmp_path / "x", 0o700, 0o600, 1000)
    assert not (tmp_path / "x").exists()


def test_verify_counts_flags_mismatch():
    cur = FakeCursor()
    manifest = {"tables": {"parent": {"rows": 2, "csv_records": 2}, "child": {"rows": 5, "csv_records": 5}}}
    bad = ex.verify_counts(FakeConn(cur), SCHEMA, manifest)
    assert len(bad) == 1 and bad[0].startswith("demo.child")


def test_config_has_no_hosts_or_secrets():
    cfg_text = (Path(__file__).resolve().parent / "config" / "sql_export.json").read_text()
    assert "onrender" not in cfg_text and "postgresql://" not in cfg_text
    cfg = json.loads(cfg_text)
    assert set(cfg["schemas"]) == {"vera_vjay", "internship"}


def test_post_data_indexes_before_constraints_before_triggers():
    post = ex.build_ddl(fake_fetch, SCHEMA)["post"]
    i_idx = next(i for i, x in enumerate(post) if "child_parent_idx" in x)
    i_fk = next(i for i, x in enumerate(post) if "FOREIGN KEY" in x)
    i_trg = next(i for i, x in enumerate(post) if "CREATE TRIGGER" in x)
    assert i_idx < i_fk < i_trg


def test_unsafe_table_name_refused():
    cat = dict(CATALOG)
    cat["tables"] = [(100, "../evil\n\\! rm", "r", "o", None, None, False)]
    with pytest.raises(ex.ExportBlocked, match="unsafe"):
        ex.build_ddl(lambda k, p: cat[(k, p["oid"])] if "oid" in p else cat[k], SCHEMA)


def test_rls_and_unowned_sequence_warn():
    cat = dict(CATALOG)
    cat["rls"] = [("parent",)]
    cat["sequences"] = CATALOG["sequences"] + [("free_seq", "bigint", 1, 1, 1, 9, 1, False, "o", None, "", None)]
    d = ex.build_ddl(lambda k, p: cat[(k, p["oid"])] if "oid" in p else cat[k], SCHEMA)
    assert any("row-level" in w for w in d["warnings"]) and any("free_seq" in w for w in d["warnings"])


def test_redact_hides_configured_values_and_ips():
    e = Exception("invalid dsn: postgresql://u:s3cretpw@db.internal.example:5432/x at 10.0.0.7")
    out = ex.redact(e, ("postgresql://u:s3cretpw@db.internal.example:5432/x",))
    assert "s3cretpw" not in out and "10.0.0.7" not in out


def test_write_login_allowed_only_with_flag_and_runs_read_only(tmp_path):
    cur = FakeCursor(write_privs=3)
    m = ex.export_schema(FakeConn(cur), SCHEMA, tmp_path / "w", 0o700, 0o600, 1000, allow_write_login=True)
    assert m["tables"]
    assert cur.executed[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert cur.executed.index(ex.Q["tx_state"]) < cur.executed.index(ex.Q["write_privs"])
    # nothing but SELECT / COPY TO / SET TRANSACTION / set_config is ever executed
    for q in cur.executed:
        assert q.lstrip().split()[0].upper() in ("SET", "SELECT", "WITH"), q


@pytest.mark.parametrize("tx", [("off", "repeatable read"), ("on", "read committed")])
def test_session_not_read_only_snapshot_is_refused(tmp_path, tx):
    with pytest.raises(ex.ExportBlocked, match="READ ONLY REPEATABLE READ"):
        ex.export_schema(FakeConn(FakeCursor(write_privs=3, tx=tx)), SCHEMA, tmp_path / "n", 0o700, 0o600, 1000,
                         allow_write_login=True)
    assert not (tmp_path / "n").exists()


def test_internship_config_uses_course_service_login_with_flag():
    cfg = json.loads((Path(__file__).resolve().parent / "config" / "sql_export.json").read_text())
    s = cfg["schemas"]["internship"]
    assert s["account_env"] == "DB_ACCOUNT" and s["password_env"] == "DB_PASSWORD" and s["allow_write_login"] is True
    assert "allow_write_login" not in cfg["schemas"]["vera_vjay"]


def test_conninfo_password_env(monkeypatch):
    for k, v in {"A": "svc", "PW": "pw", "H": "h", "S": ".x", "D": "db"}.items():
        monkeypatch.setenv(k, v)
    ci = ex.conninfo({"account_env": "A", "password_env": "PW", "host_env": "H", "host_suffix_env": "S", "dbname_env": "D"})
    assert ci["user"] == "svc" and ci["password"] == "pw" and ci["host"] == "h.x"
