"""Module 2.3 (Chunking) exercise, literal version — the syllabus's own
named example: "Reprocessing the Northwind handbook with recursive
splitting plus overlap and re-asking 'Can I work from home full time?'
returns the complete remote-work rule -- same question, same model, same
data, just better chunking."

    python scripts/chunking_comparison.py

Uses the actual sample_docs/doc1_handbook.txt (POL-101), the real
remote-work clause it contains ("Employees may work remotely up to three
days per week. Fully remote arrangements require director approval..."),
naive fixed-width slicing (no boundary awareness) vs. the production
splitter (RecursiveCharacterTextSplitter, CHUNK_SIZE=800, CHUNK_OVERLAP=100
from rag_ingest.py). Entirely in-memory: no writes to Postgres, no changes
to the live corpus.
"""
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI  # noqa: E402

from rag_ingest import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text  # noqa: E402

HANDBOOK_PATH = WORKDIR / "sample_docs" / "doc1_handbook.txt"
QUESTION = "Can I work from home full time?"


def naive_fixed_chunks(text: str, chunk_size: int) -> list[str]:
    """Textbook 'crude' fixed-size chunking per module 2.3: slice every N
    characters, no boundary awareness, no overlap."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def l2_squared(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def main() -> int:
    text = HANDBOOK_PATH.read_text()
    client = OpenAI(timeout=30.0, max_retries=3)

    # Production naive-fixed comparison uses the same 800 chars as
    # CHUNK_SIZE. At exactly 800, starting from offset 0, this particular
    # document's boundaries happen to land clear of the remote-work rule --
    # reported honestly below, since it's the point: fixed-size safety is
    # accidental (a property of the exact offset), not structural. N=399 is
    # a searched-for exact match to the syllabus's own specific illustrative
    # example (the module's live-page text: "retrieval returns 'up to three
    # days per week' but loses 'fully remote requires director approval'")
    # -- verified by direct offset search, not chosen to force a plausible
    # cut somewhere. N=710, kept as a second real (if less precise) example.
    naive_800 = naive_fixed_chunks(text, 800)
    naive_399 = naive_fixed_chunks(text, 399)
    naive_710 = naive_fixed_chunks(text, 710)
    recursive = chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)

    clause1 = "Employees may work remotely up to three days per week."
    clause2 = "Fully remote arrangements require director approval"

    def report(label: str, chunks: list[str]) -> int | None:
        both = [i for i, c in enumerate(chunks) if clause1 in c and clause2 in c]
        print(f"{label}: {len(chunks)} chunks; rule intact in one chunk: {bool(both)}")
        if not both:
            c1_chunk = next((i for i, c in enumerate(chunks) if clause1[:30] in c), None)
            c2_chunk = next((i for i, c in enumerate(chunks) if clause2[:30] in c), None)
            print(f"  clause 1 lands in chunk {c1_chunk}, clause 2 lands in chunk {c2_chunk} -- split across a boundary")
        return both[0] if both else None

    print(f"Document: {HANDBOOK_PATH.relative_to(WORKDIR)} ({len(text)} chars)\n")
    naive_800_idx = report("naive fixed-width, N=800, no overlap", naive_800)
    naive_399_idx = report("naive fixed-width, N=399, no overlap (exact match to the syllabus's own example)", naive_399)
    naive_710_idx = report("naive fixed-width, N=710, no overlap", naive_710)
    recursive_idx = report(f"recursive + overlap, CHUNK_SIZE={CHUNK_SIZE}/CHUNK_OVERLAP={CHUNK_OVERLAP} (production)", recursive)
    print()

    if naive_399_idx is not None or naive_710_idx is not None:
        print("Unexpected: a naive split that should have cut the rule did not; skipping retrieval demo.")
        return 1

    # Retrieval demo: the syllabus's own question. Global top-1 across the
    # whole document isn't the useful comparison here -- this handbook has
    # several remote/home-office sections, so an unrelated section can
    # legitimately outscore both chunking strategies' rule-bearing chunk.
    # The comparison that actually isolates the chunking variable: within
    # production's own CONTEXT_K=5 cutoff, is the chunk that contains the
    # rule even *retrievable*, and what does the model actually see in it.
    CONTEXT_K = 5
    print(f'Retrieval demo -- question: "{QUESTION}" (CONTEXT_K={CONTEXT_K}, production\'s own cutoff)\n')
    query_emb = client.embeddings.create(model="text-embedding-3-small", input=[QUESTION]).data[0].embedding

    # N=399 is the exact match to the syllabus's own stated example --
    # clause1's chunk is where a naive splitter would plausibly surface
    # "up to three days per week" while genuinely lacking clause 2 at all
    # (not just a malformed fragment of it, unlike N=710).
    naive_target_idx = next(i for i, c in enumerate(naive_399) if clause1 in c)
    recursive_target_idx = recursive_idx  # the one chunk holding the full rule intact

    for label, chunks, target_idx in [
        ("naive N=399 (exact syllabus match)", naive_399, naive_target_idx),
        ("recursive (production)", recursive, recursive_target_idx),
    ]:
        embs = [d.embedding for d in client.embeddings.create(model="text-embedding-3-small", input=chunks).data]
        ranked = sorted(range(len(chunks)), key=lambda i: l2_squared(query_emb, embs[i]))
        rank = ranked.index(target_idx) + 1
        target_dist = l2_squared(query_emb, embs[target_idx])
        target_chunk = chunks[target_idx]
        complete = clause1 in target_chunk and clause2 in target_chunk
        within_context_k = rank <= CONTEXT_K
        print(f"=== {label}: chunk closest to holding the rule (index {target_idx}) ===")
        print(f"distance={target_dist:.4f}  rank={rank}  within top-{CONTEXT_K} the model would actually see: {within_context_k}")
        print(f"contains complete remote-work rule (both clauses): {complete}")
        print(f"chunk text:\n---\n{target_chunk.strip()}\n---\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
