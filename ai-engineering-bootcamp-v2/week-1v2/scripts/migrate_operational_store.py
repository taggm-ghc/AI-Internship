"""Explicit, restartable migration from preserved local inputs into PostgreSQL.

Run from any directory with the project virtualenv. Writes only internship schema
and enables vector. Never deletes source files or drops unrelated database state.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / '.env')
from sqlalchemy import text
from db import get_admin_engine
from operational_store import PgCollection, install_schema, put_artifact, record_event, create_vector_indexes


def migrate():
    import chromadb
    from pdf_extract import extract_pdf_text
    install_schema()
    engine = get_admin_engine()  # one-off migration runs as the admin account (item #47)
    source = chromadb.PersistentClient(path=str(BASE / 'chroma_store'))
    report = {'collections':{}, 'artifacts':0, 'documents':0}
    for name in ['week2_rag_corpus','week2_rag_documents']:
        old = source.get_collection(name)
        new = PgCollection(name)
        for offset in range(0,old.count(),250):
            data = old.get(limit=250, offset=offset, include=['documents','metadatas','embeddings'])
            new.upsert(**{k:data[k] for k in ['ids','documents','metadatas','embeddings']})
        assert new.count() == old.count(), f'Unexpected target count in {name}; inspect without deleting data'
        report['collections'][name] = new.count()
        print(f'{name}: {new.count()} records migrated',flush=True)
    provenance = json.loads((BASE/'ingestion_quarantine/new_docs_provenance.json').read_text())['documents']
    by_id = {r['document_id']:r for r in provenance}
    collection = PgCollection('week2_rag_corpus')
    for pdf in sorted((BASE/'ingestion_quarantine/pdfs').glob('*.pdf')):
        collection.save_document(pdf.stem,extract_pdf_text(pdf),{'source':pdf.name},by_id[pdf.stem])
        report['documents'] += 1
    # All retained acquisition records/payloads, including dropped sources, are
    # recovery artifacts. Dropped PDFs do not become active vectors/documents.
    files = [p for p in (BASE/'ingestion_quarantine').rglob('*') if p.is_file()]
    files += [BASE/'config/golden_eval_set.json']
    availability = BASE/'.model-availability.json'
    if availability.exists(): files.append(availability)
    for index,path in enumerate(sorted(files)):
        put_artifact(str(path.relative_to(BASE)),path.read_bytes())
        report['artifacts'] += 1
        if index % 100 == 0: print(f'Artifacts: {index+1}/{len(files)}',flush=True)
    if availability.exists():
        observations = json.loads(availability.read_text())
        with engine.begin() as conn:
            for row in observations:
                conn.execute(text('''INSERT INTO internship.provider_observations(provider,model,observed_at)
                    VALUES (:provider,:model,CAST(:observed_at AS timestamptz)) ON CONFLICT DO NOTHING'''),row)
    print('Building vector indexes',flush=True)
    create_vector_indexes()
    report['source_manifest_sha256'] = hashlib.sha256((BASE/'ingestion_quarantine/new_docs_provenance.json').read_bytes()).hexdigest()
    record_event('migration', report,event_id='migration:local-to-postgres:'+report['source_manifest_sha256'])
    print(json.dumps(report,indent=2),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',required=True,help='Enable pgvector, install schema and migrate existing local operational data')
    parser.parse_args()
    try:
        migrate()
    except Exception as exc:
        print(f'Migration failed: {type(exc).__name__} (connection details withheld)',file=sys.stderr)
        import traceback
        for frame in traceback.extract_tb(exc.__traceback__):
            print(f'{Path(frame.filename).name}:{frame.lineno} in {frame.name}',file=sys.stderr)
        raise SystemExit(1)
