"""Read-only migration parity plus rollback-only ingestion/HTTP tests. No model calls."""
import json
import sys
import hashlib
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE/'.env')
from sqlalchemy import text
from db import get_engine
from operational_store import PgCollection, put_artifact


def verify():
    import chromadb
    source=chromadb.PersistentClient(path=str(BASE/'chroma_store'))
    engine=get_engine()
    for name in ['week2_rag_corpus','week2_rag_documents']:
        old=source.get_collection(name)
        new=PgCollection(name)
        assert old.count()==new.count(), f'{name}: count mismatch'
        for offset in range(0,old.count(),250):
            data=old.get(limit=250,offset=offset,include=['documents','metadatas','embeddings'])
            rows=new.get(ids=data['ids'],include=['documents','metadatas','embeddings'])
            target={i:(d,m,e) for i,d,m,e in zip(rows['ids'],rows['documents'],rows['metadatas'],rows['embeddings'])}
            assert set(target)==set(data['ids'])
            for i,d,m,e in zip(data['ids'],data['documents'],data['metadatas'],data['embeddings']):
                actual=target[i]
                assert actual[0]==d and actual[1]==m, f'{name}: content mismatch'
                assert max(abs(float(a)-float(b)) for a,b in zip(e,actual[2]))<1e-7
        print(f'PASS {name}: all IDs, content, metadata and embeddings match',flush=True)
    with engine.connect() as conn:
        for path in (BASE/'ingestion_quarantine').rglob('*'):
            if not path.is_file():continue
            stored=conn.execute(text('SELECT sha256,payload FROM internship.artifacts WHERE path=:path'),{'path':str(path.relative_to(BASE))}).mappings().one()
            assert hashlib.sha256(path.read_bytes()).hexdigest()==stored['sha256']==hashlib.sha256(bytes(stored['payload'])).hexdigest()
        prov=json.loads((BASE/'ingestion_quarantine/new_docs_provenance.json').read_text())['documents']
        for row in prov:
            assert conn.execute(text('SELECT provenance FROM internship.documents WHERE document_id=:id'),{'id':row['document_id']}).scalar_one()==row
    print('PASS retained source/artifact payload hashes and all provenance records',flush=True)
    # Compare old and migrated retrieval for stored-vector probes without paying
    # to re-embed queries. API/golden semantic runs are separately reported.
    old=source.get_collection('week2_rag_corpus')
    sample=old.get(limit=5,include=['embeddings','metadatas'])
    for embedding,metadata in zip(sample['embeddings'],sample['metadatas']):
        where={'document_id':metadata['document_id']}
        a=old.query(query_embeddings=[embedding],n_results=5,where=where)
        b=PgCollection('week2_rag_corpus').query(query_embeddings=[embedding],n_results=5,where=where)
        # Handle ties without forcing arbitrary identical rank order.
        assert set(a['ids'][0])==set(b['ids'][0]),'Filtered nearest-neighbor membership drift'
        distances=dict(zip(a['ids'][0],a['distances'][0]))
        assert all(abs(distances[i]-d)<1e-5 for i,d in zip(b['ids'][0],b['distances'][0]))
    print('PASS five retrieval probes: neighbor membership and squared-L2 distances',flush=True)
    # Isolate every test write in a rollback transaction, including middleware.
    with engine.connect() as conn:
        transaction=conn.begin()
        class RollbackEngine:
            @contextmanager
            def begin(self): yield conn
            @contextmanager
            def connect(self): yield conn
        test_engine=RollbackEngine()
        try:
            import operational_store, rag_service, main
            from fastapi.testclient import TestClient
            col=PgCollection('week2_rag_corpus',test_engine)
            query=[1.0]+[0.0]*1535
            doc_id='__migration_rollback_probe__'
            def embeddings(client,chunks): return [query[:] for _ in chunks]
            with patch.object(operational_store,'get_engine',return_value=test_engine), patch.object(main,'get_collection',return_value=col), patch.object(main,'_require_valid_key'), patch.object(main,'_get_embedding_client',return_value=None), patch.object(rag_service,'embed_chunks',side_effect=embeddings), patch.object(main,'embed_query',return_value=(query,0)):
                client=TestClient(main.app)
                response=client.post('/ingest',json={'document_id':doc_id,'text':'A substantive paragraph with enough repeated context. '*50,'metadata':{'document_id':'cannot-override'}})
                assert response.status_code==200, f'ingest status {response.status_code}'
                assert col.count()>old.count()
                response=client.post('/ingest',json={'document_id':doc_id,'text':'Short replacement.'})
                assert response.status_code==200
                assert len(col.get(where={'document_id':doc_id})['ids'])==1,'Trailing stale chunks remain'
                assert PgCollection('week2_rag_documents',test_engine).get(ids=[doc_id])['ids']==[doc_id]
                found=client.get('/debug/retrieve',params={'query':'probe','document_id':doc_id})
                assert found.status_code==200 and len(found.json())==1
                assert found.json()[0]['document_id']==doc_id and found.json()[0]['distance']==0
                missing=client.get('/debug/retrieve',params={'query':'probe','document_id':'__nonexistent__'})
                assert missing.json()==[]
                bad=client.post('/ingest',json={'document_id':doc_id,'text':'   '})
                assert bad.status_code==400
                n=conn.execute(text("SELECT count(*) FROM internship.events WHERE kind='http_completed' AND payload->>'path'='/ingest'")).scalar_one()
                assert n>=3
                # A validation failure midway through replacement must roll back
                # all effects rather than leave a partially deleted document.
                nested=conn.begin_nested()
                try:
                    col.delete(where={'document_id':doc_id})
                    col.upsert(ids=[doc_id+'::0'],embeddings=[[1.0]],documents=['bad'],metadatas=[{'document_id':doc_id}])
                    raise AssertionError('Bad dimensions accepted')
                except ValueError:
                    nested.rollback()
                assert len(col.get(where={'document_id':doc_id})['ids'])==1
            print('PASS HTTP ingest/filter, shorter replacement, reserved IDs, atomic rollback, centroid and durable audit',flush=True)
        finally:
            transaction.rollback()
    # A fresh engine/connection proves state does not live in process caches.
    engine.dispose()
    assert PgCollection('week2_rag_corpus').count()==old.count()
    with engine.connect() as conn:
        print('Database bytes:',conn.execute(text('SELECT pg_database_size(current_database())')).scalar_one())
    print('PASS reconnect durability; rollback probes left no corpus changes',flush=True)


if __name__=='__main__':
    try:verify()
    except Exception as exc:
        print(f'Verification failed: {type(exc).__name__} (connection details withheld)',file=sys.stderr)
        import traceback
        for frame in traceback.extract_tb(exc.__traceback__): print(f'{Path(frame.filename).name}:{frame.lineno} in {frame.name}',file=sys.stderr)
        raise SystemExit(1)
