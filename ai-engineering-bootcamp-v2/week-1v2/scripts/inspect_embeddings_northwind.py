"""Module 2.1 (Embeddings Intuition), the LITERAL exercise — not the
adapted one in scripts/inspect_embeddings.py.

    python scripts/inspect_embeddings_northwind.py

"Embed a handful of Northwind sentences, embed some questions, and read the
similarity scores. You will watch a question with zero shared words score
high against the right sentence, and near zero against an unrelated one."

Uses the actual sample_docs/ pack (fetched from the syllabus's own zip URL,
unpacked 2026-09-21), the project's locked embedding model
(text-embedding-3-small), and the project's own metric (squared L2, lower =
more similar). Pure in-memory embedding comparison -- no Postgres, no
/debug/retrieve, no live app involved, nothing written back to the corpus.
"""
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKDIR))

from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI  # noqa: E402

EMBEDDING_MODEL = "text-embedding-3-small"

# One representative sentence per document (doc IDs per sample_docs/README.md).
SENTENCES = {
    "POL-101 (handbook)": "Employees may work remotely up to three days per week.",
    "POL-101 (handbook, full remote)": "Fully remote arrangements require director approval and are reviewed every six months.",
    "POL-114 (expenses)": "Mileage is reimbursed at 45 pence per mile once an employee exceeds fifty miles in a single trip.",
    "POL-207 (security)": "Passwords must be at least fourteen characters long and rotated every ninety days.",
    "SPEC-WB9 (product)": "The WB-9 warehouse robot carries a maximum payload of twenty-five kilograms.",
    "POL-220 (IT, noise)": "Employees must use the approved software catalogue for any new application installs.",
    "POL-118 (facilities, noise)": "Desks in Cambridge and Berlin are booked through the Workplace app by five p.m. the day before.",
}

# Zero-shared-vocabulary paraphrases of a subset of the above, plus one
# genuinely off-topic question -- this is the exact demonstration the
# module asks for: a question sharing no words with the right sentence
# should still score close to it, and far from an unrelated one.
QUESTIONS = {
    "How often is a change of login credentials required?": "POL-207 (security)",
    "Who has to sign off on a permanent work-from-anywhere setup?": "POL-101 (handbook, full remote)",
    "What's the heaviest load the warehouse unit can lift?": "SPEC-WB9 (product)",
    "What is a good recipe for lasagna?": None,  # genuinely unrelated -- off-topic control
}


def l2_squared(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def main() -> int:
    client = OpenAI(timeout=20.0, max_retries=3)

    sentence_labels = list(SENTENCES.keys())
    sentence_texts = list(SENTENCES.values())
    sentence_embs = [
        d.embedding
        for d in client.embeddings.create(model=EMBEDDING_MODEL, input=sentence_texts).data
    ]

    question_labels = list(QUESTIONS.keys())
    question_embs = [
        d.embedding
        for d in client.embeddings.create(model=EMBEDDING_MODEL, input=question_labels).data
    ]

    print(f"Embedding model: {EMBEDDING_MODEL} | metric: squared L2 (lower = more similar)\n")

    for question, q_emb, expected_label in zip(question_labels, question_embs, QUESTIONS.values()):
        print(f"=== \"{question}\"")
        if expected_label:
            print(f"    expected match: {expected_label}  -- \"{SENTENCES[expected_label]}\"")
        else:
            print("    expected match: none (off-topic control)")
        scored = sorted(
            zip(sentence_labels, sentence_texts, sentence_embs),
            key=lambda t: l2_squared(q_emb, t[2]),
        )
        for label, text, emb in scored:
            dist = l2_squared(q_emb, emb)
            marker = "  <-- expected" if label == expected_label else ""
            shared_words = set(question.lower().split()) & set(text.lower().split())
            print(f"    distance={dist:.4f}  shared_words={sorted(shared_words) or '{}'}  {label}{marker}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
