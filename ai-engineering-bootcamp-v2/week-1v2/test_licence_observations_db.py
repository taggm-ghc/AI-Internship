"""Item #97 H-23 (course side): DB mirror of the observation outbox, loader, migrations. Fakes only; no DB connection."""
import json
import re
import uuid
from pathlib import Path

import pytest

from intake import pipeline, verify
from scripts import load_licence_observations as loader
from test_intake_pipeline import CC, Resp, PUB
from test_intake_verify import benign, lines

HERE = Path(__file__).parent
R = HERE.parent.parent
MIG = HERE / "migrations" / "pending" / "007_licence_observations.sql"
ROLLBACK = HERE / "migrations" / "rollback" / "007_licence_observations_rollback.sql"


class Conn:
    def __init__(self, eng): self.eng = eng
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def exec_driver_sql(self, sql): self.eng.log.append(("raw", sql, None))
    def execute(self, stmt, params=None):
        s = str(stmt)
        if self.eng.fail_on and self.eng.fail_on in s:
            raise RuntimeError("boom")
        self.eng.log.append(("exec", s, params))
        if s.lstrip().upper().startswith("SELECT"):
            return [(i,) for i in self.eng.have if i in params["ids"]]
        return None


class FakeEngine:
    def __init__(self, fail_on=None, have=()):
        self.log, self.fail_on, self.have = [], fail_on, set(have)
    def begin(self): return Conn(self)
    def inserts(self): return [e for e in self.log if e[0] == "exec" and e[1].lstrip().upper().startswith("INSERT")]


@pytest.fixture
def cfg(tmp_path):
    c = pipeline.load_config()
    c["policy"]["quarantine_dir"] = str(tmp_path / "q")
    return c


def vet(cfg, body, **kw):
    return pipeline.vet("https://example.org/a", opener=lambda q: Resp(body), resolver=lambda h, p: [PUB], cfg=cfg, **kw)


def test_shipped_config_flag_on_u03():
    """U03 (R1 2026-10-10): the shipped config enables the DB path."""
    v = verify.load_config()
    assert v["licence_observations_db_enabled"] is True and verify.db_enabled(v) is True


def test_shipped_flag_path_reaches_stub_only(cfg, monkeypatch):
    """With the shipped config (no override) a vet reaches the DB inserter exactly once; stubbed, so no production write."""
    seen = []
    monkeypatch.setattr(verify, "insert_observation_db", lambda rec, vcfg, engine=None: seen.append(rec))
    assert vet(cfg, benign(CC))["verdict"] == "allow"
    assert len(seen) == 1


def test_flag_off_makes_no_db_call(cfg, monkeypatch):
    cfg["verify"] = {**verify.load_config(), "licence_observations_db_enabled": False}
    monkeypatch.setattr(verify, "insert_observation_db", lambda *a, **k: pytest.fail("DB touched with flag off"))
    r = vet(cfg, benign(CC))
    assert r["verdict"] == "allow" and len(lines(cfg, "licence_observations.jsonl")) == 1


def test_flag_on_inserts_once_after_jsonl(cfg, monkeypatch):
    seen = []
    cfg["verify"] = {**verify.load_config(), "licence_observations_db_enabled": True}
    monkeypatch.setattr(verify, "insert_observation_db",
                        lambda rec, vcfg, engine=None: seen.append((rec, len(lines(cfg, "licence_observations.jsonl")))))
    assert vet(cfg, benign(CC))["verdict"] == "allow"
    assert len(seen) == 1 and seen[0][1] == 1 and seen[0][0]["decision"] == "allow"


def test_db_failure_keeps_verdict_and_jsonl(cfg, monkeypatch, caplog):
    cfg["verify"] = {**verify.load_config(), "licence_observations_db_enabled": True}
    monkeypatch.setattr(verify, "insert_observation_db", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    with caplog.at_level("WARNING"):
        r = vet(cfg, benign(CC))
    assert r["verdict"] == "allow"
    assert len(lines(cfg, "licence_observations.jsonl")) == 1
    assert any("outbox kept" in m for m in caplog.messages)


def test_insert_uses_timeout_and_conflict_clause():
    eng = FakeEngine()
    rec = verify.build_observation(purpose="ingest", url="https://example.org/a", usha="a" * 64, verdict="allow",
                                   reasons=[], lrep={"spdx": "CC-BY-4.0", "family": "CC-BY"}, vcfg=verify.load_config(),
                                   cfg_version="v1", reason_code="licence_ok")
    assert verify.emit_observation_db(rec, {**verify.load_config(), "licence_observations_db_enabled": True}, eng) is True
    assert eng.log[0][0] == "raw" and "statement_timeout = 2000" in eng.log[0][1]
    assert "ON CONFLICT (obs_id) DO NOTHING" in eng.log[1][1] and eng.log[1][2]["obs_id"] == rec["obs_id"]
    assert eng.log[1][2]["spdx"] == "CC-BY-4.0"


def _write_outbox(path, n, ts0=0, dup=False):
    recs = []
    for i in range(n):
        recs.append({"ts": f"2026-10-09T10:00:{ts0 + i:02d}+00:00", "source_system": "course-gateway", "portal": "example.org",
                     "host": "example.org", "url_sha256": f"{i:064x}", "url": None, "licence": None, "decision": "hold",
                     "reason_code": "licence_unknown_hold", "config_version": "v"})
    if dup:
        recs.append(recs[0])
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return recs


def run_loader(tmp_path, args, eng=None, enabled=True):
    out = []
    code = loader.main(["--outbox", str(tmp_path / "o.jsonl")] + args, engine=eng,
                       vcfg={**verify.load_config(), "licence_observations_db_enabled": enabled},
                       cfg=pipeline.load_config(), out=out.append)
    return code, out


def test_loader_dry_run_default_touches_nothing(tmp_path):
    _write_outbox(tmp_path / "o.jsonl", 3, dup=True)
    code, out = run_loader(tmp_path, [], eng=object())  # object() would explode on any DB use
    assert code == 0 and "pending=3" in out[0] and "duplicate_in_outbox=1" in out[0] and "no DB connection" in out[-1]
    assert not (tmp_path / "o.jsonl.hwm").exists()


def test_loader_refuses_write_when_flag_disabled(tmp_path):
    _write_outbox(tmp_path / "o.jsonl", 1)
    eng = FakeEngine()
    code, out = run_loader(tmp_path, ["--write"], eng=eng, enabled=False)
    assert code == 2 and "refused" in out[0] and eng.log == []


def test_loader_write_dedupes_skips_existing_and_sets_hwm(tmp_path):
    recs = _write_outbox(tmp_path / "o.jsonl", 4, dup=True)
    have = {verify.observation_obs_id(recs[0])}
    eng = FakeEngine(have=have)
    code, out = run_loader(tmp_path, ["--write", "--batch-size", "2"], eng=eng)
    assert code == 0 and "inserted=3 already_in_db=1" in out[-1]
    inserted = [p for e in eng.inserts() for p in e[2]]
    assert len({p["obs_id"] for p in inserted}) == len(inserted) == 3
    assert json.loads((tmp_path / "o.jsonl.hwm").read_text())["ts"] == recs[3]["ts"]
    code, out = run_loader(tmp_path, ["--write"], eng=FakeEngine())  # second run: only the last-second row is re-checked
    assert "below_high_water=3" in out[0]


def test_loader_fails_verbosely_and_keeps_hwm_before_failed_batch(tmp_path):
    _write_outbox(tmp_path / "o.jsonl", 4)
    eng = FakeEngine(fail_on="INSERT")
    code, out = run_loader(tmp_path, ["--write"], eng=eng)
    assert code == 2 and "FAILED" in out[-1] and "not advanced" in out[-1]
    assert not (tmp_path / "o.jsonl.hwm").exists()


def test_loader_bounded_by_max_rows(tmp_path):
    _write_outbox(tmp_path / "o.jsonl", 5)
    code, out = run_loader(tmp_path, ["--write", "--max-rows", "2", "--batch-size", "1"], eng=FakeEngine())
    assert code == 3 and "STOPPED" in out[-1] and "inserted=2" in out[-1]


def test_legacy_line_gets_deterministic_obs_id():
    rec = {"ts": "t", "source_system": "s", "url_sha256": "u"}
    assert verify.observation_obs_id(rec) == verify.observation_obs_id(dict(rec))
    uuid.UUID(verify.observation_obs_id(rec))


def test_migration_files_append_only_and_rollback_not_auto_applied():
    sql = MIG.read_text()
    assert "licence_observations_append_only" in sql and "BEFORE UPDATE OR DELETE" in sql and "BEFORE TRUNCATE" in sql
    grants = re.findall(r"GRANT\s+(.*?)\s+ON", sql, re.S)
    assert sorted(grants) == ["INSERT", "SELECT"]
    assert "internship_rw" in sql and "internship_ro" in sql
    assert "DESTRUCTIVE" in ROLLBACK.read_text()
    assert not list((HERE / "migrations").glob("[0-9][0-9][0-9]_*rollback*"))  # install_schema globs this dir
    assert (R / "p3m3" / "licence_report.sql").read_text().count("SELECT") >= 2


def test_pending_migrations_not_matched_by_install_schema_glob():
    import inspect, operational_store
    assert "glob('[0-9][0-9][0-9]_*.sql')" in inspect.getsource(operational_store.install_schema)  # the glob this test mirrors
    applied = {p.name for p in (HERE / "migrations").glob("[0-9][0-9][0-9]_*.sql")}
    pending = [p for p in (HERE / "migrations" / "pending").rglob("*.sql")]
    assert pending and applied.isdisjoint(p.name for p in pending)
    assert "007_licence_observations.sql" not in applied
