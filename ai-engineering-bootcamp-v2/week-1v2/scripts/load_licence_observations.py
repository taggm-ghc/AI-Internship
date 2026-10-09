"""Drain the licence-observation JSONL outbox into internship.licence_observations (item #97, H-23).

Dry-run by default: reads the outbox files only and prints counts; it never touches the DB.
--write inserts the missing rows through the rw app-account engine in bounded batches. It refuses unless the
config flag licence_observations_db_enabled is true. De-duplicates by obs_id (legacy lines get a derived uuid5 over
url_sha256 + source_system + ts), skips lines older than the high-water mark, and fails verbosely (exit 2) on the
first non-duplicate error without advancing the mark past the failed batch.
High-water mark: <outbox>.hwm (JSON {"ts": ...}); lines with ts < mark are skipped, equal ts are re-checked and the DB
de-duplicates them, so a crash never loses a row.
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from intake import fetch, pipeline, verify  # noqa: E402

DEFAULT_MAX_ROWS = 5000
DEFAULT_MAX_SECONDS = 60
DEFAULT_BATCH = 200
HARD_MAX_ROWS = 100000


class LoaderError(Exception):
    pass


def default_outbox(cfg, vcfg):
    return Path(fetch.quarantine_dir(cfg["policy"])) / vcfg["audit_subdir"] / vcfg["observation_file"]


def outbox_files(outbox, keep):
    """Oldest first: rotated files .keep .. .1, then the live file."""
    p = Path(outbox)
    files = [Path(f"{p}.{i}") for i in range(keep, 0, -1)] + [p]
    return [f for f in files if f.is_file()]


def read_hwm(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("ts") or ""
    except (OSError, ValueError):
        return ""


def write_hwm(path, ts, loaded):
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps({"ts": ts, "rows_loaded_last_run": loaded}), encoding="utf-8")
    tmp.replace(path)


def scan(files, hwm):
    """Returns (pending rows [(obs_id, ts, params)], stats). In-file duplicates and malformed lines are counted."""
    stats = {"lines": 0, "malformed": 0, "duplicate_in_outbox": 0, "below_high_water": 0}
    seen, pending = set(), []
    for f in files:
        for raw in f.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            stats["lines"] += 1
            try:
                rec = json.loads(raw)
                row = verify.observation_row(rec)
            except (ValueError, KeyError, TypeError, AttributeError):
                stats["malformed"] += 1
                continue
            if row["obs_id"] in seen:
                stats["duplicate_in_outbox"] += 1
                continue
            seen.add(row["obs_id"])
            if hwm and row["observed_at"] < hwm:
                stats["below_high_water"] += 1
                continue
            pending.append(row)
    pending.sort(key=lambda r: r["observed_at"])
    stats["pending"] = len(pending)
    return pending, stats


def existing_ids(conn, ids):
    from sqlalchemy import text
    rows = conn.execute(text("SELECT obs_id::text FROM internship.licence_observations WHERE obs_id::text = ANY(:ids)"),
                        {"ids": list(ids)})
    return {r[0] for r in rows}


def load(pending, engine, *, batch, max_rows, max_seconds, hwm_path, clock=time.monotonic):
    from sqlalchemy import text
    deadline = clock() + max_seconds
    res = {"inserted": 0, "already_in_db": 0, "stopped": ""}
    todo = pending[:max_rows]
    if len(pending) > max_rows:
        res["stopped"] = f"max_rows={max_rows} reached; {len(pending) - max_rows} rows remain"
    for i in range(0, len(todo), batch):
        if clock() >= deadline:
            res["stopped"] = f"max_seconds={max_seconds} reached; {len(todo) - i} rows remain in this run"
            break
        chunk = todo[i:i + batch]
        try:
            with engine.begin() as conn:
                have = existing_ids(conn, [r["obs_id"] for r in chunk])
                new = [r for r in chunk if r["obs_id"] not in have]
                if new:
                    conn.execute(text(verify.OBS_INSERT_SQL), new)
        except Exception as e:
            raise LoaderError(f"batch starting at pending row {i} failed ({type(e).__name__}); "
                              f"high-water mark not advanced; {res['inserted']} rows inserted earlier in this run") from e
        res["inserted"] += len(new)
        res["already_in_db"] += len(have)
        write_hwm(hwm_path, chunk[-1]["observed_at"], res["inserted"])
    return res


def main(argv=None, *, engine=None, vcfg=None, cfg=None, out=print):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="insert into the DB (default is a dry-run: counts only)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run (the default)")
    ap.add_argument("--outbox", help="JSONL outbox path (default: <quarantine>/audit/<observation_file>)")
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    ap.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    a = ap.parse_args(argv)
    if a.write and a.dry_run:
        out("refused: --write and --dry-run are mutually exclusive")
        return 2
    if not (0 < a.max_rows <= HARD_MAX_ROWS and a.max_seconds > 0 and a.batch_size > 0):
        out(f"refused: need 0 < max-rows <= {HARD_MAX_ROWS}, max-seconds > 0, batch-size > 0")
        return 2
    try:
        vcfg = vcfg or verify.load_config()
        cfg = cfg or pipeline.load_config()
        outbox = Path(a.outbox) if a.outbox else default_outbox(cfg, vcfg)
    except Exception as e:
        out(f"FAILED: cannot resolve configuration/outbox: {type(e).__name__}: {e}")
        return 2
    if a.write and not verify.db_enabled(vcfg):
        out("refused: licence_observations_db_enabled is false in config/intake_verify_callers.json "
            "(R1 enables it after the DDL is applied)")
        return 2
    hwm_path = Path(f"{outbox}.hwm")
    files = outbox_files(outbox, int(vcfg.get("observation_keep", 3)))
    pending, stats = scan(files, read_hwm(hwm_path))
    out(f"outbox files={len(files)} lines={stats['lines']} malformed={stats['malformed']} "
        f"duplicate_in_outbox={stats['duplicate_in_outbox']} below_high_water={stats['below_high_water']} "
        f"pending={stats['pending']}")
    if not a.write:
        out("dry-run: no DB connection made; use --write to insert")
        return 0
    if not pending:
        out("nothing to load")
        return 0
    if engine is None:
        from db import get_engine
        engine = get_engine()
    try:
        res = load(pending, engine, batch=a.batch_size, max_rows=a.max_rows, max_seconds=a.max_seconds, hwm_path=hwm_path)
    except LoaderError as e:
        out(f"FAILED: {e}")
        return 2
    out(f"inserted={res['inserted']} already_in_db={res['already_in_db']}" + (f" STOPPED: {res['stopped']}" if res["stopped"] else ""))
    return 3 if res["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
