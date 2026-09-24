"""p3m3 item #40 -- scores dense vs. hybrid vs. dense+rerank retrieval on
config/retrieval_eval_set.json (built by build_retrieval_eval_set.py), plus
config/retrieval_eval_authentic.json if present (hand-written real-style
questions: [{"question": ..., "expected_document_id": ...}]).

Replaces the 6-question pass/fail comparisons in hybrid_default_comparison.py
/ reranking_comparison.py as the basis for the #9/#12 wiring decisions.
Design + research: p3m3/week2-priority-checklist.md, section D-N+6.

Per question and variant:
  doc_hit@5  -- expected document is in select_context_chunks' final context
                (what /ask would actually hand the generator)
  doc_rr@8   -- reciprocal rank of the expected document's first chunk in the
                variant's ranked list, truncated to RERANK_K=8 so all three
                variants are scored over the same depth (rerank only returns 8)
  chunk_rr@8 -- same, for the exact source chunk (synthetic entries only)
  latency    -- retrieval time for that variant, excluding the shared query
                embedding call
Significance, per arXiv:1905.11096 (Urbano et al., SIGIR 2019): paired
t-test and paired sign-flip permutation test on per-question differences vs.
dense. Wilcoxon/sign/bootstrap deliberately not used (that paper recommends
discontinuing them). The t-test p-value uses the normal approximation, which
is close for n>=30; the permutation test is exact up to resampling noise.

Read-only: embedding calls, query_store/bm25/get reads, and the reranker's
LLM call. No /ask, no record_event, no writes.

Run with: python scripts/retrieval_eval.py [--limit N] [--out-dir DIR]
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from openai import OpenAI

from rag_service import (
    CONTEXT_K,
    OVERFETCH_K,
    RERANK_K,
    candidates_to_pool,
    embed_query,
    get_collection,
    hybrid_retrieve,
    query_store,
    rerank_candidates,
    select_context_chunks,
)

VARIANTS = ["dense", "hybrid", "rerank", "production"]
COMPARED = VARIANTS[1:]
METRICS = ["doc_hit@5", "doc_rr@8", "chunk_rr@8"]
PERMUTATIONS = 10_000


def doc_of(chunk_id: str) -> str:
    return chunk_id.rsplit("::", 1)[0]


def rr(ids: list[str], match) -> float:
    for rank, cid in enumerate(ids[:RERANK_K], start=1):
        if match(cid):
            return 1.0 / rank
    return 0.0


def score(pool: dict, entry: dict, ordered: bool) -> dict:
    context = select_context_chunks(pool, context_k=CONTEXT_K, preserve_order=ordered)
    doc = entry["expected_document_id"]
    chunk = entry.get("expected_chunk_id")
    return {
        "doc_hit@5": float(any(doc_of(c) == doc for c in context["ids"])),
        "doc_rr@8": rr(pool["ids"], lambda c: doc_of(c) == doc),
        "chunk_rr@8": rr(pool["ids"], lambda c: c == chunk) if chunk else None,
        "top_ids": pool["ids"][:RERANK_K],
    }


def run_entry(client, collection, entry: dict) -> dict:
    """Variants (item #41 revision): what /ask can actually run now.
    dense      -- the pre-#41 /ask path (distance order)
    hybrid     -- dense + BM25, RRF-fused order kept
    rerank     -- dense -> reranker (pinned model), reranker order kept
    production -- hybrid -> reranker, exactly what /ask runs with both
                  switches on (build_candidate_pool + rerank_candidates)
    The first #40 run's "rerank" used the floating provider chain and the
    distance re-sort bug; its report is kept as retrieval_eval_report_run1.txt."""
    q = entry["question"]
    emb, _ = embed_query(client, q)
    out = {"id": entry["id"], "stratum": entry["stratum"], "question": q}

    t = time.perf_counter()
    dense_pool = query_store(collection, emb, top_k=OVERFETCH_K)
    dense_s = time.perf_counter() - t
    out["dense"] = score(dense_pool, entry, ordered=False) | {"latency_s": dense_s}

    t = time.perf_counter()
    hybrid_pool = candidates_to_pool(hybrid_retrieve(emb, q, top_k=OVERFETCH_K))
    hybrid_s = time.perf_counter() - t
    out["hybrid"] = score(hybrid_pool, entry, ordered=True) | {"latency_s": hybrid_s}

    t = time.perf_counter()
    rerank_pool = rerank_candidates(dense_pool, q, rerank_k=RERANK_K)
    out["rerank"] = score(rerank_pool, entry, ordered=True) | {"latency_s": dense_s + time.perf_counter() - t}

    t = time.perf_counter()
    prod_pool = rerank_candidates(hybrid_pool, q, rerank_k=RERANK_K)
    out["production"] = score(prod_pool, entry, ordered=True) | {"latency_s": hybrid_s + time.perf_counter() - t}
    return out


def paired_tests(a: np.ndarray, b: np.ndarray, rng: np.random.Generator) -> tuple[float, float, float]:
    """Returns (mean difference b-a, t-test p, permutation p), two-sided."""
    d = b - a
    n = len(d)
    mean = float(d.mean())
    if n < 2 or not d.any():
        return mean, 1.0, 1.0
    sd = float(d.std(ddof=1))
    t_p = 1.0 if sd == 0 else math.erfc(abs(mean / (sd / math.sqrt(n))) / math.sqrt(2))
    signs = rng.choice([-1.0, 1.0], size=(PERMUTATIONS, n))
    null = np.abs((signs * d).mean(axis=1))
    perm_p = float((np.sum(null >= abs(mean) - 1e-12) + 1) / (PERMUTATIONS + 1))
    return mean, t_p, perm_p


def report(records: list[dict], label: str, rng: np.random.Generator) -> list[str]:
    lines = [f"\n## {label} (n={len(records)})"]
    lines.append(f"{'metric':<11}" + "".join(f"{v:>15}" for v in VARIANTS) + "".join(f"   {v+'-dense (t p / perm p)':<34}" for v in COMPARED))
    for m in METRICS:
        rows = [r for r in records if r["dense"][m] is not None]
        if not rows:
            continue
        vals = {v: np.array([r[v][m] for r in rows]) for v in VARIANTS}
        line = f"{m:<11}" + "".join(f"{vals[v].mean():>15.3f}" for v in VARIANTS)
        for v in COMPARED:
            diff, tp, pp = paired_tests(vals["dense"], vals[v], rng)
            line += f"   {f'{diff:+.3f} ({tp:.3f} / {pp:.3f})':<34}"
        lines.append(line)
    lat = {v: np.array([r[v]["latency_s"] for r in records]) for v in VARIANTS}
    lines.append(f"{'latency s':<11}" + "".join(f"{lat[v].mean():>15.2f}" for v in VARIANTS) + "   (mean; retrieval only, shared embedding call excluded)")
    for v in COMPARED:
        won = sum(1 for r in records if r[v]["doc_hit@5"] > r["dense"]["doc_hit@5"])
        lost = sum(1 for r in records if r[v]["doc_hit@5"] < r["dense"]["doc_hit@5"])
        lines.append(f"doc_hit@5 discordant pairs, {v} vs dense: {won} {v}-only hits, {lost} dense-only hits")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=str(BASE / "config" / "retrieval_eval_set.json"))
    ap.add_argument("--authentic", default=str(BASE / "config" / "retrieval_eval_authentic.json"))
    ap.add_argument("--limit", type=int, default=None, help="first N entries only (smoke run)")
    ap.add_argument("--out-dir", default=str(BASE.parents[1] / "p3m3"))
    args = ap.parse_args()

    data = json.loads(Path(args.set).read_text())
    entries = data["entries"]
    authentic = Path(args.authentic)
    if authentic.exists():
        for i, e in enumerate(json.loads(authentic.read_text())):
            entries.append({"id": f"a{i:03d}", "stratum": "authentic", "question": e["question"],
                            "expected_document_id": e["expected_document_id"]})
    if args.limit:
        entries = entries[: args.limit]

    collection = get_collection()
    revision = collection.revision()
    if revision != data["meta"]["corpus_revision"]:
        print(f"WARNING: corpus revision {revision} != eval set's {data['meta']['corpus_revision']}; "
              "chunk IDs may have moved -- rebuild the set before trusting chunk_rr@8.", flush=True)

    client = OpenAI(timeout=30.0)
    records = []
    for i, e in enumerate(entries, 1):
        records.append(run_entry(client, collection, e))
        if i % 20 == 0:
            print(f"  {i}/{len(entries)}", flush=True)

    rng = np.random.default_rng(40)
    lines = [f"# Retrieval eval — {len(records)} questions, corpus revision {revision}, "
             f"set built {data['meta']['created_at']} ({data['meta']['generator_model']})"]
    lines += report(records, "All strata", rng)
    for stratum in sorted({r["stratum"] for r in records}):
        lines += report([r for r in records if r["stratum"] == stratum], f"Stratum: {stratum}", rng)
    text = "\n".join(lines)
    print(text, flush=True)

    out = Path(args.out_dir)
    suffix = f"_limit{args.limit}" if args.limit else ""
    (out / f"retrieval_eval_report{suffix}.txt").write_text(text + "\n")
    (out / f"retrieval_eval_records{suffix}.json").write_text(json.dumps(records, indent=1) + "\n")
    print(f"\nwrote {out}/retrieval_eval_report{suffix}.txt and retrieval_eval_records{suffix}.json", flush=True)


if __name__ == "__main__":
    main()
