"""p3m3 item #42 -- calibrates the "rerank only when confidence is neither
high nor low" band for /ask from item #40/#41's saved per-question results,
instead of guessing thresholds. Design: p3m3/week2-priority-checklist.md,
section D-N+8.

Signals, computed from the same 15 dense distances /ask already has:
  d1  -- top-1 distance (high d1 = weak retrieval = LOW confidence)
  gap -- distance of the best chunk from a DIFFERENT document minus d1
         (large gap = one document clearly ahead = HIGH confidence).
         The top-score gap is the confidence signal arXiv:2609.15578 found
         strongest (Score Gap r=.467 vs MaxScore r=.328).
Policy under test: rerank iff d1 <= LOW_D1 and gap <= HIGH_GAP. (Shipped
policy, 2026-09-24: the HIGH_GAP skip only; calibration found no useful LOW_D1.)

Gated metrics are computed EXACTLY from the saved records: for a question
in the band use its "rerank" (dense -> pinned reranker, ordered) result,
otherwise its "dense" result. No LLM calls. Thresholds are chosen on
even-indexed questions and reported on the odd-indexed hold-out, to avoid
grading the band on the same data that picked it.

Read-only: one embedding + one query_store per question.
Run with: python scripts/rerank_gate_calibration.py [--records PATH]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from openai import OpenAI

from rag_service import OVERFETCH_K, confidence_signals, embed_query, get_collection, query_store

METRICS = ["doc_hit@5", "doc_rr@8", "chunk_rr@8"]
MIN_GAIN_RETAINED = 0.9  # keep >= 90% of full reranking's doc_rr@8 gain on the calibration half


def evaluate(rows: list[dict], low_d1: float, high_gap: float) -> dict:
    in_band = [r["d1"] <= low_d1 and r["gap"] <= high_gap for r in rows]
    out = {"n": len(rows), "rerank_calls": int(sum(in_band)), "skipped_share": 1 - float(np.mean(in_band))}
    for m in METRICS:
        dense = np.array([r["dense"][m] for r in rows if r["dense"][m] is not None])
        full = np.array([r["rerank"][m] for r in rows if r["dense"][m] is not None])
        gated = np.array([(r["rerank"] if b else r["dense"])[m] for r, b in zip(rows, in_band) if r["dense"][m] is not None])
        out[m] = {"dense": dense.mean(), "full_rerank": full.mean(), "gated": gated.mean()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default=str(BASE.parents[1] / "p3m3" / "retrieval_eval_records.json"))
    args = ap.parse_args()
    records = json.loads(Path(args.records).read_text())

    client, collection = OpenAI(timeout=30.0), get_collection()
    for i, r in enumerate(records, 1):
        emb, _ = embed_query(client, r["question"])
        pool = query_store(collection, emb, top_k=OVERFETCH_K)
        r["d1"], r["gap"] = confidence_signals(pool["ids"], pool["distances"])
        r["rr_gain"] = r["rerank"]["doc_rr@8"] - r["dense"]["doc_rr@8"]
        if i % 50 == 0:
            print(f"  {i}/{len(records)}", flush=True)

    d1s, gaps, gains = (np.array([r[k] for r in records]) for k in ("d1", "gap", "rr_gain"))
    print(f"\nsignal ranges: d1 {d1s.min():.3f}-{d1s.max():.3f} (median {np.median(d1s):.3f}); "
          f"gap {gaps.min():.3f}-{gaps.max():.3f} (median {np.median(gaps):.3f})")
    for name, sig in (("d1", d1s), ("gap", gaps)):
        print(f"corr({name}, rerank doc_rr gain) = {np.corrcoef(sig, gains)[0, 1]:+.3f}")
    print("\nmean rerank doc_rr gain by signal quartile:")
    for name, sig in (("d1", d1s), ("gap", gaps)):
        qs = np.quantile(sig, [0, .25, .5, .75, 1])
        parts = []
        for lo, hi in zip(qs[:-1], qs[1:]):
            mask = (sig >= lo) & (sig <= hi)
            parts.append(f"[{lo:.3f},{hi:.3f}] {gains[mask].mean():+.3f} (n={mask.sum()})")
        print(f"  {name}: " + "  ".join(parts))

    calib, holdout = records[0::2], records[1::2]
    full_gain = np.mean([r["rr_gain"] for r in calib])
    best = None
    for low_d1 in np.quantile(d1s, np.linspace(0.5, 1.0, 11)):
        for high_gap in np.quantile(gaps, np.linspace(0.3, 1.0, 15)):
            res = evaluate(calib, low_d1, high_gap)
            gain = res["doc_rr@8"]["gated"] - res["doc_rr@8"]["dense"]
            if full_gain > 0 and gain >= MIN_GAIN_RETAINED * full_gain:
                if best is None or res["rerank_calls"] < best[2]["rerank_calls"]:
                    best = (float(low_d1), float(high_gap), res)
    if best is None:
        print("\nNo band retains the required gain on the calibration half -- keep reranking always-on or always-off.")
        return
    low_d1, high_gap, _ = best
    print(f"\nChosen on calibration half (fewest calls retaining >= {MIN_GAIN_RETAINED:.0%} of full-rerank doc_rr gain):")
    print(f"  rerank iff d1 <= {low_d1:.4f} and gap <= {high_gap:.4f}")
    for label, rows in (("calibration (even)", calib), ("HOLD-OUT (odd)", holdout), ("all 200", records)):
        res = evaluate(rows, low_d1, high_gap)
        print(f"\n  {label}: n={res['n']}, reranker calls {res['rerank_calls']} ({1 - res['skipped_share']:.0%} of questions; {res['skipped_share']:.0%} skipped)")
        for m in METRICS:
            v = res[m]
            print(f"    {m:<11} dense {v['dense']:.3f}   full rerank {v['full_rerank']:.3f}   gated {v['gated']:.3f}")
    out = BASE.parents[1] / "p3m3" / "rerank_gate_calibration.json"
    out.write_text(json.dumps({"low_d1": low_d1, "high_gap": high_gap,
                               "rows": [{k: r[k] for k in ("id", "stratum", "d1", "gap", "rr_gain")} for r in records]}, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
