"""Module 2.1 (Embeddings Intuition) exercise — inspect real similarity
scores on this project's actual corpus instead of just trusting that
retrieval works.

    python scripts/inspect_embeddings.py

Runs every question in config/golden_eval_set.json through the same
embed_query + query_store path GET /debug/retrieve uses (no HTTP server
needed — calls rag_service directly, same in-process pattern
golden_eval.py's preflight_check already uses), top_k=5, and prints the raw
distances. Read-only: no writes to Postgres, no chunk/embedding changes.
"""
import json
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKDIR))  # same convention as scripts/migrate_operational_store.py etc.

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI  # noqa: E402  (after load_dotenv, matches main.py's ordering)

from rag_service import RAG_RELEVANCE_THRESHOLD, embed_query, get_collection, query_store  # noqa: E402

GOLDEN_SET_PATH = WORKDIR / "config" / "golden_eval_set.json"


def main() -> int:
    data = json.loads(GOLDEN_SET_PATH.read_text())
    cases = data["entries"] if isinstance(data, dict) else data

    client = OpenAI(timeout=20.0, max_retries=3)
    collection = get_collection()

    print(f"Corpus: collection revision {collection.revision()}, {collection.count()} chunks")
    print(f"Embedding model: text-embedding-3-small | metric: squared L2 (pgvector `<->`, squared in Python)")
    print(f"RAG_RELEVANCE_THRESHOLD = {RAG_RELEVANCE_THRESHOLD}\n")

    for case in cases:
        question = case["question"]
        expected_status = case["expected_status"]
        expected_doc = case.get("expected_document_id")
        embedding, tokens = embed_query(client, question)
        result = query_store(collection, embedding, top_k=5)

        print(f"=== {question}")
        print(f"    expected_status={expected_status} expected_document_id={expected_doc} (query embed: {tokens} tokens)")
        for rank, (chunk_id, doc_id, dist) in enumerate(
            zip(result["ids"], [m["document_id"] for m in result["metadatas"]], result["distances"]), start=1
        ):
            gate = "PASS" if dist <= RAG_RELEVANCE_THRESHOLD else "gated out"
            marker = " <-- expected doc" if doc_id == expected_doc else ""
            print(f"    #{rank}  distance={dist:.4f}  [{gate}]  {doc_id}  ({chunk_id}){marker}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
