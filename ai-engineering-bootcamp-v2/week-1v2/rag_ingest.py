"""Re-embed canonical PostgreSQL document text into PostgreSQL vectors.

Run explicitly (incurs embedding calls):

    python rag_ingest.py
"""

import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import OpenAI

from pdf_extract import extract_pdf_text

load_dotenv()

THIS_DIR = Path(__file__).resolve().parent
PDF_DIR = THIS_DIR / "ingestion_quarantine" / "pdfs"
CHROMA_PATH = THIS_DIR / "chroma_store"
COLLECTION_NAME = "week2_rag_corpus"
EMBEDDING_MODEL = "text-embedding-3-small"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_text(text)


def embed_chunks(client: OpenAI, chunks: list[str]) -> list[list[float]]:
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=chunks)
    return [item.embedding for item in response.data]


def ingest_all() -> dict:
    """Explicit re-embedding from canonical PostgreSQL document text.

    Initial local-file/Chroma migration uses scripts/migrate_operational_store.py
    and does not call an embedding API. This operation intentionally does.
    """
    from sqlalchemy import text
    from db import get_engine
    from rag_service import get_collection, get_document_collection, upsert_chunks
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT document_id,text_content,metadata FROM internship.documents ORDER BY document_id")).mappings().all()
    client = OpenAI(timeout=30.0,max_retries=3)
    total_chunks=0
    for row in rows:
        total_chunks += upsert_chunks(client,get_collection(),row['document_id'],bytes(row['text_content']).decode('utf-8') if row['text_content'] is not None else '',row['metadata'])
    return {'documents':len(rows),'total_chunks':total_chunks,'collection_count':get_collection().count(),
            'document_collection_count':get_document_collection().count()}


if __name__ == '__main__':
    print(ingest_all())
