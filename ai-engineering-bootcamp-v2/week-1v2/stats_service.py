"""Public, aggregate-only operational statistics (p3m3 item #62, W1/D3).

GET /stats/summary (wired in main.py) answers what the observability
dashboard used to compute client-side from up to 500 raw events pulled off
the now-keyed GET /debug/events. Everything here is computed SERVER-SIDE and
only counts, rates, percentiles and sums leave this module: never payloads,
IP addresses, request ids, questions, answers, titles or document text.

Disclosure control (plan section 5, source S2):
- any count below MIN_COUNT (10) is hidden (null) and listed in `suppressed`;
- complementary suppression: when a published total is the sum of a group of
  cells, hiding exactly one cell (or cells summing to < MIN_COUNT) would let
  it be back-calculated from the total, so the next-smallest visible cell is
  hidden too, and if none is left the total itself is hidden; repeated to a
  fixed point across every group;
- a rate or percentile resting on fewer than MIN_COUNT events is null.
MIN_COUNT is a module constant, never a caller parameter.

Resource control (F10): one result per window_hours is cached for
CACHE_TTL_SECONDS, and main.py rate limits the route, so a public caller
cannot force repeated table scans.

Known limit: operational_store.query_events has no time filter, so this reads
the newest FETCH_LIMIT rows per event kind and filters to the window in
Python; on a busy window older events beyond that cap are not counted.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict

from operational_store import query_events

MIN_COUNT = 10
CACHE_TTL_SECONDS = 60.0
FETCH_LIMIT = 500
MAX_WINDOW_HOURS = 24 * 7

# Fixed allowlist (the paths operational_audit.py records, plus /agent from
# agent_run events). A path outside it is never echoed back, so a public
# response cannot carry an arbitrary caller-chosen string.
ASK_PATHS = ("/ask", "/ask/stream")
INGEST_PATHS = ("/ingest", "/ingest/batch", "/ingest-pdf")
HTTP_PATHS = ASK_PATHS + INGEST_PATHS + ("/summarize", "/analyze-sentiment", "/debug/retrieve")
AGENT_PATH = "/agent"


class LatencyCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int | None
    p50: float | None
    p95: float | None


class Totals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: int | None
    ask_calls: int | None
    agent_runs: int | None
    ingests: int | None
    errors: int | None


class CostUsd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: float | None
    per_ask: float | None


class StatsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: str
    window_hours: int
    min_count: int
    totals: Totals
    latency_ms_by_path: dict[str, LatencyCell]
    error_rate: float | None
    grounded_answer_hit_rate: float | None
    cost_usd: CostUsd
    agent_found_nothing_rate: float | None
    suppressed: list[str]


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (pandas' default), so the
    numbers match what the dashboard showed when it computed them itself."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def suppress_counts(values: dict[str, int], groups: list[tuple[str, list[str]]], min_count: int) -> set[str]:
    """Primary plus complementary suppression over named counts.

    values: name -> count. groups: (total_name, [cell names]) where the total
    equals (or bounds) the sum of the cells. Returns the set of hidden names.
    A zero is not hidden (nothing to disclose); a count of 1..min_count-1 is.
    Groups may nest (a subtotal is a cell of a larger group and the total of
    its own). Two rules, applied until nothing changes:
    A. total published: hidden cells must number 0, or at least 2 with a
       combined count >= min_count; otherwise hide the smallest visible cell
       (or, with none left, the total).
    B. total hidden: at least one of its cells must be hidden too, or the
       total is just the sum of published cells."""
    hidden = {name for name, v in values.items() if 0 < v < min_count}
    changed = True
    while changed:
        changed = False
        for total, cells in groups:
            cells = [c for c in cells if values.get(c, 0) > 0]
            hid = [c for c in cells if c in hidden]
            visible = sorted((values[c], c) for c in cells if c not in hidden)
            if total in hidden:
                if cells and not hid:
                    hidden.add(visible[0][1])
                    changed = True
            elif hid and (len(hid) == 1 or sum(values[c] for c in hid) < min_count):
                hidden.add(visible[0][1] if visible else total)
                changed = True
    return hidden


def compute_summary(rows_by_kind: dict[str, list[dict]], window_hours: int, now: datetime,
                    min_count: int = MIN_COUNT) -> dict:
    """Pure function: raw event rows in, aggregate-only dict out. Reads only
    the payload fields it needs; nothing from a row is copied verbatim."""
    since = now - timedelta(hours=window_hours)

    def in_window(rows):
        for row in rows or []:
            ts = _parse_ts(row.get("created_at"))
            payload = row.get("payload")
            if ts is not None and ts >= since and isinstance(payload, dict):
                yield payload

    latencies: dict[str, list[float]] = {}
    errors = 0
    ask_with_status = 0
    ask_supported = 0
    ask_costs: list[float] = []
    for p in in_window(rows_by_kind.get("http_completed")):
        path = p.get("path")
        if path not in HTTP_PATHS:
            continue
        latencies.setdefault(path, [])
        if _is_number(p.get("latency_ms")):
            latencies[path].append(float(p["latency_ms"]))
        else:
            latencies[path].append(float("nan"))
        status = p.get("http_status") or 200
        if p.get("error") or (_is_number(status) and status >= 400):
            errors += 1
        response = p.get("response")
        if path == "/ask" and isinstance(response, dict):
            if response.get("status"):
                ask_with_status += 1
                ask_supported += response.get("status") == "supported"
            if _is_number(response.get("cost_usd")):
                ask_costs.append(float(response["cost_usd"]))

    agent_runs = 0
    agent_found_nothing = 0
    agent_durations: list[float] = []
    for p in in_window(rows_by_kind.get("agent_run")):
        agent_runs += 1
        agent_found_nothing += p.get("grounding") == "tool_found_nothing"
        if _is_number(p.get("duration_ms")):
            agent_durations.append(float(p["duration_ms"]))
    if agent_durations:
        latencies[AGENT_PATH] = agent_durations

    path_n = {path: len(vals) for path, vals in latencies.items()}
    requests = sum(n for path, n in path_n.items() if path in HTTP_PATHS)
    counts = {
        "totals.requests": requests,
        "totals.ask_calls": sum(path_n.get(p, 0) for p in ASK_PATHS),
        "totals.agent_runs": agent_runs,
        "totals.ingests": sum(path_n.get(p, 0) for p in INGEST_PATHS),
        "totals.errors": errors,
        **{f"latency_ms_by_path.{path}": n for path, n in path_n.items()},
    }
    cell = lambda paths: [f"latency_ms_by_path.{p}" for p in paths if p in path_n]  # noqa: E731
    other_paths = [p for p in HTTP_PATHS if p not in ASK_PATHS + INGEST_PATHS]
    groups = [
        ("totals.requests", ["totals.ask_calls", "totals.ingests"] + cell(other_paths)),
        ("totals.ask_calls", cell(ASK_PATHS)),
        ("totals.ingests", cell(INGEST_PATHS)),
        ("totals.agent_runs", cell([AGENT_PATH])),
    ]
    hidden = suppress_counts(counts, groups, min_count)
    shown = lambda name: None if name in hidden else counts[name]  # noqa: E731

    suppressed = set(hidden)
    latency_out = {}
    for path in sorted(path_n):
        name = f"latency_ms_by_path.{path}"
        vals = sorted(v for v in latencies[path] if v == v)  # drop NaN placeholders
        if name in hidden or len(vals) < min_count:
            # n may still be publishable when only the latency values are thin.
            latency_out[path] = {"n": shown(name), "p50": None, "p95": None}
            if name not in hidden:
                suppressed.update({f"{name}.p50", f"{name}.p95"})
        else:
            latency_out[path] = {"n": path_n[path], "p50": round(_percentile(vals, 0.5), 1),
                                 "p95": round(_percentile(vals, 0.95), 1)}

    def rate(name, numerator, denominator, extra_hidden=()):
        if denominator < min_count or any(h in hidden for h in extra_hidden):
            suppressed.add(name)
            return None
        return round(numerator / denominator, 4)

    error_rate = rate("error_rate", errors, requests, ("totals.errors", "totals.requests"))
    hit_rate = rate("grounded_answer_hit_rate", ask_supported, ask_with_status)
    found_nothing_rate = rate("agent_found_nothing_rate", agent_found_nothing, agent_runs, ("totals.agent_runs",))
    if len(ask_costs) < min_count:
        cost = {"total": None, "per_ask": None}
        suppressed.update({"cost_usd.total", "cost_usd.per_ask"})
    else:
        cost = {"total": round(sum(ask_costs), 6), "per_ask": round(sum(ask_costs) / len(ask_costs), 6)}

    return StatsSummary(
        generated_at=now.isoformat(),
        window_hours=window_hours,
        min_count=min_count,
        totals={key.split(".", 1)[1]: shown(key) for key in counts if key.startswith("totals.")},
        latency_ms_by_path=latency_out,
        error_rate=error_rate,
        grounded_answer_hit_rate=hit_rate,
        cost_usd=cost,
        agent_found_nothing_rate=found_nothing_rate,
        suppressed=sorted(suppressed),
    ).model_dump()


_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_summary(window_hours: int = 24) -> dict:
    """Cached wrapper: at most one set of event reads per window_hours per
    CACHE_TTL_SECONDS, however many public callers ask (F10)."""
    if not 1 <= window_hours <= MAX_WINDOW_HOURS:
        raise ValueError(f"window_hours must be between 1 and {MAX_WINDOW_HOURS}")
    with _cache_lock:
        hit = _cache.get(window_hours)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        rows_by_kind = {kind: query_events(kind=kind, limit=FETCH_LIMIT) for kind in ("http_completed", "agent_run")}
        summary = compute_summary(rows_by_kind, window_hours, datetime.now(timezone.utc))
        _cache[window_hours] = (time.monotonic() + CACHE_TTL_SECONDS, summary)
        return summary
