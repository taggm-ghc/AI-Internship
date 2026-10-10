"""Portable SQL export of the project schemas (p3m3 item #99; R1 2026-10-09 "SQL exports ... for later rebuild and port").

Why not pg_dump: the server is PostgreSQL 18, the local client is 16 (pg_dump refuses a newer server) and R1
declined installing a pg18 client. This script rebuilds the same pre-data / data / post-data split from pg_catalog
and the server's own pg_get_*def() functions, over a READ ONLY, REPEATABLE READ transaction (one snapshot).

Read-only accounts only: the connection must be a read-only login (checked: the account may hold no INSERT, UPDATE,
DELETE or TRUNCATE privilege on any table in the schema). Connection details come from env variables named in
config/sql_export.json; host, URL and password are never printed or written.

Output per schema, under <export_root>/<YYYY-MM-DD>/<schema>/ (outside git; dirs 0700, files 0600):
  schema.sql     extensions, schema, functions, sequences, tables (columns, defaults, identity, NOT NULL,
                 PK/UNIQUE/CHECK/EXCLUDE), views, comments
  data/<t>.csv   COPY <t> TO STDOUT (CSV HEADER); generated columns excluded, as COPY does
  post_data.sql  non-constraint indexes, foreign keys, triggers, sequence/identity setval
  grants.sql     ownership and GRANTs (roles must exist on the target; apply last, optional)
  restore_data.psql  \\copy lines in foreign-key order
  manifest.json  server version, export time, per-table row counts (snapshot count(*) and CSV record count), sha256
  README.md      restore order

Usage (repo root):
  ai-engineering-bootcamp-v2/week-1v2/.venv/bin/python ai-engineering-bootcamp-v2/week-1v2/scripts/export_sql.py \\
      [--schema vera_vjay] [--config path] [--out-root path] [--verify]
Exit codes: 0 all requested schemas exported; 2 config/connection/permission blocker (reason printed, no secrets);
3 row-count mismatch; 4 exported with warnings (known-incomplete: see manifest warnings).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # week-1v2/
DEFAULT_CONFIG = HERE / "config" / "sql_export.json"
# Invariant: privileges whose presence means the login is not read-only for our purposes.
WRITE_PRIVS = "INSERT,UPDATE,DELETE,TRUNCATE"
# Invariant: csv.field_size_limit must exceed the largest exported text value (chunk text, vectors).
CSV_FIELD_LIMIT = sys.maxsize // 2 if sys.maxsize < 2**63 else 2**31 - 1


import re
# Table names become data/<name>.csv and a psql \\copy line: allow only plain identifiers.
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


class ExportBlocked(Exception):
    """A blocker the operator (R1) must resolve; message never contains a host or secret."""


def qi(name: str) -> str:
    """Quote an identifier the way quote_ident does (always quoted; safe for any name)."""
    return '"' + name.replace('"', '""') + '"'


def ql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------- catalog queries
Q = {
    "user_and_version": "select current_user, current_setting('server_version'), current_setting('server_version_num')",
    "tx_state": "select current_setting('transaction_read_only'), current_setting('transaction_isolation')",
    "write_privs": """select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname=%(s)s and c.relkind in ('r','p','v','m','f') and has_table_privilege(c.oid, %(p)s)""",
    "schema": """select n.oid, pg_get_userbyid(n.nspowner), n.nspacl::text[], obj_description(n.oid,'pg_namespace')
        from pg_namespace n where n.nspname=%(s)s""",
    # all installed extensions (not only type-linked ones: defaults, opclasses and function bodies need them too)
    # linked = some column of this schema uses a type from the extension; unlinked ones are listed commented-out
    # (a schema-only port need not install them; if a default/opclass/function needs one, the restore fails loudly)
    "extensions": """select e.extname, en.nspname, exists (select 1 from pg_depend d join pg_attribute a on a.atttypid=d.objid
              join pg_class c on c.oid=a.attrelid join pg_namespace n on n.oid=c.relnamespace
              where d.classid='pg_type'::regclass and d.refclassid='pg_extension'::regclass and d.refobjid=e.oid
                and n.nspname=%(s)s and a.attnum>0)
        from pg_extension e join pg_namespace en on en.oid=e.extnamespace where e.extname<>'plpgsql' order by 1""",
    "rls": """select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname=%(s)s and (c.relrowsecurity or exists (select 1 from pg_policy p where p.polrelid=c.oid))""",
    "types": """select t.typname, t.typtype from pg_type t join pg_namespace n on n.oid=t.typnamespace
        where n.nspname=%(s)s and t.typtype in ('e','d','c','r')
          and not exists (select 1 from pg_class c where c.reltype=t.oid and c.relkind<>'c')
          and not exists (select 1 from pg_depend d where d.objid=t.oid and d.deptype='e')""",
    "functions": """select p.oid, p.proname, pg_get_functiondef(p.oid), pg_get_userbyid(p.proowner), p.proacl::text[],
            pg_get_function_identity_arguments(p.oid), p.prokind
        from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname=%(s)s and p.prokind in ('f','p')
          and not exists (select 1 from pg_depend d where d.objid=p.oid and d.deptype='e')
        order by p.oid""",
    "sequences": """select c.relname, format_type(s.seqtypid,null), s.seqstart, s.seqincrement, s.seqmin, s.seqmax,
            s.seqcache, s.seqcycle, pg_get_userbyid(c.relowner), c.relacl::text[],
            (select coalesce(d.deptype::text,'') from pg_depend d where d.objid=c.oid and d.classid='pg_class'::regclass
               and d.refclassid='pg_class'::regclass and d.deptype in ('a','i') limit 1) as dep,
            (select rc.relname||'.'||a.attname from pg_depend d join pg_class rc on rc.oid=d.refobjid
               join pg_attribute a on a.attrelid=d.refobjid and a.attnum=d.refobjsubid
               where d.objid=c.oid and d.classid='pg_class'::regclass and d.refclassid='pg_class'::regclass
                 and d.deptype in ('a','i') limit 1) as owned_by
        from pg_class c join pg_namespace n on n.oid=c.relnamespace join pg_sequence s on s.seqrelid=c.oid
        where n.nspname=%(s)s and c.relkind='S' order by c.relname""",
    "tables": """select c.oid, c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relacl::text[],
            obj_description(c.oid,'pg_class'), c.relispartition
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname=%(s)s and c.relkind in ('r','p') order by c.relname""",
    "columns": """select a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, a.attidentity, a.attgenerated,
            pg_get_expr(ad.adbin, ad.adrelid),
            case when a.attcollation<>0 and a.attcollation<>t.typcollation
                 then (select quote_ident(cn.nspname)||'.'||quote_ident(co.collname) from pg_collation co
                       join pg_namespace cn on cn.oid=co.collnamespace where co.oid=a.attcollation) end,
            col_description(a.attrelid,a.attnum)
        from pg_attribute a join pg_type t on t.oid=a.atttypid
        left join pg_attrdef ad on ad.adrelid=a.attrelid and ad.adnum=a.attnum
        where a.attrelid=%(oid)s and a.attnum>0 and not a.attisdropped order by a.attnum""",
    "constraints": """select con.conname, con.contype, pg_get_constraintdef(con.oid), con.confrelid::regclass::text,
            con.convalidated
        from pg_constraint con where con.conrelid=%(oid)s and con.contype in ('p','u','c','x','f')
        order by case con.contype when 'p' then 0 when 'u' then 1 when 'x' then 2 when 'c' then 3 else 4 end, con.conname""",
    "indexes": """select pg_get_indexdef(i.indexrelid) from pg_index i
        where i.indrelid=%(oid)s and not exists (select 1 from pg_constraint con where con.conindid=i.indexrelid
              and con.conrelid=i.indrelid and con.contype in ('p','u','x'))
        order by i.indexrelid""",
    "triggers": """select tg.tgname, pg_get_triggerdef(tg.oid) from pg_trigger tg
        where tg.tgrelid=%(oid)s and not tg.tgisinternal order by tg.tgname""",
    "views": """select c.relname, c.relkind, pg_get_viewdef(c.oid), pg_get_userbyid(c.relowner), c.relacl::text[],
            obj_description(c.oid,'pg_class')
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname=%(s)s and c.relkind in ('v','m') order by c.oid""",
    "seq_last_value": """select last_value from pg_sequences where schemaname=%(s)s and sequencename=%(n)s""",
}


def acl_grants(acl: list[str] | None, objtype: str, objname: str, owner: str) -> list[str]:
    """Turn an aclitem[] (as text) into GRANT statements. Owner's own implicit grants are skipped."""
    letters = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE", "D": "TRUNCATE", "x": "REFERENCES",
               "t": "TRIGGER", "X": "EXECUTE", "U": "USAGE", "C": "CREATE", "c": "CONNECT", "T": "TEMPORARY",
               "m": "MAINTAIN"}
    out = []
    for item in acl or []:
        grantee, _, rest = item.partition("=")
        privs = rest.split("/")[0]
        grantee = grantee.strip('"') or "PUBLIC"
        if grantee == owner:
            continue
        names = []
        i = 0
        while i < len(privs):
            ch = privs[i]
            opt = i + 1 < len(privs) and privs[i + 1] == "*"
            if ch in letters:
                names.append((letters[ch], opt))
            i += 2 if opt else 1
        who = "PUBLIC" if grantee == "PUBLIC" else qi(grantee)
        plain = [n for n, o in names if not o]
        withopt = [n for n, o in names if o]
        if plain:
            out.append(f"GRANT {', '.join(plain)} ON {objtype} {objname} TO {who};")
        if withopt:
            out.append(f"GRANT {', '.join(withopt)} ON {objtype} {objname} TO {who} WITH GRANT OPTION;")
    return out


def fk_order(tables: list[str], fks: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """Topological order (referenced tables first). Returns (order, tables_in_cycles)."""
    remaining = {t: {d for d in fks.get(t, set()) if d in tables and d != t} for t in tables}
    order: list[str] = []
    while remaining:
        ready = sorted(t for t, deps in remaining.items() if not deps)
        if not ready:
            cyc = sorted(remaining)
            return order + cyc, cyc
        for t in ready:
            order.append(t)
            del remaining[t]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order, []


# --------------------------------------------------------------------------- DDL generation
def build_ddl(fetch, schema: str) -> dict:
    """fetch(query_key, params) -> list of row tuples. Returns the pieces of the export (no I/O)."""
    s = {"s": schema}
    srow = fetch("schema", s)
    if not srow:
        raise ExportBlocked(f"schema {schema!r} not found or not visible to this account")
    _, sowner, sacl, scomment = srow[0]
    S = qi(schema)
    pre = ["-- generated by scripts/export_sql.py (p3m3 item #99); apply with psql -v ON_ERROR_STOP=1",
           "SET check_function_bodies = false;", "SET client_min_messages = warning;", ""]
    post_idx: list[str] = []
    post_con: list[str] = []
    post_trg: list[str] = []
    grants = [f"ALTER SCHEMA {S} OWNER TO {qi(sowner)};"] + acl_grants(sacl, "SCHEMA", S, sowner)
    warnings: list[str] = []
    notes: list[str] = []

    for ext, ens, linked in fetch("extensions", s):
        stmt = f"CREATE EXTENSION IF NOT EXISTS {qi(ext)} WITH SCHEMA {qi(ens)};"
        pre.append(stmt if linked else f"-- installed on the source database, not used by this schema's columns: {stmt}")
    pre.append(f"CREATE SCHEMA IF NOT EXISTS {S};")
    if scomment:
        pre.append(f"COMMENT ON SCHEMA {S} IS {ql(scomment)};")
    for (rname,) in fetch("rls", s):
        warnings.append(f"{schema}.{rname} uses row-level security/policies: policies are NOT exported")
    for tname, ttype in fetch("types", s):
        warnings.append(f"user-defined type {schema}.{tname} (typtype {ttype}) is NOT exported; add its DDL by hand")
    pre.append("")

    for _, pname, pdef, powner, pacl, pargs, pkind in fetch("functions", s):
        pre.append(pdef.rstrip() + ";\n")
        sig = f"{S}.{qi(pname)}({pargs})"
        kw = "PROCEDURE" if pkind == "p" else "FUNCTION"
        grants.append(f"ALTER {kw} {sig} OWNER TO {qi(powner)};")
        if pacl is not None:  # explicit ACL: start from nothing so a revoked PUBLIC EXECUTE stays revoked
            grants.append(f"REVOKE ALL ON {kw} {sig} FROM PUBLIC;")
        grants += acl_grants(pacl, kw, sig, powner)

    seqs = fetch("sequences", s)
    owned_by: list[str] = []
    seq_opts: dict[str, str] = {}
    seq_meta = []
    for (sname, stype, start, inc, smin, smax, cache, cycle, sowner_, sacl_, dep, owner_col) in seqs:
        seq_meta.append({"name": sname, "identity": dep == "i", "owned_by": owner_col})
        if not owner_col:
            warnings.append(f"sequence {schema}.{sname} has no OWNED BY column: no setval is emitted, set it by hand")
        if dep == "i":
            # no AS <type> here: the identity sequence takes the column's type (AS is rejected as redundant)
            seq_opts[owner_col] = (f"INCREMENT BY {inc} MINVALUE {smin} MAXVALUE {smax} START WITH {start} "
                                   f"CACHE {cache}{' CYCLE' if cycle else ''}")
            continue  # identity sequence: created by the column definition
        pre.append(f"CREATE SEQUENCE IF NOT EXISTS {S}.{qi(sname)} AS {stype} INCREMENT BY {inc} MINVALUE {smin} "
                   f"MAXVALUE {smax} START WITH {start} CACHE {cache}{' CYCLE' if cycle else ''};")
        if owner_col:
            t, c = owner_col.split(".", 1)
            owned_by.append(f"ALTER SEQUENCE {S}.{qi(sname)} OWNED BY {S}.{qi(t)}.{qi(c)};")
        if not owner_col:  # an OWNED BY sequence follows its table's owner; ALTER ... OWNER on it is an error
            grants.append(f"ALTER SEQUENCE {S}.{qi(sname)} OWNER TO {qi(sowner_)};")
        grants += acl_grants(sacl_, "SEQUENCE", f"{S}.{qi(sname)}", sowner_)
    pre.append("")

    tables = []
    fks: dict[str, set[str]] = {}
    for toid, tname, kind, towner, tacl, tcomment, ispart in fetch("tables", s):
        if not SAFE_NAME.match(tname):
            raise ExportBlocked(f"table name {tname!r} is unsafe as a file name / psql line; export refused")
        if kind == "p" or ispart:
            warnings.append(f"{schema}.{tname} is partitioned/partition; partitioning is NOT reproduced")
        T = f"{S}.{qi(tname)}"
        cols, comments, copy_cols, identity_cols = [], [], [], []
        for (cname, ctype, notnull, ident, gen, default, coll, ccomment) in fetch("columns", {"oid": toid}):
            parts = [qi(cname), ctype]
            if coll:
                parts.append(f"COLLATE {coll}")
            if gen == "s":
                parts.append(f"GENERATED ALWAYS AS ({default}) STORED")
            elif gen == "v":
                parts.append(f"GENERATED ALWAYS AS ({default}) VIRTUAL")
            elif ident:
                kind_ = "ALWAYS" if ident == "a" else "BY DEFAULT"
                opts = seq_opts.get(f"{tname}.{cname}", "")
                parts.append(f"GENERATED {kind_} AS IDENTITY{f' ({opts})' if opts else ''}")
                identity_cols.append(cname)
            elif default is not None:
                parts.append(f"DEFAULT {default}")
            if notnull:
                parts.append("NOT NULL")
            cols.append("    " + " ".join(parts))
            if not gen:
                copy_cols.append(cname)
            if ccomment:
                comments.append(f"COMMENT ON COLUMN {T}.{qi(cname)} IS {ql(ccomment)};")
        pre.append(f"CREATE TABLE {T} (\n" + ",\n".join(cols) + "\n);")
        fks[tname] = set()
        for cname_, ctype_, cdef, ref, validated in fetch("constraints", {"oid": toid}):
            stmt = f"ALTER TABLE ONLY {T} ADD CONSTRAINT {qi(cname_)} {cdef};"
            if not validated:
                # NOT VALID on the server (existing rows may violate it): add after the data, still NOT VALID
                # (pg_get_constraintdef keeps the clause), as pg_dump does. Found by the 2026-10-09 restore test.
                post_con.append(stmt)
                notes.append(f"{schema}.{tname}.{cname_} is NOT VALID on the server; restored NOT VALID after data")
            elif ctype_ == "f":
                post_con.append(stmt)
                if ref:
                    refname = ref.split(".")[-1].strip('"')
                    fks[tname].add(refname)
            else:
                pre.append(stmt)
        if tcomment:
            pre.append(f"COMMENT ON TABLE {T} IS {ql(tcomment)};")
        pre += comments
        for (idef,) in fetch("indexes", {"oid": toid}):
            post_idx.append(idef + ";")
        for _, tdef in fetch("triggers", {"oid": toid}):
            post_trg.append(tdef + ";")
        grants.append(f"ALTER TABLE {T} OWNER TO {qi(towner)};")
        grants += acl_grants(tacl, "TABLE", T, towner)
        tables.append({"name": tname, "copy_columns": copy_cols, "identity_columns": identity_cols})
        pre.append("")

    pre += owned_by
    for vname, vkind, vdef, vowner, vacl, vcomment in fetch("views", s):
        V = f"{S}.{qi(vname)}"
        kw = "MATERIALIZED VIEW" if vkind == "m" else "VIEW"
        pre.append(f"CREATE {kw} {V} AS\n{vdef.rstrip().rstrip(';')}{' WITH NO DATA' if vkind == 'm' else ''};")
        if vcomment:
            pre.append(f"COMMENT ON {kw} {V} IS {ql(vcomment)};")
        grants.append(f"ALTER {'MATERIALIZED VIEW' if vkind == 'm' else 'VIEW'} {V} OWNER TO {qi(vowner)};")
        grants += acl_grants(vacl, "TABLE", V, vowner)
        if vkind == "m":
            post_trg.append(f"REFRESH MATERIALIZED VIEW {V};")

    order, cyc = fk_order([t["name"] for t in tables], fks)
    # Foreign keys are added in post_data.sql, after the data, so a cycle does not block the load; it is
    # recorded so a loader that adds keys first (not this restore) knows to defer them.
    if cyc:
        notes.append(f"foreign-key cycle among {cyc} (harmless here: keys are added after the data)")
    post = post_idx + post_con + post_trg  # all indexes first, so FKs find their unique indexes
    return {"pre": pre, "post": post, "grants": grants, "tables": tables, "order": order,
            "sequences": seq_meta, "warnings": warnings, "notes": notes}


def setval_sql(schema: str, seq: dict) -> str | None:
    """Restore a sequence from the data itself (max of its owning column): works without SELECT on the sequence."""
    if not seq.get("owned_by"):
        return None
    t, c = seq["owned_by"].split(".", 1)
    S = qi(schema)
    return (f"SELECT setval(pg_get_serial_sequence({ql(f'{S}.{qi(t)}')}, {ql(c)}), "
            f"coalesce(max({qi(c)}), 1), max({qi(c)}) IS NOT NULL) FROM {S}.{qi(t)};")


# --------------------------------------------------------------------------- connection (no secrets printed)
def load_env_files(files: list[str], base: Path) -> None:
    """Minimal KEY=VALUE loader; existing environment wins. Values are never echoed."""
    for f in files:
        p = (base / f).resolve()
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


def conninfo(sconf: dict) -> dict:
    """Return psycopg2.connect kwargs for one schema's configured account. Raises ExportBlocked naming env vars only."""
    if "url_env" in sconf:
        url = os.getenv(sconf["url_env"])
        if not url:
            raise ExportBlocked(f"env {sconf['url_env']} is unset")
        return {"dsn": url}
    acct_env = sconf["account_env"]
    account = os.getenv(acct_env)
    if not account:
        raise ExportBlocked(f"env {acct_env} is unset: no read-only login is provisioned for this schema "
                            f"(owner: R1 / DB admin; creating one needs a named request)")
    pw_env = sconf["password_env"] if "password_env" in sconf else sconf["password_env_prefix"] + account
    missing = [v for v in (pw_env, sconf["host_env"], sconf["dbname_env"]) if not os.getenv(v)]
    if missing:
        raise ExportBlocked(f"env {missing} unset for account in {acct_env}")
    return {"user": account, "password": os.environ[pw_env], "dbname": os.environ[sconf["dbname_env"]],
            "host": os.environ[sconf["host_env"]] + os.getenv(sconf.get("host_suffix_env", ""), "")}


def redact(exc: BaseException, secrets: tuple = ()) -> str:
    """First line of a driver error, with the configured conninfo values, IPs and anything host-like removed."""
    msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    for val in sorted((str(v) for v in secrets if v), key=len, reverse=True):
        msg = msg.replace(val, "<redacted>")
    msg = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b|\b[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}\b", "<ip>", msg)
    msg = re.sub(r'(host|server at|to server)\s*"?[^\s",)]+"?', r"\1 <redacted>", msg, flags=re.I)
    return re.sub(r"[A-Za-z0-9.-]+\.(onrender\.com|render\.com|amazonaws\.com)", "<redacted>", msg)


# --------------------------------------------------------------------------- export
def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_records(p: Path) -> int:
    csv.field_size_limit(CSV_FIELD_LIMIT)
    with p.open(newline="", encoding="utf-8") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def write_text(p: Path, text: str, fmode: int) -> None:
    p.write_text(text, encoding="utf-8")
    os.chmod(p, fmode)


def readme(schema: str, ddl: dict) -> str:
    return f"""# Portable SQL export: schema `{schema}`

Generated by `week-1v2/scripts/export_sql.py` (p3m3 item #99). Contains production data, possibly including
licensed source text: keep it outside git and outside any public location.

Restore into an empty PostgreSQL (same major version or newer; extensions listed in schema.sql must be installable),
from this directory, as a role that will own the objects:

1. `psql -v ON_ERROR_STOP=1 -d <target> -f schema.sql`  (tables without foreign keys, triggers or secondary indexes)
2. `psql -v ON_ERROR_STOP=1 -d <target> -f restore_data.psql`  (\\copy per table, foreign-key order; COPY writes
   identity-ALWAYS values as-is)
3. `psql -v ON_ERROR_STOP=1 -d <target> -f post_data.sql`  (indexes, foreign keys, triggers, then sequence setval
   from max(id); triggers are created after the data so append-only/immutability triggers do not block the load)
4. Optional, last: `psql -d <target> -f grants.sql`  (ownership and GRANTs; the named roles must exist on the target;
   without -v ON_ERROR_STOP so a missing role is reported and skipped)
5. Check: row counts in manifest.json (`tables.<t>.rows`) equal `SELECT count(*)` on the target.

Warnings recorded at export time: {json.dumps(ddl['warnings'])}
"""


def export_schema(conn, schema: str, out_dir: Path, dmode: int, fmode: int, timeout_ms: int,
                  allow_write_login: bool = False) -> dict:
    """allow_write_login (R1 decision 8, 2026-10-10, internship): the configured login may hold write privileges
    (the existing course-service login); the only control is then the READ ONLY, REPEATABLE READ transaction, whose
    state is verified from the server before any export query runs."""
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    cur.execute(Q["tx_state"])
    ro, iso = cur.fetchone()
    if ro != "on" or iso != "repeatable read":
        raise ExportBlocked(f"session is not a READ ONLY REPEATABLE READ transaction (read_only={ro}, isolation={iso}): refusing")
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),))
    # pin name qualification of pg_get_*def() output, as pg_dump does; and fail loudly instead of
    # silently exporting only the rows a row-level-security policy shows this account
    cur.execute("SELECT set_config('search_path', 'pg_catalog', true), set_config('row_security', 'off', true)")
    cur.execute(Q["user_and_version"])
    user, version, version_num = cur.fetchone()
    cur.execute(Q["write_privs"], {"s": schema, "p": WRITE_PRIVS})
    if cur.fetchone()[0] and not allow_write_login:
        raise ExportBlocked(f"account {user!r} holds write privileges in {schema!r}: refusing (read-only accounts only)")

    def fetch(key, params):
        cur.execute(Q[key], params)
        return cur.fetchall()

    ddl = build_ddl(fetch, schema)
    for d in (out_dir, out_dir / "data"):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, dmode)
    tables, copy_lines = {}, []
    for name in ddl["order"]:
        t = next(x for x in ddl["tables"] if x["name"] == name)
        T = f"{qi(schema)}.{qi(name)}"
        cols = ", ".join(qi(c) for c in t["copy_columns"])
        cur.execute(f"SELECT count(*) FROM {T}")
        snap = cur.fetchone()[0]
        p = out_dir / "data" / f"{name}.csv"
        with p.open("w", encoding="utf-8", newline="") as f:
            cur.copy_expert(f"COPY {T} ({cols}) TO STDOUT WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')", f)
        os.chmod(p, fmode)
        recs = csv_records(p)
        if recs != snap:
            raise ExportBlocked(f"{schema}.{name}: snapshot count(*)={snap} but CSV has {recs} records")
        tables[name] = {"rows": snap, "csv_records": recs, "file": f"data/{name}.csv", "sha256": sha256(p)}
        copy_lines.append(f"\\copy {T} ({cols}) FROM 'data/{name}.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')")
    setvals = [x for x in (setval_sql(schema, sq) for sq in ddl["sequences"]) if x]
    conn.rollback()  # end the read-only snapshot; nothing was written
    files = {
        "schema.sql": "\n".join(ddl["pre"]) + "\n",
        "post_data.sql": "\n".join(ddl["post"] + ["", "-- sequences: restart after the loaded data"] + setvals) + "\n",
        "grants.sql": "\n".join(ddl["grants"]) + "\n",
        "restore_data.psql": "\\set ON_ERROR_STOP on\n" + "\n".join(copy_lines) + "\n",
        "README.md": readme(schema, ddl),
    }
    for fn, text in files.items():
        write_text(out_dir / fn, text, fmode)
    manifest = {
        "schema": schema, "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "server_version": version, "server_version_num": version_num, "account": user,
        "tool": "week-1v2/scripts/export_sql.py", "snapshot": "REPEATABLE READ, READ ONLY",
        "tables": tables, "load_order": ddl["order"], "warnings": ddl["warnings"], "notes": ddl["notes"],
        "files": {fn: sha256(out_dir / fn) for fn in files},
        "restore_validated_against_empty_postgres": False,
    }
    write_text(out_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n", fmode)
    return manifest


def verify_counts(conn, schema: str, manifest: dict) -> list[str]:
    """Fresh-transaction count(*) per table vs the manifest; returns mismatch lines (empty = equal)."""
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ ONLY")
    bad = []
    for name, t in manifest["tables"].items():
        cur.execute(f"SELECT count(*) FROM {qi(schema)}.{qi(name)}")
        live = cur.fetchone()[0]
        if not (live == t["rows"] == t["csv_records"]):
            bad.append(f"{schema}.{name}: manifest rows={t['rows']} csv={t['csv_records']} live count(*)={live}")
    conn.rollback()
    return bad


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--schema", action="append", help="limit to these schemas (default: all configured)")
    ap.add_argument("--out-root", help="override export_root")
    ap.add_argument("--verify", action="store_true", help="re-count rows after export and compare with the manifest")
    args = ap.parse_args(argv)
    os.umask(0o077)  # files are private from creation, not only after the chmod
    cfg = json.loads(Path(args.config).read_text())
    load_env_files(cfg.get("env_files", []), HERE)
    dmode, fmode = int(cfg["dir_mode"], 8), int(cfg["file_mode"], 8)
    root = Path(os.path.expanduser(args.out_root or cfg["export_root"]))
    date_dir = root / dt.date.today().isoformat()
    import psycopg2

    rc = 0
    for schema in args.schema or list(cfg["schemas"]):
        sconf = cfg["schemas"].get(schema)
        if sconf is None:
            print(f"[{schema}] BLOCKED: not in config", file=sys.stderr)
            rc = max(rc, 2)
            continue
        ci = {}
        try:
            ci = conninfo(sconf)
            conn = psycopg2.connect(connect_timeout=cfg["connect_timeout_s"], **ci)
        except ExportBlocked as e:
            print(f"[{schema}] BLOCKED: {e}", file=sys.stderr)
            rc = max(rc, 2)
            continue
        except Exception as e:  # driver errors can carry the host: redact
            print(f"[{schema}] BLOCKED: connect failed: {redact(e, tuple(ci.values()))}", file=sys.stderr)
            rc = max(rc, 2)
            continue
        try:
            for d in (root, date_dir):
                d.mkdir(parents=True, exist_ok=True)
                os.chmod(d, dmode)
            m = export_schema(conn, schema, date_dir / schema, dmode, fmode, cfg["statement_timeout_ms"],
                              allow_write_login=bool(sconf.get("allow_write_login")))
            total = sum(t["rows"] for t in m["tables"].values())
            print(f"[{schema}] exported {len(m['tables'])} tables, {total} rows -> {date_dir / schema}")
            for w in m["warnings"]:
                print(f"[{schema}] WARNING: {w}")
            if m["warnings"]:
                rc = max(rc, 4)  # export written but known-incomplete
            if args.verify:
                bad = verify_counts(conn, schema, m)
                for b in bad:
                    print(f"[{schema}] MISMATCH: {b}", file=sys.stderr)
                print(f"[{schema}] verify: {'OK' if not bad else 'MISMATCH'} ({len(m['tables'])} tables)")
                rc = max(rc, 3 if bad else 0)
        except ExportBlocked as e:
            print(f"[{schema}] BLOCKED: {e}", file=sys.stderr)
            rc = max(rc, 2)
        except Exception as e:
            print(f"[{schema}] FAILED: {type(e).__name__}: {redact(e, tuple(ci.values()))}", file=sys.stderr)
            rc = max(rc, 2)
        finally:
            conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
