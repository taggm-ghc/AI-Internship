"""Module 2.4 (Reranking and Hybrid Search) exercise, literal version —
the syllabus's own named example: "searching for a policy code like
'POL-114' fails because uncommon codes carry little vector weight" under
dense-only retrieval, while hybrid (BM25 + vector, fused via RRF) resolves
it "without giving up semantic matching to get it."

    python scripts/hybrid_search_comparison.py

Chunks all 6 Northwind sample_docs with the production splitter
(rag_ingest.chunk_text, CHUNK_SIZE=800/CHUNK_OVERLAP=100), builds an
in-memory BM25 index (rank_bm25.BM25Okapi, same tokenization as
rag_service._bm25_index) and dense embeddings, then reuses
rag_service.reciprocal_rank_fusion (the actual production RRF
implementation -- a pure function, no DB dependency) to fuse them. This
does NOT write anything into Postgres or the live corpus; the Northwind
pack is never ingested into the app's collection, only chunked/embedded
in-process for this comparison.
"""
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

from rag_ingest import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text  # noqa: E402
from rag_service import reciprocal_rank_fusion  # noqa: E402

SAMPLE_DOCS_DIR = WORKDIR / "sample_docs"
QUERY = "What does POL-114 cover?"
TARGET_DOC = "doc2_expenses.txt"  # POL-114


def l2_squared(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def main() -> int:
    client = OpenAI(timeout=30.0, max_retries=3)

    chunk_texts: list[str] = []
    chunk_doc: list[str] = []
    for path in sorted(SAMPLE_DOCS_DIR.glob("doc*.txt")):
        for chunk in chunk_text(path.read_text(), CHUNK_SIZE, CHUNK_OVERLAP):
            chunk_texts.append(chunk)
            chunk_doc.append(path.name)
    print(f"Corpus: {len(chunk_texts)} chunks across {len(set(chunk_doc))} Northwind documents\n")

    # Dense retrieval
    query_emb = client.embeddings.create(model="text-embedding-3-small", input=[QUERY]).data[0].embedding
    chunk_embs = [
        d.embedding for d in client.embeddings.create(model="text-embedding-3-small", input=chunk_texts).data
    ]
    dense_ranked = sorted(range(len(chunk_texts)), key=lambda i: l2_squared(query_emb, chunk_embs[i]))

    # Lexical (BM25) retrieval -- same tokenization as rag_service._bm25_index
    tokenized = [c.lower().split() for c in chunk_texts]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(QUERY.lower().split())
    bm25_ranked = sorted(range(len(chunk_texts)), key=lambda i: scores[i], reverse=True)

    # Hybrid: production's own reciprocal_rank_fusion, given each ranking as chunk-index "ids"
    dense_ids = [str(i) for i in dense_ranked]
    bm25_ids = [str(i) for i in bm25_ranked]
    fused_scores = reciprocal_rank_fusion([dense_ids, bm25_ids])
    fused_ranked = sorted(range(len(chunk_texts)), key=lambda i: fused_scores.get(str(i), 0.0), reverse=True)

    def report(label: str, ranking: list[int], top_k: int = 3) -> None:
        print(f"=== {label} — top-{top_k} ===")
        for rank, idx in enumerate(ranking[:top_k], start=1):
            marker = "  <-- target (POL-114)" if chunk_doc[idx] == TARGET_DOC else ""
            preview = chunk_texts[idx].replace("\n", " ")[:90]
            print(f"  #{rank}  {chunk_doc[idx]}  \"{preview}...\"{marker}")
        target_rank = next((r for r, idx in enumerate(ranking, start=1) if chunk_doc[idx] == TARGET_DOC), None)
        print(f"  first POL-114 chunk appears at overall rank: {target_rank}\n")

    print(f'Query: "{QUERY}"  (target: {TARGET_DOC} / POL-114)\n')
    report("DENSE ONLY (current production /ask path)", dense_ranked)
    report("BM25 ONLY (lexical)", bm25_ranked)
    report("HYBRID (dense + BM25, fused via production reciprocal_rank_fusion)", fused_ranked)

    return 0


if __name__ == "__main__":
    sys.exit(main())
