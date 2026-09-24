"""p3m3 item #10 -- dense vs. dense+reranked comparison, unblocked by item
#9's rerank_candidates(). Reuses the existing golden-eval question set
(config/golden_eval_set.json) rather than inventing new test cases --
same discipline as scripts/hybrid_search_comparison.py reusing production
primitives instead of reimplementing them. Read-only against the corpus;
makes real embedding + reranking LLM calls (small, real cost -- no
corpus writes)."""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from openai import OpenAI

from rag_service import (
    CONTEXT_K,
    OVERFETCH_K,
    RERANK_K,
    embed_query,
    get_collection,
    query_store,
    rerank_candidates,
    select_context_chunks,
)


def main():
    data = json.loads((BASE / "config" / "golden_eval_set.json").read_text())
    cases = [e for e in data["entries"] if e.get("expected_document_id")]
    print(f"{len(cases)} golden-eval cases with an expected document (out of {len(data['entries'])} total).", flush=True)

    client = OpenAI(timeout=30.0)
    collection = get_collection()
    dense_hits, reranked_hits, rerank_latencies = 0, 0, []

    for case in cases:
        question, expected = case["question"], case["expected_document_id"]
        query_embedding, _ = embed_query(client, question)
        overfetched = query_store(collection, query_embedding, top_k=OVERFETCH_K)

        dense_context = select_context_chunks(overfetched, context_k=CONTEXT_K)
        dense_ok = any(cid.rsplit("::", 1)[0] == expected for cid in dense_context["ids"])

        start = time.perf_counter()
        reranked_pool = rerank_candidates(overfetched, question, rerank_k=RERANK_K)
        rerank_latencies.append(time.perf_counter() - start)
        reranked_context = select_context_chunks(reranked_pool, context_k=CONTEXT_K, preserve_order=True)
        reranked_ok = any(cid.rsplit("::", 1)[0] == expected for cid in reranked_context["ids"])

        dense_hits += dense_ok
        reranked_hits += reranked_ok
        flag = "" if dense_ok == reranked_ok else "  <-- DIFFERS"
        print(f"  [{'PASS' if dense_ok else 'FAIL'}|{'PASS' if reranked_ok else 'FAIL'}] {question[:70]}{flag}", flush=True)

    print(flush=True)
    print(f"Dense-only:      {dense_hits}/{len(cases)} correct document in final context", flush=True)
    print(f"Dense+reranked:  {reranked_hits}/{len(cases)} correct document in final context", flush=True)
    print(f"Reranking added latency: mean {sum(rerank_latencies)/len(rerank_latencies):.2f}s, one extra LLM call per question", flush=True)


if __name__ == "__main__":
    main()
