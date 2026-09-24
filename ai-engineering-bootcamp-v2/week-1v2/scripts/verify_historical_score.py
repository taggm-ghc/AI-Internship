"""p3m3 item #13 -- verifies rag_service.compute_historical_scores() against
real, seeded internship.events rows (not mocked), matching this project's
established verify_operational_store.py/golden_eval.py pattern: run for
real, check the actual output, clean up the test rows afterward. Two cases:
(1) a chunk with 4 past selections, 3 "supported" -> expects 0.75; (2) a
chunk with only 2 past selections -> expects None (MIN_HISTORY_SAMPLES=3
guard, not enough history to report a confident ratio)."""
import sys
from pathlib import Path
from uuid import uuid4

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from sqlalchemy import text

from db import get_engine
from operational_store import record_event
from rag_service import compute_historical_scores

CHUNK_ENOUGH_HISTORY = "verify-historical-score-test-chunk-a::0"
CHUNK_THIN_HISTORY = "verify-historical-score-test-chunk-b::0"


def seed(chunk_id: str, statuses: list[str]) -> list[str]:
    request_ids = []
    for status in statuses:
        request_id = str(uuid4())
        request_ids.append(request_id)
        record_event("retrieval", {
            "request_id": request_id, "question": "verify_historical_score.py test",
            "candidate_ids": [chunk_id], "distances": [0.5], "selected_ids": [chunk_id],
            "collection_revision": 0, "embedding_tokens": 0, "embedding_cost_usd": 0.0,
        })
        record_event("http_completed", {
            "request_id": request_id, "path": "/ask", "http_status": 200,
            "latency_ms": 0.0, "response": {"status": status}, "response_truncated": False,
            "error": None, "usage_known": True,
        }, event_id=request_id)
    return request_ids


def cleanup(request_ids: list[str]):
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM internship.events WHERE payload->>'request_id' = ANY(:ids)"),
            {"ids": request_ids},
        )


def main():
    request_ids = []
    try:
        request_ids += seed(CHUNK_ENOUGH_HISTORY, ["supported", "supported", "supported", "insufficient"])
        request_ids += seed(CHUNK_THIN_HISTORY, ["supported", "supported"])

        scores = compute_historical_scores([CHUNK_ENOUGH_HISTORY, CHUNK_THIN_HISTORY, "chunk-with-no-history::0"])
        print("computed scores:", scores)

        assert scores.get(CHUNK_ENOUGH_HISTORY) == 0.75, f"expected 0.75, got {scores.get(CHUNK_ENOUGH_HISTORY)}"
        assert scores.get(CHUNK_THIN_HISTORY) is None, f"expected None (below MIN_HISTORY_SAMPLES), got {scores.get(CHUNK_THIN_HISTORY)}"
        assert "chunk-with-no-history::0" not in scores or scores["chunk-with-no-history::0"] is None
        print("PASS: 4-selection/3-supported chunk -> 0.75; 2-selection chunk -> None (thin-history guard); no-history chunk -> absent/None")
    finally:
        cleanup(request_ids)
        print(f"cleaned up {len(request_ids)} seeded test events")


if __name__ == "__main__":
    main()
