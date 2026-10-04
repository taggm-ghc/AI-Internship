"""Table-driven verdict. Precedence comes from config verdict_order. Fails closed:
missing/unmapped licence or an empty report gives the configured hold verdict."""
from .safe import token

VERDICTS = ("allow", "label", "hold", "reject")


def _cfg(cfg: dict) -> dict:
    return cfg.get("decide", cfg)


def _licence_status(licence):
    if licence is None:
        return None, ""
    if isinstance(licence, str):
        return licence.lower(), ""
    return (licence.get("status") or "").lower() or None, licence.get("spdx") or ""


def verdict(report: dict, licence, cfg: dict) -> dict:
    """licence: None (not checked -> hold), a status string, or {status, spdx}."""
    c = _cfg(cfg)
    order = c["verdict_order"]
    bad = [v for v in list(c["flag_verdicts"].values()) + list(c["licence_status_verdicts"].values()) if v not in VERDICTS]
    bad += [c.get(k) for k in ("licence_unmapped_verdict", "empty_report_verdict") if c.get(k) not in VERDICTS]
    if bad or set(order) != set(VERDICTS):
        raise ValueError(f"intake decide config has invalid verdict values: {bad or order}")
    hits: list[tuple[str, str]] = []
    if not isinstance(report, dict) or not isinstance(report.get("flags"), list):
        hits.append((c["empty_report_verdict"], f"empty or malformed report -> {c['empty_report_verdict']}"))
        report = {}
    for f in report.get("flags", []):
        name = f["name"] if isinstance(f, dict) else f
        v = c["flag_verdicts"].get(name)
        if v is None:
            raise ValueError(f"flag {name!r} has no entry in decide.flag_verdicts; add it to config")
        hits.append((v, f"flag {name} -> {v}"))
    scores = report.get("scores", {})
    for r in c["score_rules"]:
        if scores.get(r["score"], 0) >= r["gte"]:
            hits.append((r["verdict"], f"score {r['score']}={scores[r['score']]} >= {r['gte']} -> {r['verdict']}"))
    status, spdx = _licence_status(licence)
    if status in c["licence_status_verdicts"]:
        v = c["licence_status_verdicts"][status]
        hits.append((v, f"licence {status} -> {v}"))
    else:
        v = c["licence_unmapped_verdict"]
        hits.append((v, f"licence status {token(status)} not mapped (spdx {token(spdx)}) -> {v}"))
    final = "allow"
    for v in order:
        if any(h[0] == v for h in hits):
            final = v
            break
    return {"verdict": final, "reasons": [r for v, r in hits if v == final] or ["no flags"]}
