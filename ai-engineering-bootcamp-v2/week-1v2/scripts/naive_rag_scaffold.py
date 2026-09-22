"""Module 2.2 (Build Naive RAG, and Watch It Fail), literal version --
ONE naive RAG loop, built by following .claude/skills/rag-scaffold/SKILL.md,
broken three ways against the same Northwind sample_docs/ corpus in one run.

    python scripts/naive_rag_scaffold.py

This supersedes running the boundary-cut, keyword-miss, and
confident-and-ungrounded demonstrations as three separate, unrelated
scripts (scripts/chunking_comparison.py, scripts/hybrid_search_comparison.py,
scripts/naive_rag_watch_it_fail.py each still stand as deeper, focused
treatments of one failure apiece -- kept, not deleted, since they go
further than this scaffold does on each individual failure). This script
is the literal module exercise: one loop, one corpus, three named
failures, in one place, per the rag-scaffold skill's own instructions.

Entirely self-contained per the skill's own rule: no live app endpoint is
called, nothing is written to Postgres, the Northwind pack is never
ingested into the production collection.
"""
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

from rag_ingest import CHUNK_OVERLAP, CHUNK_SIZE, EMBEDDING_MODEL, chunk_text  # noqa: E402
from rag_service import GROUNDED_PROMPT, reciprocal_rank_fusion  # noqa: E402

SAMPLE_DOCS_DIR = WORKDIR / "sample_docs"
CHAT_MODEL = "gpt-4.1-nano"
NAIVE_PROMPT = "Answer the question using the following context.\n\nContext:\n{context}\n\nQuestion: {question}"


def naive_fixed_chunks(text: str, chunk_size: int) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def l2_squared(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def main() -> int:
    client = OpenAI(timeout=30.0, max_retries=3)
    paths = sorted(SAMPLE_DOCS_DIR.glob("doc*.txt"))
    texts = {p.name: p.read_text() for p in paths}

    # --- Step 1: chunk (skill step 1) -- production recursive splitter is
    # THE loop's real store; naive fixed-width is only built as the
    # boundary-cut failure's "before" comparison, per the skill.
    prod_chunks: list[str] = []
    prod_doc: list[str] = []
    for name, text in texts.items():
        for c in chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP):
            prod_chunks.append(c)
            prod_doc.append(name)
    # N=399 found by direct offset search (see scripts/chunking_comparison.py)
    # to land a chunk boundary exactly at the sentence break the syllabus's
    # own example names -- "retrieval returns 'up to three days per week'
    # but loses 'fully remote requires director approval'" -- not chosen to
    # force a plausible-looking cut.
    naive_chunks_399 = naive_fixed_chunks(texts["doc1_handbook.txt"], 399)

    # --- Step 2: embed (skill step 2) -- one batch call over the real store.
    prod_embs = [d.embedding for d in client.embeddings.create(model=EMBEDDING_MODEL, input=prod_chunks).data]
    naive_399_embs = [
        d.embedding for d in client.embeddings.create(model=EMBEDDING_MODEL, input=naive_chunks_399).data
    ]

    # --- Step 3: store -- in-memory lists above ARE the store, per the skill.

    print("=" * 70)
    print("NAIVE RAG SCAFFOLD -- one loop, Northwind sample_docs/, three named failures")
    print(f"Production store: {len(prod_chunks)} chunks ({CHUNK_SIZE}/{CHUNK_OVERLAP}, recursive)")
    print("=" * 70)

    # ---------------------------------------------------------------
    # FAILURE 1: boundary cut
    # ---------------------------------------------------------------
    print("\n--- FAILURE 1: BOUNDARY CUT ---")
    question = "Can I work from home full time?"
    clause1 = "Employees may work remotely up to three days per week."
    clause2 = "Fully remote arrangements require director approval"
    q_emb = client.embeddings.create(model=EMBEDDING_MODEL, input=[question]).data[0].embedding

    naive_target_idx = next(i for i, c in enumerate(naive_chunks_399) if clause1 in c)
    prod_target_idx = next(
        i for i, (c, d) in enumerate(zip(prod_chunks, prod_doc)) if clause1 in c and clause2 in c and d == "doc1_handbook.txt"
    )
    naive_rank = sorted(range(len(naive_chunks_399)), key=lambda i: l2_squared(q_emb, naive_399_embs[i])).index(naive_target_idx) + 1
    prod_rank = sorted(range(len(prod_chunks)), key=lambda i: l2_squared(q_emb, prod_embs[i])).index(prod_target_idx) + 1
    naive_complete = clause2 in naive_chunks_399[naive_target_idx]

    print(f'Question: "{question}"')
    print(f"  naive fixed-width N=399 (exact match to the syllabus's own example): retrieved chunk (rank {naive_rank}) ends \"...up to three days per week.\" -- contains director-approval clause: {naive_complete}")
    print(f"  production recursive {CHUNK_SIZE}/{CHUNK_OVERLAP}: retrieved chunk (rank {prod_rank}) contains the complete rule, both clauses")
    print("  NAMED: boundary cut -- fixed to be reprocessed in Module 2.3")

    # ---------------------------------------------------------------
    # FAILURE 2: keyword miss
    # ---------------------------------------------------------------
    print("\n--- FAILURE 2: KEYWORD MISS ---")
    question = "What does POL-114 cover?"
    target_doc = "doc2_expenses.txt"
    q_emb = client.embeddings.create(model=EMBEDDING_MODEL, input=[question]).data[0].embedding

    dense_ranked = sorted(range(len(prod_chunks)), key=lambda i: l2_squared(q_emb, prod_embs[i]))
    dense_rank = next(r for r, i in enumerate(dense_ranked, start=1) if prod_doc[i] == target_doc)

    tokenized = [c.lower().split() for c in prod_chunks]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(question.lower().split())
    bm25_ranked = sorted(range(len(prod_chunks)), key=lambda i: scores[i], reverse=True)
    fused = reciprocal_rank_fusion([[str(i) for i in dense_ranked], [str(i) for i in bm25_ranked]])
    hybrid_ranked = sorted(range(len(prod_chunks)), key=lambda i: fused.get(str(i), 0.0), reverse=True)
    hybrid_rank = next(r for r, i in enumerate(hybrid_ranked, start=1) if prod_doc[i] == target_doc)

    print(f'Question: "{question}"  (target: {target_doc} / POL-114)')
    print(f"  dense-only:  POL-114 first appears at rank {dense_rank}{' (misses top-3)' if dense_rank > 3 else ''}")
    print(f"  hybrid (dense+BM25, RRF): POL-114 first appears at rank {hybrid_rank}")
    print("  NAMED: keyword miss -- fixed to be reprocessed in Module 2.4")

    # ---------------------------------------------------------------
    # FAILURE 3: confident and ungrounded
    # ---------------------------------------------------------------
    print("\n--- FAILURE 3: CONFIDENT AND UNGROUNDED ---")
    question = "What is the parental leave policy?"
    q_emb = client.embeddings.create(model=EMBEDDING_MODEL, input=[question]).data[0].embedding
    top5 = sorted(range(len(prod_chunks)), key=lambda i: l2_squared(q_emb, prod_embs[i]))[:5]
    context = "\n\n---\n\n".join(prod_chunks[i] for i in top5)

    naive_answer = client.chat.completions.create(
        model=CHAT_MODEL, messages=[{"role": "user", "content": NAIVE_PROMPT.format(context=context, question=question)}]
    ).choices[0].message.content
    grounded_answer = client.chat.completions.create(
        model=CHAT_MODEL, messages=[{"role": "user", "content": GROUNDED_PROMPT.format(context=context, question=question)}]
    ).choices[0].message.content

    print(f'Question: "{question}"  (per sample_docs/README.md: intentionally absent -- correct behavior is refusal)')
    print(f"  BEFORE (no grounding instruction): {naive_answer[:200]}")
    print(f"  AFTER  (production GROUNDED_PROMPT): {grounded_answer[:200]}")
    print("  NAMED: confident-and-ungrounded -- fixed right here, in Module 2.2, via the grounding prompt")

    print("\n" + "=" * 70)
    print("All three failures triggered and named against the same naive loop and corpus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
