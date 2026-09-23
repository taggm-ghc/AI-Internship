"""PostgreSQL operational state. Schema installation is explicit, never on import.

A small collection adapter preserves the existing retrieval contract during the
Chroma migration. Distances remain squared L2; pgvector's <-> is unsquared L2.
All SQL values are bound parameters. Local Chroma is only a migration input.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from db import get_engine

DIMENSIONS = 1536


def vector_literal(values):
    values = [float(v) for v in values]
    if len(values) != DIMENSIONS or not all(math.isfinite(v) for v in values):
        raise ValueError(f"Expected {DIMENSIONS} finite embedding values")
    return json.dumps(values, allow_nan=False)


def install_schema(engine=None):
    engine = engine or get_engine()
    sql = (Path(__file__).parent / 'migrations/001_operational_store.sql').read_text()
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)


def record_event(kind, payload, *, event_id=None, conn=None):
    event_id = event_id or str(uuid4())
    raw = json.dumps(payload, allow_nan=False)
    # JSONB cannot encode NUL. Keep the exact canonical JSON bytes and a
    # queryable preview with replacement characters for that uncommon case.
    params = {'id': event_id, 'kind': kind, 'payload': raw.replace('\\u0000','\\ufffd'), 'raw': raw.encode('utf-8')}
    stmt = text('INSERT INTO internship.events(id,kind,payload,payload_raw) VALUES (:id,:kind,CAST(:payload AS jsonb),:raw) ON CONFLICT(id) DO NOTHING')
    if conn is not None:
        conn.execute(stmt, params)
    else:
        with get_engine().begin() as connection:
            connection.execute(stmt, params)
    return event_id


def query_events(kind=None, limit=200):
    """Read-only counterpart to record_event, added 2026-09-22 for the
    observability dashboard (GET /debug/events) — record_event has been
    write-only since this table existed; nothing previously read it back
    except raw SQL. Most-recent-first, optionally filtered to one kind
    (http_started, http_completed, ingest, retrieval, retrieval_error).
    limit is the caller's responsibility to bound (main.py caps it) —
    this function trusts what it's given, same as the rest of this
    module's query-side functions."""
    stmt = text(
        'SELECT id, kind, payload, created_at FROM internship.events'
        + (' WHERE kind = :kind' if kind else '')
        + ' ORDER BY created_at DESC LIMIT :limit'
    )
    params = {'limit': limit} | ({'kind': kind} if kind else {})
    with get_engine().connect() as conn:
        rows = conn.execute(stmt, params).fetchall()
    return [
        {'id': r.id, 'kind': r.kind, 'payload': r.payload, 'created_at': r.created_at.isoformat()}
        for r in rows
    ]


def corpus_summary(sample_size=10):
    """Document count + a random sample of titles, added 2026-09-22 (p3m3
    permanent item #20) so the Streamlit UI can hint what the corpus
    actually covers before a user asks something it was never going to
    answer -- the gap found in #19 (a genuinely off-topic question gets a
    confidently fabricated answer, not a visible refusal) is exactly the
    failure mode this is meant to help a user avoid triggering. Falls back
    to the document_id when provenance.title is missing (covers any
    document ingested before this project's own provenance-parity fix,
    #15) rather than omitting it."""
    with get_engine().connect() as conn:
        total = conn.execute(text('SELECT COUNT(*) FROM internship.documents')).scalar()
        rows = conn.execute(
            text(
                "SELECT document_id, provenance->>'title' AS title FROM internship.documents "
                "ORDER BY random() LIMIT :n"
            ),
            {'n': sample_size},
        ).fetchall()
    return {
        'document_count': total,
        'sample_titles': [r.title or r.document_id for r in rows],
    }


def document_exists(document_id):
    """p3m3 D-N+2 (item #30) -- backs main.py's overwrite guard: an
    unauthenticated /ingest caller may create a new document_id but not
    silently replace an existing one (including the baseline corpus),
    closing the single-ID-collision corpus-poisoning vector OWASP's RAG
    Security Cheat Sheet flags. Indexed primary-key lookup, same
    get_engine().connect() pattern as corpus_summary above."""
    with get_engine().connect() as conn:
        return conn.execute(
            text('SELECT 1 FROM internship.documents WHERE document_id=:id'), {'id': document_id}
        ).first() is not None


def put_artifact(path, payload, conn=None):
    params = {'path': path, 'sha': hashlib.sha256(payload).hexdigest(), 'payload': payload}
    stmt = text('''INSERT INTO internship.artifacts(path,sha256,payload) VALUES (:path,:sha,:payload)
        ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,payload=excluded.payload,updated_at=now()
        WHERE internship.artifacts.sha256 IS DISTINCT FROM excluded.sha256''')
    if conn is not None:
        conn.execute(stmt, params)
    else:
        with get_engine().begin() as connection:
            connection.execute(stmt, params)


class PgCollection:
    def __init__(self, name, engine=None, conn=None):
        self.name = name
        self.engine = engine or get_engine()
        self.conn = conn

    @contextmanager
    def connection(self):
        if self.conn is not None:
            yield self.conn
        else:
            with self.engine.begin() as conn:
                yield conn

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            yield PgCollection(self.name, self.engine, conn)

    def peer(self, name):
        return PgCollection(name, self.engine, self.conn)

    def _filter(self, ids=None, where=None):
        params = {'collection': self.name}
        clauses = ['collection_name=:collection']
        if ids is not None:
            clauses.append('id = ANY(:ids)')
            params['ids'] = list(ids)
        if where is not None:
            # Current API supports equality filters (document_id). Reject unsupported
            # operators rather than silently changing the meaning of a Chroma filter.
            if not isinstance(where, dict) or any(k.startswith('$') or isinstance(v, (dict, list)) for k,v in where.items()):
                raise ValueError('PostgreSQL metadata filters support scalar equality only')
            clauses.append('metadata @> CAST(:metadata AS jsonb)')
            params['metadata'] = json.dumps(where)
        return ' AND '.join(clauses), params

    def count(self):
        with self.connection() as conn:
            return conn.execute(text('SELECT count(*) FROM internship.vectors WHERE collection_name=:name'), {'name':self.name}).scalar_one()

    def revision(self):
        with self.connection() as conn:
            return conn.execute(text('SELECT revision FROM internship.collections WHERE name=:name'), {'name':self.name}).scalar_one_or_none() or 0

    def get(self, ids=None, where=None, include=None, limit=None, offset=0):
        include = ['documents','metadatas'] if include is None else include
        clause, params = self._filter(ids, where)
        columns = 'id'
        columns += ',content' if 'documents' in include else ''
        columns += ',metadata' if 'metadatas' in include else ''
        columns += ',embedding::text AS embedding' if 'embeddings' in include else ''
        params.update(limit=limit, offset=offset)
        with self.connection() as conn:
            rows = conn.execute(text(f'SELECT {columns} FROM internship.vectors WHERE {clause} ORDER BY id LIMIT :limit OFFSET :offset'), params).mappings().all()
        result = {'ids': [r['id'] for r in rows]}
        for key,col in [('documents','content'),('metadatas','metadata'),('embeddings','embedding')]:
            result[key] = ([json.loads(r[col]) if key == 'embeddings' else bytes(r[col]).decode('utf-8') if key == 'documents' else r[col] for r in rows] if key in include else None)
        return result

    def upsert(self, ids, embeddings, documents, metadatas):
        if not (len(ids)==len(embeddings)==len(documents)==len(metadatas)) or len(set(ids)) != len(ids):
            raise ValueError('Collection values must have equal lengths and unique IDs')
        rows = [{'collection':self.name,'id':i,'doc_id':m['document_id'],'content':d.encode('utf-8'),'meta':json.dumps(m), 'embedding':vector_literal(e)} for i,e,d,m in zip(ids,embeddings,documents,metadatas)]
        if not rows:
            return
        with self.connection() as conn:
            conn.execute(text('INSERT INTO internship.collections(name) VALUES (:name) ON CONFLICT DO NOTHING'), {'name':self.name})
            from psycopg2.extras import execute_values
            values = [(r['collection'],r['id'],r['doc_id'],r['content'],r['meta'],r['embedding']) for r in rows]
            with conn.connection.driver_connection.cursor() as cursor:
                execute_values(cursor, """INSERT INTO internship.vectors(collection_name,id,document_id,content,metadata,embedding)
                    VALUES %s ON CONFLICT(collection_name,id) DO UPDATE SET document_id=excluded.document_id,
                    content=excluded.content,metadata=excluded.metadata,embedding=excluded.embedding""", values,
                    template="(%s,%s,%s,%s,%s::jsonb,%s::vector)", page_size=250)
            conn.execute(text('UPDATE internship.collections SET revision=revision+1 WHERE name=:name'), {'name':self.name})

    def delete(self, ids=None, where=None):
        if ids is None and where is None:
            raise ValueError('Explicit IDs or filter required for deletion')
        clause, params = self._filter(ids, where)
        with self.connection() as conn:
            conn.execute(text(f'DELETE FROM internship.vectors WHERE {clause}'),params)
            conn.execute(text('UPDATE internship.collections SET revision=revision+1 WHERE name=:name'), {'name':self.name})

    def query(self, query_embeddings, n_results=5, where=None):
        if n_results < 1:
            raise ValueError('n_results must be positive')
        clause, params = self._filter(where=where)
        result = {k: [] for k in ['ids','documents','metadatas','distances']}
        for query in query_embeddings:
            params.update(embedding=vector_literal(query),limit=n_results)
            with self.connection() as conn:
                # Filtering uses exact search so a selective filter cannot be starved
                # by ANN's post-filtering. Unfiltered queries can use the HNSW index.
                conn.execute(text("SET LOCAL hnsw.ef_search=100"))
                if where:
                    conn.execute(text('SET LOCAL enable_indexscan=off'))
                rows = conn.execute(text(f'''SELECT id,content,metadata,embedding <-> CAST(:embedding AS vector) AS distance
                    FROM internship.vectors WHERE {clause}
                    ORDER BY embedding <-> CAST(:embedding AS vector) LIMIT :limit'''),params).mappings().all()
            rows = sorted(rows, key=lambda r:(r['distance'],r['id']))
            for key,col in [('ids','id'),('documents','content'),('metadatas','metadata'),('distances','distance')]:
                result[key].append([float(r[col])**2 if key=='distances' else bytes(r[col]).decode('utf-8') if key=='documents' else r[col] for r in rows])
        return result

    def save_document(self, document_id, content, metadata, provenance=None):
        with self.connection() as conn:
            conn.execute(text('''INSERT INTO internship.documents(document_id,text_content,metadata,provenance)
                VALUES (:id,:content,CAST(:metadata AS jsonb),CAST(:provenance AS jsonb))
                ON CONFLICT(document_id) DO UPDATE SET text_content=excluded.text_content,metadata=excluded.metadata,
                provenance=CASE WHEN :has_provenance THEN excluded.provenance ELSE internship.documents.provenance END,updated_at=now()'''),
                {'id':document_id,'content':content.encode('utf-8') if content is not None else None,'metadata':json.dumps(metadata),'provenance':json.dumps(provenance or {}),'has_provenance':provenance is not None})


def create_vector_indexes(engine=None):
    with (engine or get_engine()).begin() as conn:
        # Partial indexes separate chunk and centroid search spaces.
        for suffix,name in [('chunks','week2_rag_corpus'),('documents','week2_rag_documents')]:
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS vectors_{suffix}_hnsw ON internship.vectors USING hnsw (embedding vector_l2_ops) WHERE collection_name='{name}'")
        conn.exec_driver_sql('ANALYZE internship.vectors')
