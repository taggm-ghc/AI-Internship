"""p3m3 item #12 -- dense-only vs. hybrid(dense+BM25+RRF) comparison against
the REAL golden-eval set on the REAL 260-document corpus. The only existing
hybrid comparison (scripts/hybrid_search_comparison.py) used a synthetic
6-document Northwind corpus to demonstrate a keyword-miss case in isolation
-- useful for showing the mechanism works, not for deciding whether to wire
hybrid_retrieve() in as /ask's default. rag_service.py's own docstring
(just above _bm25_index) states the actual gate for that decision: "only
wire in if it doesn't regress golden_eval" -- same gate item #9/#10 already
applied to reranking. This script applies it to hybrid retrieval. Read-only
against the corpus; makes real embedding calls (small, real cost -- no
corpus writes)."""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from openai import OpenAI

from rag_service import (
    CONTEXT_K,
    OVERFETCH_K,
    candidates_to_pool,
    embed_query,
    get_collection,
    hybrid_retrieve,
    query_store,
    select_context_chunks,
)


def hybrid_context(question: str, query_embedding: list[float]) -> dict:
    """Runs hybrid_retrieve, then reuses select_context_chunks unchanged via
    rag_service.candidates_to_pool, keeping the fused order (preserve_order)."""
    candidates = hybrid_retrieve(query_embedding, question, top_k=OVERFETCH_K)
    return select_context_chunks(candidates_to_pool(candidates), preserve_order=True)


def main():
    data = json.loads((BASE / "config" / "golden_eval_set.json").read_text())
    cases = [e for e in data["entries"] if e.get("expected_document_id")]
    print(f"{len(cases)} golden-eval cases with an expected document (out of {len(data['entries'])} total).", flush=True)

    client = OpenAI(timeout=30.0)
    collection = get_collection()
    dense_hits, hybrid_hits = 0, 0

    for case in cases:
        question, expected = case["question"], case["expected_document_id"]
        query_embedding, _ = embed_query(client, question)

        dense_overfetched = query_store(collection, query_embedding, top_k=OVERFETCH_K)
        dense_context = select_context_chunks(dense_overfetched, context_k=CONTEXT_K)
        dense_ok = any(cid.rsplit("::", 1)[0] == expected for cid in dense_context["ids"])

        hybrid_ctx = hybrid_context(question, query_embedding)
        hybrid_ok = any(cid.rsplit("::", 1)[0] == expected for cid in hybrid_ctx["ids"])

        dense_hits += dense_ok
        hybrid_hits += hybrid_ok
        flag = "" if dense_ok == hybrid_ok else "  <-- DIFFERS"
        print(f"  [{'PASS' if dense_ok else 'FAIL'}|{'PASS' if hybrid_ok else 'FAIL'}] {question[:70]}{flag}", flush=True)

    print(flush=True)
    print(f"Dense-only:       {dense_hits}/{len(cases)} correct document in final context", flush=True)
    print(f"Dense+hybrid RRF: {hybrid_hits}/{len(cases)} correct document in final context", flush=True)
    print("Gate (rag_service.py, R5 docstring): wire hybrid_retrieve in as /ask's default only if this doesn't regress golden_eval.", flush=True)
    if hybrid_hits < dense_hits:
        print("RESULT: regression -- gate fails, do not wire in as default.", flush=True)
    elif hybrid_hits > dense_hits:
        print("RESULT: improvement -- gate passes.", flush=True)
    else:
        print("RESULT: no change on this measured set -- gate passes (no regression), but no measured benefit either.", flush=True)


if __name__ == "__main__":
    main()
