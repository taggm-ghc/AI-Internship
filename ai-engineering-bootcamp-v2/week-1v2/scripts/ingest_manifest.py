"""Generic, bounded source ingest for the `ingester` agent (R1 direction 2026-10-02:
research agents spawn ingest sub-agents; they do NOT go through POST
/ingest/versions/accept, which stays key-gated for humans and external actors).

Manifest: JSON list of {"document_id", "text_file", "provenance": {...}, "metadata": {...}}.
Default is DRY RUN: validates only, no DB, no network, no writes. `--write` does a
real ingest (the local DB is production) via POST /ingest in-process as the local
account, with INGEST_API_KEY deliberately NOT sent (a key never exempts content from
a filter). Pass `--write` only with the user's approval for that run.

Gates, applied to everyone (a failure = the item is reported and skipped, never worked around):
  - provenance has source_url, author, published_at, fetched_at, provenance_type, license
  - licence via the shared intake/licence.py detector (policy in config/intake_licence.json): ND, no-educational-use,
    strictly-for-fee -> REJECT; plain NC/NC-SA accepted; none/unclear/unrecognised -> HOLD for human review (#63)
  - verifier_status must be VERIFIED and read_status not SUMMARY/TITLE-ONLY/FETCH-FAILED
  - text non-empty; content_sha256 recorded by the API; exact duplicates are not re-ingested
    (the API returns status "duplicate"); injection phrasing is ingested as non_actionable.
Usage: .venv/bin/python scripts/ingest_manifest.py manifest.json [--write] [--max-docs N]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from intake import licence as licence_gate  # shared detector; policy lives in config/intake_licence.json

REQUIRED = ("source_url", "author", "published_at", "fetched_at", "provenance_type", "license")
BAD_READ = {"SUMMARY", "TITLE-ONLY", "FETCH-FAILED"}


def check_item(item: dict, text: str) -> tuple[str, str]:
    """Returns (verdict, reason); verdict is OK, REJECT or HOLD. Pure, offline."""
    prov = item.get("provenance") or {}
    missing = [k for k in REQUIRED if k != "license" and not prov.get(k)]
    if missing:
        return "REJECT", f"missing provenance fields: {', '.join(missing)}"
    if prov.get("verifier_status") != "VERIFIED":
        return "REJECT", f"verifier_status is {prov.get('verifier_status')!r}, not VERIFIED"
    if prov.get("read_status") in BAD_READ or not prov.get("read_status"):
        return "REJECT", f"read_status {prov.get('read_status')!r} is not a verbatim read"
    lic = str(prov.get("license") or "").strip()
    if not lic:
        return "HOLD", "no declared licence: held for human review (#63)"
    rep = licence_gate.detect("", prov.get("source_url"), {"name": lic})
    if rep["verdict_hint"] == "reject":
        return "REJECT", f"licence {lic!r} rejected by shared licence gate (#63): {'; '.join(rep['reasons'])}"
    if rep["verdict_hint"] != "ok":
        return "HOLD", f"licence {lic!r} unclear or conflicting, held for human review (#63): {'; '.join(rep['reasons'])}"
    if not text.strip():
        return "REJECT", "empty text"
    return "OK", "passes local gates"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--write", action="store_true", help="real ingest (production DB); needs the user's approval")
    ap.add_argument("--max-docs", type=int, default=5)
    args = ap.parse_args(argv)
    items = json.loads(Path(args.manifest).read_text())
    if len(items) > args.max_docs:
        print(f"STOP: {len(items)} items exceeds --max-docs {args.max_docs}; nothing processed")
        return 2
    client = None
    if args.write:
        import os
        os.chdir(Path(__file__).resolve().parent.parent)
        sys.path.insert(0, str(Path.cwd()))
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
        os.environ.pop("INGEST_API_KEY", None)  # never sent
        import main as app_main
        from fastapi.testclient import TestClient
        client = TestClient(app_main.app)
    rc = 0
    for item in items:
        text = Path(item["text_file"]).read_text()
        verdict, reason = check_item(item, text)
        print(f"{verdict} {item['document_id']}: {reason}")
        if verdict != "OK":
            rc = 1
            continue
        if client is None:
            print("  dry run: not ingested")
            continue
        r = client.post("/ingest", json={"document_id": item["document_id"], "text": text,
                                         "metadata": item.get("metadata"), "provenance": item["provenance"]})
        body = r.json()
        print(f"  HTTP {r.status_code}: {str(body)[:300]}")
        if r.status_code != 200:
            print("STOP: first failure ends the run (reported, not worked around)")
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
