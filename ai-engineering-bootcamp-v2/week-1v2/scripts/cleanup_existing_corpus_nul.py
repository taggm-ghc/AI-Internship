"""p3m3 item #2.e (D-N+4) -- retroactive NUL-byte cleanup for the existing
baseline corpus, which predates item #2.d's extraction-code fix. Repairs
already-extracted text in place; never touches the original source PDFs
(ingestion_quarantine/, internship.artifacts) or documents/chunks that
never had a NUL byte in the first place. Re-embeds only the chunks whose
text actually changes -- reuses embeddings for everything else."""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from openai import OpenAI
from sqlalchemy import text

from db import get_engine
from operational_store import vector_literal
from rag_ingest import embed_chunks
from rag_service import get_collection, get_document_collection, upsert_document_centroid


def clean(raw) -> str:
    return bytes(raw).decode("utf-8", errors="replace").replace("\x00", "�")


def main():
    engine = get_engine()
    client = OpenAI(timeout=30.0, max_retries=2)
    with engine.connect() as conn:
        docs = conn.execute(text("SELECT document_id, text_content FROM internship.documents")).fetchall()
    affected_docs = [d for d in docs if d.text_content is not None and b"\x00" in bytes(d.text_content)]
    print(f"{len(docs)} documents scanned, {len(affected_docs)} affected.", flush=True)

    total_chunks_reembedded = 0
    for doc in affected_docs:
        document_id = doc.document_id
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE internship.documents SET text_content=:t WHERE document_id=:id"),
                {"t": clean(doc.text_content).encode("utf-8"), "id": document_id},
            )
            chunks = conn.execute(
                text("SELECT id, content FROM internship.vectors WHERE collection_name='week2_rag_corpus' AND document_id=:id"),
                {"id": document_id},
            ).fetchall()
            affected_chunks = [c for c in chunks if c.content is not None and b"\x00" in bytes(c.content)]
            if affected_chunks:
                cleaned_texts = [clean(c.content) for c in affected_chunks]
                new_embeddings = embed_chunks(client, cleaned_texts)
                for chunk, cleaned_text, embedding in zip(affected_chunks, cleaned_texts, new_embeddings):
                    conn.execute(
                        text("UPDATE internship.vectors SET content=:c, embedding=CAST(:e AS vector) WHERE collection_name='week2_rag_corpus' AND id=:id"),
                        {"c": cleaned_text.encode("utf-8"), "e": vector_literal(embedding), "id": chunk.id},
                    )
                total_chunks_reembedded += len(affected_chunks)
        if affected_chunks:
            upsert_document_centroid(get_collection(), get_document_collection(), document_id)
        print(f"  cleaned {document_id}: {len(affected_chunks)} chunk(s) re-embedded", flush=True)

    print(f"Done. {len(affected_docs)} documents cleaned, {total_chunks_reembedded} chunks re-embedded.", flush=True)


if __name__ == "__main__":
    main()
