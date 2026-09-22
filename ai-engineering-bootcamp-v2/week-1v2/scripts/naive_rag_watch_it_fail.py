"""Module 2.2 (Build Naive RAG, and Watch It Fail) exercise, literal
version -- the third named failure, "confident and ungrounded": "Without
grounding instructions, the model will fabricate answers about policies
that don't exist."

    python scripts/naive_rag_watch_it_fail.py

A fully standalone naive RAG loop over the actual Northwind sample_docs
(chunk -> embed -> in-memory store -> embed question -> retrieve nearest
chunks -> insert into prompt), deliberately built separate from the live
app/Postgres corpus so this can ask a question the real Northwind pack
does not answer (per sample_docs/README.md's own suggested golden set,
question 5: "What is the parental leave policy?" -> refuse, not in
corpus) without touching production data. Runs the SAME retrieved context
through two prompts: a naive one with no grounding instruction (expected
to fabricate), then the project's actual production GROUNDED_PROMPT
(imported unmodified from rag_service.py) to show the fix. No writes to
Postgres, no changes to the live corpus -- this never touches the app.
"""
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI  # noqa: E402

from rag_ingest import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text  # noqa: E402
from rag_service import GROUNDED_PROMPT  # noqa: E402

SAMPLE_DOCS_DIR = WORKDIR / "sample_docs"
QUESTION = "What is the parental leave policy?"
TOP_K = 5
CHAT_MODEL = "gpt-4.1-nano"  # same pin golden_eval.py uses, to isolate the prompt as the variable

NAIVE_PROMPT = """Answer the question using the following context.

Context:
{context}

Question: {question}"""


def l2_squared(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def main() -> int:
    client = OpenAI(timeout=30.0, max_retries=3)

    chunks: list[str] = []
    for path in sorted(SAMPLE_DOCS_DIR.glob("doc*.txt")):
        chunks.extend(chunk_text(path.read_text(), CHUNK_SIZE, CHUNK_OVERLAP))
    print(f"Naive RAG loop: {len(chunks)} chunks from {len(list(SAMPLE_DOCS_DIR.glob('doc*.txt')))} Northwind documents (in-memory store, no Postgres)\n")

    chunk_embs = [d.embedding for d in client.embeddings.create(model="text-embedding-3-small", input=chunks).data]
    query_emb = client.embeddings.create(model="text-embedding-3-small", input=[QUESTION]).data[0].embedding

    ranked = sorted(range(len(chunks)), key=lambda i: l2_squared(query_emb, chunk_embs[i]))[:TOP_K]
    retrieved = [chunks[i] for i in ranked]
    context = "\n\n---\n\n".join(retrieved)

    print(f'Question: "{QUESTION}"  (per sample_docs/README.md: parental leave is intentionally absent from this corpus -- correct behavior is refusal)\n')
    print(f"Retrieved top-{TOP_K} chunks (nearest by embedding distance, none of them actually about parental leave):")
    for i in ranked:
        print(f"  - {chunks[i][:70].strip()}...")
    print()

    naive_response = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": NAIVE_PROMPT.format(context=context, question=QUESTION)}],
    )
    print("=== BEFORE -- naive prompt, no grounding instruction ===")
    print(naive_response.choices[0].message.content)
    print()

    grounded_response = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": GROUNDED_PROMPT.format(context=context, question=QUESTION)}],
    )
    print("=== AFTER -- production GROUNDED_PROMPT (rag_service.py, imported unmodified) ===")
    print(grounded_response.choices[0].message.content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
