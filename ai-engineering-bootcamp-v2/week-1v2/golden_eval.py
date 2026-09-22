#!/usr/bin/env python3
"""Golden-set RAG eval: measures retrieval and generation separately against
real, live-verified behavior on the actual 250-document corpus (expanded
from 50 on 2026-09-19), per deliverables-outcomes.md's Session 2 core
deliverable ("Golden-set eval... Measure retrieval + generation separately")
and week2-priority-checklist.md's Tier 4a.

The golden set itself lives in config/golden_eval_set.json (externalized
2026-09-19 -- see load_golden_set() below and
p3m3/golden-eval-remediation-design.md), not as a literal in this file.
Every (question, expected_document_id, expected_status) entry was verified
live against GET /debug/retrieve and POST /ask before being written there --
not guessed from filenames. Real OpenAI + free-tier provider spend, same as
test_all_stages.py, not a zero-cost check. A pre-flight check (see
preflight_check() below) runs before any of that spend happens, refusing to
score a golden set whose expected_document_id references have gone stale
against the live corpus.

Two scores, kept genuinely separate rather than one blended pass/fail:

  retrieval:  for "supported" questions, did the expected document actually
              come back in the top-5 (a real Recall@5 style hit)? For
              "insufficient"/"not_applicable" questions (no single expected
              document exists), did the RAG_RELEVANCE_THRESHOLD gate land on
              the right side -- i.e. did retrieval correctly signal "there's
              something plausibly relevant" vs. "there's nothing relevant"?

  generation: did /ask's reported status match what's expected, and for
              supported answers, are the citations actually drawn from the
              expected document (not just non-empty) -- the citation-
              precision property added 2026-09-18 alongside this eval.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

WORKDIR = Path(__file__).resolve().parent
GOLDEN_SET_PATH = WORKDIR / "config" / "golden_eval_set.json"

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "in",
    "on", "for", "and", "or", "it", "its", "that", "this", "as", "at", "by",
    "with", "from", "not", "no", "does", "do", "did", "has", "have", "had",
}


def load_golden_set(path: Path = GOLDEN_SET_PATH) -> list[dict]:
    """Externalized 2026-09-19 (direct instruction: "there shouldn't be
    anything hardcoded in any script") -- was a Python literal in this file,
    the same anti-pattern this project already fixed once for the model
    list (config/model-selection.json). Loaded fresh on every run; adding,
    correcting, or removing a golden-set entry is now a data change to
    config/golden_eval_set.json, not a code edit here. Matches
    pricing_config.py's load_model_pricing()/load_model_selection() pattern:
    config/*.json + a typed loader, not a value embedded in a .py file. See
    p3m3/golden-eval-remediation-design.md (Gap 7) for the fuller design."""
    data = json.loads(path.read_text())
    entries = data["entries"] if isinstance(data, dict) else data
    return entries


def preflight_check(golden_set: list[dict], live_document_ids: set[str]) -> list[str]:
    """2026-09-19 (golden_eval.py remediation, Gap 1) -- corpus churn has
    already silently broken this eval twice (the 2.5MB-cap restructuring,
    then the 700KB-cap expansion): a golden question's expected_document_id
    can stop existing in the live corpus without the eval itself ever
    noticing, since a missing document just becomes a retrieval miss rather
    than a distinguishable "this question is now unanswerable by
    construction" signal. Fails fast and loud instead -- refuses to run a
    scored eval against a golden set with dangling document references,
    rather than silently reporting a real-looking but meaningless score.
    Returns the list of broken questions (empty if none) so a caller can
    choose to raise or just report, rather than baking one behavior in."""
    broken = []
    for case in golden_set:
        expected = case.get("expected_document_id")
        if expected and expected not in live_document_ids:
            broken.append(case["question"])
    return broken


_spend: list[tuple[str, float]] = []


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [str(WORKDIR / ".venv/bin/uvicorn"), "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=WORKDIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_up(base: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/docs", timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def content_overlap_check(answer_text: str, cited_texts: list[str]) -> tuple[bool, float]:
    """2026-09-21 -- closes the gap flagged in
    p3m3/module-2.6-rag-evals.md: citation_ok below only checks that the
    expected DOCUMENT is among the cited chunk IDs, never whether the
    generated answer's actual content is supported by the cited passage
    TEXT. This is a lightweight heuristic, explicitly not a full
    entailment/faithfulness judge: fraction of the answer's non-stopword
    words that also appear somewhere in the combined cited passage text.
    Threshold (0.2) chosen loosely, not tuned against this golden set --
    called out the same way RAG_RELEVANCE_THRESHOLD's own calibration
    comment calls out its own looseness, rather than presented as exact."""
    answer_words = {w for w in re.findall(r"[a-z0-9]+", answer_text.lower()) if w not in _STOPWORDS and len(w) > 2}
    if not answer_words:
        return True, 1.0
    passage_words = {w for w in re.findall(r"[a-z0-9]+", " ".join(cited_texts).lower())}
    overlap = len(answer_words & passage_words) / len(answer_words)
    return overlap >= 0.2, overlap


def run_one(base: str, case: dict) -> dict:
    retrieve = httpx.get(
        f"{base}/debug/retrieve", params={"query": case["question"], "top_k": 5}, timeout=30.0
    ).json()
    retrieved_doc_ids = [r["document_id"] for r in retrieve]

    # model pinned to gpt-4.1-nano rather than left on the default
    # free-tier fallback chain: the chain can route a given question to
    # different providers/models across runs, which showed up as real
    # supported/insufficient flips on borderline questions during this
    # eval's own development (verified 2026-09-18) -- a confound this eval
    # should isolate out, not silently average over. Small real OpenAI
    # spend per question, same tradeoff test_all_stages.py already makes.
    ask = httpx.post(
        f"{base}/ask", json={"question": case["question"], "model": "gpt-4.1-nano"}, timeout=60.0
    ).json()
    cost = ask.get("cost_usd")
    if isinstance(cost, (int, float)):
        _spend.append((case["question"][:40], cost))

    if case["expected_document_id"] is not None:
        retrieval_ok = case["expected_document_id"] in retrieved_doc_ids
    else:
        # No single expected document -- score the gate itself: did the
        # top-1 distance land on the side of the threshold that matches
        # what we independently verified live before writing this case.
        top_distance = retrieve[0]["distance"] if retrieve else float("inf")
        gate_expects_relevant = case["expected_status"] == "insufficient"
        retrieval_ok = (top_distance <= 1.2) == gate_expects_relevant

    status_ok = ask.get("status") == case["expected_status"]
    citations = ask.get("citations", [])
    if case["expected_status"] == "supported":
        # The expected document must be *among* the citations, not the only
        # one -- several of this corpus's real questions are legitimately
        # answerable from more than one document (e.g. graph-RAG and
        # agent-safety topics each span multiple papers here), and the
        # citation-precision fix correctly reflects that rather than
        # collapsing it to a single source. Requiring exact-match here would
        # be testing this eval's own oversimplified assumption, not a real
        # defect.
        citation_ids = {c.split("::", 1)[0] for c in citations}
        citation_ok = len(citations) > 0 and case["expected_document_id"] in citation_ids
    else:
        citation_ok = citations == []
    generation_ok = status_ok and citation_ok

    # Content-overlap heuristic (2026-09-21, closes the "citation-ID-only"
    # gap): a citation string can be a real chunk_id while still not being
    # what the answer text actually says. Not folded into generation_ok --
    # this is a new, separately-reported axis (does the answer's content
    # match what's in the cited passage), not a redefinition of the
    # existing citation-membership check. top_k=15 matches production's
    # OVERFETCH_K so every chunk /ask could possibly have cited is covered
    # by this one extra lookup, not just the top-5 already fetched above.
    content_overlap_ok, content_overlap_ratio = None, None
    if case["expected_status"] == "supported" and citations:
        overfetch = httpx.get(
            f"{base}/debug/retrieve", params={"query": case["question"], "top_k": 15}, timeout=30.0
        ).json()
        text_by_id = {r["chunk_id"]: r["text"] for r in overfetch}
        cited_texts = [text_by_id[c] for c in citations if c in text_by_id]
        answer_text = ask.get("answer", {}).get("answer", "")
        if cited_texts:
            content_overlap_ok, content_overlap_ratio = content_overlap_check(answer_text, cited_texts)

    return {
        "question": case["question"],
        "retrieval_ok": retrieval_ok,
        "status_ok": status_ok,
        "citation_ok": citation_ok,
        "generation_ok": generation_ok,
        "content_overlap_ok": content_overlap_ok,
        "content_overlap_ratio": content_overlap_ratio,
        "actual_status": ask.get("status"),
        "actual_citations": citations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("GOLDEN_EVAL_BASE_URL"),
        help=(
            "Run against an already-running target (e.g. the deployed Render URL) instead of "
            "spinning up a local uvicorn subprocess. Also settable via GOLDEN_EVAL_BASE_URL. "
            "Default (unset): unchanged local-subprocess behavior."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    golden_set = load_golden_set()

    # Pre-flight (2026-09-19, Gap 1) -- checked before the server even
    # starts, so a broken golden set fails immediately and cheaply rather
    # than spending real API calls on a run whose score would be
    # meaningless. Queries the collection directly (no HTTP round trip
    # needed, no server required yet) -- this is exactly the check that
    # would have caught both real corpus-churn breaks this project already
    # had (the 2.5MB-cap restructuring, then the 700KB-cap expansion) the
    # moment they happened, instead of a manual grep afterward.
    import rag_service

    live_document_ids = {
        m["document_id"] for m in rag_service.get_collection().get(include=["metadatas"])["metadatas"]
    }
    broken = preflight_check(golden_set, live_document_ids)
    if broken:
        print(f"=== PRE-FLIGHT FAIL — {len(broken)} golden question(s) reference documents no longer in the corpus ===")
        for q in broken:
            print(f"  {q}")
        print("Refusing to run a scored eval against a golden set with dangling document references.")
        print("Fix config/golden_eval_set.json before re-running (see ingestion_quarantine/dropped/ for what left the corpus).")
        return 1

    remote = args.base_url is not None
    if remote:
        base = args.base_url.rstrip("/")
        proc = None
    else:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        proc = start_server(port)

    results: list[dict] = []
    try:
        if not remote and not wait_up(base):
            print(f"\n=== FAIL — server did not start on {base} ===")
            return 1
        print(f"Target: {base} ({'remote' if remote else 'local subprocess'})")
        for case in golden_set:
            print(f"\n=== {case['question']} ===")
            result = run_one(base, case)
            results.append(result)
            print(f"  [{'PASS' if result['retrieval_ok'] else 'FAIL'}] retrieval")
            print(
                f"  [{'PASS' if result['generation_ok'] else 'FAIL'}] generation "
                f"(status={result['actual_status']}, expected={case['expected_status']}, "
                f"citations={result['actual_citations']})"
            )
            if result["content_overlap_ok"] is not None:
                print(
                    f"  [{'PASS' if result['content_overlap_ok'] else 'FAIL'}] content overlap (heuristic, "
                    f"ratio={result['content_overlap_ratio']:.2f})"
                )
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)

    retrieval_passed = sum(1 for r in results if r["retrieval_ok"])
    generation_passed = sum(1 for r in results if r["generation_ok"])
    overlap_checked = [r for r in results if r["content_overlap_ok"] is not None]
    overlap_passed = sum(1 for r in overlap_checked if r["content_overlap_ok"])
    print("\n" + "=" * 40)
    print("SUMMARY")
    print(f"  retrieval:  {retrieval_passed}/{len(results)} passed")
    print(f"  generation: {generation_passed}/{len(results)} passed")
    if overlap_checked:
        print(f"  content overlap (heuristic, supported cases only): {overlap_passed}/{len(overlap_checked)} passed")

    if _spend:
        print("\nREAL SPEND THIS RUN:")
        total = 0.0
        for label, cost in _spend:
            print(f"  ${cost:.6f}  {label}")
            total += cost
        print(f"  ${total:.6f}  TOTAL")

    from operational_store import record_event
    import hashlib
    record_event("golden_eval", {"target": base, "dataset_sha256": hashlib.sha256(GOLDEN_SET_PATH.read_bytes()).hexdigest(),
        "cases": golden_set, "results": results, "retrieval_passed": retrieval_passed,
        "generation_passed": generation_passed, "content_overlap_passed": overlap_passed,
        "content_overlap_checked": len(overlap_checked), "spend": _spend,
        "collection_revision": rag_service.get_collection().revision(), "model": "gpt-4.1-nano"})
    return 0 if retrieval_passed == len(results) and generation_passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
