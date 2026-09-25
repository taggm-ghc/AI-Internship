"""p3m3 item #40 -- builds a stratified, versioned retrieval eval set from
this project's own corpus, large enough to actually separate the dense /
hybrid / rerank variants (items #9, #12) that a 6-question golden-set
comparison could not. Design + research reconciliation:
p3m3/week2-priority-checklist.md, section D-N+6.

Ground truth for each question is the chunk it was generated from (and
that chunk's document). Strata aim at what each variant is supposed to fix:
  paraphrase       -- no distinctive terms reused; the dense baseline case
  short            -- <=8 words, underspecified, how people actually type
                      (arXiv:2609.14579: long synthetic queries favoured a
                      slower hybrid retriever that didn't help real short
                      queries -- so short queries must be in the set)
  exact_term       -- hinges on an identifier/acronym/number (BM25's case)
  hard_distractor  -- the chunk's nearest neighbour from ANOTHER document is
                      close; the question must separate them (reranker's case)

Read-only against the corpus: one bulk get(), per-chunk embedding reads and
query_store() for distractors. No /ask, no record_event, no writes. The
generator/judge model is pinned (GEN_MODEL, default gpt-4.1-nano, the lowest
cost; --model to override) and the resolved snapshot is recorded, so
scores stay comparable across runs.

Run with: python scripts/build_retrieval_eval_set.py [--out config/retrieval_eval_set.json]
"""
import argparse
import datetime
import faulthandler
import json
import random
import re
import signal
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from openai import OpenAI

from rag_service import get_collection, query_store

# Lowest-cost model by default (project rule, 2026-09-25): gpt-4.1-nano is 4x
# cheaper than gpt-4.1-mini. The committed set (config/retrieval_eval_set.json,
# corpus revision 167) was generated with mini on a judge-quality hunch that
# was never measured. Use --model to escalate only for a demonstrated gap,
# e.g. nano's judge passing leaked or vague questions in a spot-check.
GEN_MODEL = "gpt-4.1-nano"
# v2 (2026-09-23): v1 spot-check found prompt vocabulary leaking into questions ("How does PASSAGE A...",
# "...in the passage?") past the judge, and paraphrase questions too vague to have one right document
# ("Which method shows better performance during training evaluations on two test sets?"). Added
# LEAK_PATTERN, the SPECIFICITY rule for the generator, and a `specific` judge check.
PROMPT_VERSION = "2"
SEED = 40
TARGETS = {"paraphrase": 80, "short": 50, "exact_term": 40, "hard_distractor": 30}
MAX_WORDS = {"paraphrase": 15, "short": 8, "exact_term": 15, "hard_distractor": 15}
MIN_CHUNK_CHARS = 400
MAX_PARAPHRASE_OVERLAP = 0.5  # share of question content words found verbatim in the chunk
# squared L2, same metric as RAG_RELEVANCE_THRESHOLD. Probed 2026-09-23 on 30 random chunks: the nearest
# other-document chunk ranged 0.61-0.88 (median 0.79), so 0.75 keeps roughly the closest quarter.
DISTRACTOR_MAX_DISTANCE = 0.75
DISTRACTOR_SEARCH_K = 40  # at k=15 only 13/30 chunks had any other-document neighbour at all
DISTRACTOR_TRIES_PER_DOC = 3
WORKERS = 8
# v2's first run hung silently for 30+ min inside exact_term (all threads parked on futexes, ~0 CPU,
# no ptrace to inspect it). Hence: a hard per-item timeout, SIGUSR1 stack dumps, and per-stratum
# checkpoints so a restart doesn't regenerate finished strata.
ITEM_TIMEOUT_S = 180

STOPWORDS = set("""a an the of in on for to and or with by from as at is are was were be been being this that these
those what which who whom how why when where does do did can could would should will may might it its their there
than then into about over under between within without paper study authors approach method model models""".split())

LEAK_PATTERN = re.compile(r"\b(passage|excerpt|this paper|the paper|this study|the study|the authors|this work|the text)\b", re.I)
SPECIFICITY = (
    " The reader is searching a library of about 260 machine-learning research papers and has NOT seen this "
    "text: never mention 'passage', 'paper', 'study', 'authors' or 'text', and make the question specific "
    "enough that it points to the source paper's particular content rather than to ML research in general."
)

STRATUM_INSTRUCTIONS = {
    "paraphrase": (
        "Write one question that this passage answers. Paraphrase: do NOT reuse the passage's distinctive "
        "terms, names, or phrases -- use different words for the same idea. At most 15 words."
    ),
    "short": (
        "Write one very short search query (at most 8 words) that someone looking for this passage's "
        "information might type -- terse, keyword-like, possibly underspecified, like a real user query. "
        "No full sentence needed."
    ),
    "exact_term": (
        "Write one question that hinges on a specific identifier in the passage -- an acronym, a named "
        "method/model/dataset, or a specific number -- and include that exact term in the question. "
        "At most 15 words. If the passage has no such distinctive term, set skip=true."
    ),
    "hard_distractor": (
        "Write one question that PASSAGE A answers but PASSAGE B (a similar passage from a different "
        "paper) does not. The question must hinge on what distinguishes A from B. At most 15 words. "
        "If you cannot, set skip=true."
    ),
}

GEN_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "question", "strict": True,
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"question": {"type": "string"}, "skip": {"type": "boolean"}},
                   "required": ["question", "skip"]},
    },
}
JUDGE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "judgement", "strict": True,
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"standalone": {"type": "boolean"},
                                  "specific": {"type": "boolean"},
                                  "answerable_from_a": {"type": "boolean"},
                                  "answerable_from_b": {"type": "boolean"}},
                   "required": ["standalone", "specific", "answerable_from_a", "answerable_from_b"]},
    },
}
JUDGE_PROMPT = """Judge a search question against one or two passages.
- standalone: true if the question makes sense on its own to someone who has not seen the passage (no "this paper", "the authors", "the passage", "the proposed method" without naming it).
- specific: true if the question targets particular content of PASSAGE A's source (a named method, result, setting, or claim) such that, in a library of ~260 ML papers, it would point mainly to that source -- false if many different ML papers could equally answer it.
- answerable_from_a: true if PASSAGE A alone contains the information the question asks for.
- answerable_from_b: true if PASSAGE B alone contains it (false if there is no PASSAGE B).

QUESTION: {question}

PASSAGE A:
{a}

PASSAGE B:
{b}"""


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{3,}", text.lower()) if w not in STOPWORDS}


def looks_like_references(text: str) -> bool:
    citations = len(re.findall(r"\[\d+\]|et al\.|arXiv:|doi\.org|\(\d{4}\)\.", text))
    return citations >= 5 or text.lstrip().lower().startswith("references")


def chat_json(client: OpenAI, prompt: str, schema: dict) -> tuple[dict, str]:
    resp = client.chat.completions.create(
        model=GEN_MODEL, temperature=0.2, response_format=schema,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content), resp.model


def nearest_other_doc(collection, chunk_id: str, doc_id: str):
    emb = collection.get(ids=[chunk_id], include=["embeddings"])["embeddings"][0]
    hits = query_store(collection, emb, top_k=DISTRACTOR_SEARCH_K)
    for cid, text, meta, dist in zip(hits["ids"], hits["documents"], hits["metadatas"], hits["distances"]):
        if meta["document_id"] != doc_id:
            return cid, text, dist
    return None


def make_item(client, collection, stratum, chunk):
    cid, text, doc_id = chunk
    distractor = None
    if stratum == "hard_distractor":
        distractor = nearest_other_doc(collection, cid, doc_id)
        if not distractor or distractor[2] > DISTRACTOR_MAX_DISTANCE:
            return None, "no_close_distractor", None
        prompt = f"{STRATUM_INSTRUCTIONS[stratum]}{SPECIFICITY}\n\nPASSAGE A:\n{text}\n\nPASSAGE B:\n{distractor[1]}"
    else:
        prompt = f"{STRATUM_INSTRUCTIONS[stratum]}{SPECIFICITY}\n\nPASSAGE:\n{text}"
    gen, model = chat_json(client, prompt, GEN_SCHEMA)
    q = gen["question"].strip()
    if gen["skip"] or not q:
        return None, "generator_skipped", model
    if LEAK_PATTERN.search(q):
        return None, "leaked_reference", model
    if len(q.split()) > MAX_WORDS[stratum]:
        return None, "too_long", model
    if stratum == "paraphrase":
        words = content_words(q)
        if words and len(words & content_words(text)) / len(words) > MAX_PARAPHRASE_OVERLAP:
            return None, "lexical_overlap", model
    judge, _ = chat_json(client, JUDGE_PROMPT.format(question=q, a=text, b=distractor[1] if distractor else "(none)"), JUDGE_SCHEMA)
    if not (judge["standalone"] and judge["specific"] and judge["answerable_from_a"]):
        return None, "judge_rejected", model
    if distractor and judge["answerable_from_b"]:
        return None, "distractor_also_answers", model
    item = {"question": q, "stratum": stratum, "expected_chunk_id": cid, "expected_document_id": doc_id}
    if distractor:
        item.update(distractor_chunk_id=distractor[0], distractor_distance=round(distractor[2], 4))
    return item, "kept", model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BASE / "config" / "retrieval_eval_set.json"))
    ap.add_argument("--model", default=GEN_MODEL, help="generator + judge model (default: lowest cost)")
    args = ap.parse_args()
    globals()["GEN_MODEL"] = args.model
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>` dumps every thread's stack
    partial_path = Path(args.out + ".partial")
    checkpoint = json.loads(partial_path.read_text()) if partial_path.exists() else None

    collection = get_collection()
    revision = collection.revision()
    allc = collection.get(include=["documents", "metadatas"])
    pool = [
        (cid, text, meta["document_id"])
        for cid, text, meta in zip(allc["ids"], allc["documents"], allc["metadatas"])
        if not cid.endswith("::0") and len(text) >= MIN_CHUNK_CHARS and not looks_like_references(text)
    ]
    print(f"corpus revision {revision}: {len(allc['ids'])} chunks, {len(pool)} eligible", flush=True)

    rng = random.Random(SEED)
    by_doc: dict[str, list] = {}
    for c in pool:
        by_doc.setdefault(c[2], []).append(c)
    client = OpenAI(timeout=60.0, max_retries=2)
    entries, reasons, models, used_chunks = [], Counter(), set(), set()
    done: dict[str, list] = {}
    if checkpoint and (checkpoint["prompt_version"], checkpoint["seed"], checkpoint["corpus_revision"]) == (PROMPT_VERSION, SEED, revision):
        done = checkpoint["strata"]
        reasons.update(checkpoint["filter_counts"])
        models.update(checkpoint["models"])
        print(f"resuming from checkpoint: {sorted(done)} already complete", flush=True)

    for stratum, target in TARGETS.items():
        # one chunk per document per stratum, docs in seeded order; oversample to absorb rejections
        docs = sorted(by_doc)
        rng.shuffle(docs)
        per_doc = DISTRACTOR_TRIES_PER_DOC if stratum == "hard_distractor" else 1
        candidates = []
        for d in docs:
            options = [c for c in by_doc[d] if c[0] not in used_chunks]
            candidates.extend(rng.sample(options, min(per_doc, len(options))))
        if stratum in done:  # candidate sampling above still ran, so the seeded RNG stays in step
            kept = done[stratum]
            used_chunks.update(e["expected_chunk_id"] for e in kept)
            print(f"  {stratum}: kept {len(kept)}/{target} (from checkpoint)", flush=True)
            entries.extend(kept)
            continue
        kept, kept_docs = [], set()
        ex = ThreadPoolExecutor(WORKERS)
        try:
            for start in range(0, len(candidates), WORKERS * 2):
                if len(kept) >= target:
                    break
                batch = candidates[start:start + WORKERS * 2]
                futures = [ex.submit(make_item, client, collection, stratum, c) for c in batch]
                for fut in futures:
                    try:
                        item, reason, model = fut.result(timeout=ITEM_TIMEOUT_S)
                    except FutureTimeout:
                        item, reason, model = None, "timeout", None
                    except Exception as exc:  # one bad API call must not sink the whole build
                        item, reason, model = None, f"error_{type(exc).__name__}", None
                    reasons[f"{stratum}:{reason}"] += 1
                    if model:
                        models.add(model)
                    if item and len(kept) < target and item["expected_document_id"] not in kept_docs:
                        kept.append(item)
                        kept_docs.add(item["expected_document_id"])
                        used_chunks.add(item["expected_chunk_id"])
        finally:
            ex.shutdown(wait=False, cancel_futures=True)  # never block on a stuck worker
        print(f"  {stratum}: kept {len(kept)}/{target}", flush=True)
        entries.extend(kept)
        done[stratum] = kept
        partial_path.write_text(json.dumps({
            "prompt_version": PROMPT_VERSION, "seed": SEED, "corpus_revision": revision,
            "strata": done, "filter_counts": dict(reasons), "models": sorted(models),
        }, ensure_ascii=False))

    for i, e in enumerate(entries):
        e["id"] = f"r{i:03d}"
    out = {
        "meta": {
            "item": "p3m3 #40", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "corpus_revision": revision, "seed": SEED, "generator_model": GEN_MODEL,
            "generator_model_snapshots": sorted(models), "prompt_version": PROMPT_VERSION,
            "targets": TARGETS, "max_words": MAX_WORDS, "filter_counts": dict(sorted(reasons.items())),
            "note": "Ground truth = source chunk/document. Chunk IDs are only valid for corpus_revision; "
                    "retrieval_eval.py warns if the corpus has changed since.",
        },
        "entries": entries,
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    partial_path.unlink(missing_ok=True)
    print(f"wrote {len(entries)} entries -> {args.out}", flush=True)
    print("filter counts:", dict(sorted(reasons.items())), flush=True)
    print("\nSpot-check sample (review these before trusting the set):", flush=True)
    for e in random.Random(1).sample(entries, min(20, len(entries))):
        print(f"  [{e['stratum']}] {e['question']}  ->  {e['expected_document_id']}", flush=True)


if __name__ == "__main__":
    main()
