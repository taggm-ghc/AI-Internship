"""Retroactive adversarial-content scan over every existing document, p3m3
D-N+2 (item #30) third pass. rag_service.detect_adversarial_content only
ever ran on new POST /ingest calls -- this closes the gap for the 258
documents already in the corpus before that check existed, which were
never scanned by anything. Read + one targeted UPDATE per flagged/rescanned
document (provenance jsonb merge, text_content untouched); no model calls,
no re-embedding. Rerunnable -- safe to invoke after any bulk ingest."""
import datetime
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from sqlalchemy import text

from db import get_engine
from rag_service import detect_adversarial_content


def scan():
    with get_engine().begin() as conn:
        rows = conn.execute(text("SELECT document_id, text_content FROM internship.documents")).fetchall()
        flagged = []
        for row in rows:
            raw = row.text_content
            content = bytes(raw).decode("utf-8", errors="replace") if raw is not None else ""
            flags = detect_adversarial_content(content)
            scan_record = {
                "content_scan": {"flags": flags, "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            }
            conn.execute(
                text("UPDATE internship.documents SET provenance = provenance || CAST(:scan AS jsonb) WHERE document_id = :id"),
                {"scan": json.dumps(scan_record), "id": row.document_id},
            )
            if flags:
                flagged.append((row.document_id, flags))
        print(f"Scanned {len(rows)} documents, {len(flagged)} flagged, {len(rows) - len(flagged)} clean.", flush=True)
        for document_id, flags in flagged:
            print(f"  FLAGGED {document_id}: {flags}", flush=True)
        print(
            "Flags are recorded, not enforced -- this script never deletes or blocks a document; "
            "review flagged entries by hand (see item #30's write-up for the 4 known false positives "
            "confirmed 2026-09-23: legitimate security-research papers discussing prompt injection as "
            "their subject, not attacks).",
            flush=True,
        )


if __name__ == "__main__":
    scan()
