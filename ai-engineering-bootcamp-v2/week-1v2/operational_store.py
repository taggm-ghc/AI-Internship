"""PostgreSQL operational state. Schema installation is explicit, never on import.

PgCollection is the current store of source documents and chunks (internship.vectors,
internship.documents). Its adapter keeps the retrieval contract the app was built on
(a collection-style query returning ids/documents/metadatas/distances). Distances are
squared L2; pgvector's <-> is unsquared L2. All SQL values are bound parameters.
The legacy local Chroma store was retired 2026-10-01 (p3m3 item #63).
"""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from db import get_admin_engine, get_engine

DIMENSIONS = 1536


def vector_literal(values):
    values = [float(v) for v in values]
    if len(values) != DIMENSIONS or not all(math.isfinite(v) for v in values):
        raise ValueError(f"Expected {DIMENSIONS} finite embedding values")
    return json.dumps(values, allow_nan=False)


def install_schema(engine=None):
    engine = engine or get_admin_engine()  # DDL: admin account only (item #47)
    # Every migration, in filename order, one transaction each. All of them are idempotent
    # (IF NOT EXISTS / state-checked), so re-running against an installed database is safe.
    files = sorted((Path(__file__).parent / 'migrations').glob('[0-9][0-9][0-9]_*.sql'))
    if not files:
        raise FileNotFoundError('no migrations found next to operational_store.py')
    for path in files:
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(path.read_text())
        except Exception as exc:
            raise RuntimeError(f'migration {path.name} failed (rolled back; earlier files stay applied): {exc}') from exc


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


def corpus_overview():
    """Return only public corpus-version metadata, never titles or content."""
    with get_engine().connect() as conn:
        row = conn.execute(text(
            'SELECT COUNT(*) AS document_count, MAX(updated_at) AS updated_at '
            'FROM internship.documents'
        )).one()
    updated_at = row.updated_at.isoformat() if row.updated_at is not None else None
    return {'document_count': int(row.document_count or 0), 'corpus_updated_at': updated_at}


def find_live_document_by_content_sha(content_sha256, exclude_document_id=None):
    """Cross-document exact-duplicate check: returns the document_id of a live
    document (other than exclude_document_id) whose recorded
    provenance->>'content_sha256' equals content_sha256, else None. The
    per-document version history is handled by stage_document_version; this
    covers identical content arriving under a DIFFERENT document_id. Documents
    ingested before content_sha256 was recorded carry no hash and cannot match
    (a known limit). Read-only."""
    with get_engine().connect() as conn:
        row = conn.execute(
            text("""SELECT document_id FROM internship.documents
                WHERE provenance->>'content_sha256'=:sha AND document_id IS DISTINCT FROM :ex
                ORDER BY document_id LIMIT 1"""),
            {"sha": content_sha256, "ex": exclude_document_id},
        ).first()
        return row[0] if row else None


def document_exists(document_id):
    """p3m3 item #30, repurposed by item #33: backs main.py's decision of
    whether a POST /ingest call is a first-time ingest (goes live
    immediately, nothing to conflict with) or a re-ingest of an existing
    document_id (item #33: always staged as a new version now, never a
    direct overwrite -- the single-ID-collision corpus-poisoning vector
    OWASP's RAG Security Cheat Sheet flags is closed structurally, not by
    an auth check on a destructive path that no longer exists). Indexed
    primary-key lookup, same get_engine().connect() pattern as
    corpus_summary above."""
    with get_engine().connect() as conn:
        return conn.execute(
            text('SELECT 1 FROM internship.documents WHERE document_id=:id'), {'id': document_id}
        ).first() is not None


def stage_document_version(document_id, text_content, metadata, provenance, content_sha256, adversarial_flags, client_ip, authenticated):
    """p3m3 item #33 -- the only way a re-ingest of an existing document_id
    can ever reach internship.documents/vectors is through
    accept_document_version below; this never writes to either. Backfills
    version=1 from the document's current live content the first time it's
    ever re-ingested, so the full lineage (including the original) is
    always diffable, not just versions created after staging existed.
    Returns the new version's row as a dict, or {"duplicate": True} with
    no version created if content_sha256 exactly matches ANY version this
    document has ever had -- the live content, a pending staged version,
    or a historical (superseded) one, not just the currently active one.
    An exact-duplicate re-ingest has nothing to version, and staging it
    anyway would just be a no-op row cluttering the history."""
    with get_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 1))"), {"id": document_id})
        already_seen = conn.execute(
            text("""SELECT 1 FROM internship.document_versions WHERE document_id=:id AND content_sha256=:sha
                UNION SELECT 1 FROM internship.documents WHERE document_id=:id AND provenance->>'content_sha256'=:sha"""),
            {"id": document_id, "sha": content_sha256},
        ).first()
        if already_seen is not None:
            return {"document_id": document_id, "version": None, "duplicate": True}
        existing_max = conn.execute(
            text("SELECT max(version) FROM internship.document_versions WHERE document_id=:id"), {"id": document_id}
        ).scalar()
        if existing_max is None:
            original = conn.execute(
                text("SELECT text_content, metadata, provenance FROM internship.documents WHERE document_id=:id"),
                {"id": document_id},
            ).mappings().one()
            conn.execute(
                text("""INSERT INTO internship.document_versions
                    (document_id, version, text_content, metadata, provenance, content_sha256, accepted_at, accepted_by)
                    VALUES (:id, 1, :content, :metadata, :provenance, :sha, now(), 'original')"""),
                {
                    "id": document_id,
                    "content": bytes(original["text_content"]) if original["text_content"] is not None else None,
                    "metadata": json.dumps(original["metadata"]),
                    "provenance": json.dumps(original["provenance"]),
                    "sha": original["provenance"].get("content_sha256"),
                },
            )
            existing_max = 1
        new_version = existing_max + 1
        conn.execute(
            text("""INSERT INTO internship.document_versions
                (document_id, version, text_content, metadata, provenance, content_sha256,
                 adversarial_flags, client_ip, authenticated)
                VALUES (:id, :version, :content, CAST(:metadata AS jsonb), CAST(:provenance AS jsonb), :sha,
                        CAST(:flags AS jsonb), :ip, :authed)"""),
            {
                "id": document_id, "version": new_version,
                "content": text_content.encode("utf-8"),
                "metadata": json.dumps(metadata or {}), "provenance": json.dumps(provenance or {}),
                "sha": content_sha256, "flags": json.dumps(adversarial_flags or {}),
                "ip": client_ip, "authed": authenticated,
            },
        )
        return {"document_id": document_id, "version": new_version}


_SOURCE_TABLES = ("source_identifiers", "identifiers", "sources", "document_sources", "source_identifier_types")


def _is_missing_source_table(exc):
    """True only for 'relation does not exist' (SQLSTATE 42P01) naming a migration-006 table."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == "42P01" and any(t in str(orig) for t in _SOURCE_TABLES)


def find_source_identity(identifiers):
    """Read-only lookup for item #65: returns (links, documents_by_source).
    links: every identifier link {source_id, type, value_norm, is_canonical, origin} of each source
    that shares any (type, value_norm) with `identifiers`; documents_by_source: {source_id:
    [document_id, ...]} ordered by linked_at then document_id (first = primary, the earliest linked). Returns ([], {}) when the migration
    006 tables do not exist yet; any other DB error propagates."""
    if not identifiers:
        return [], {}
    clauses, params = [], {}
    for n, i in enumerate(identifiers):
        clauses.append(f"(i.type=:t{n} AND i.value_norm=:v{n})")
        params[f"t{n}"], params[f"v{n}"] = i["type"], i["value_norm"]
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(
                text(f"""SELECT si.source_id, si.identifier_type AS type, i.value_norm, si.is_canonical, si.origin
                    FROM internship.source_identifiers si JOIN internship.identifiers i ON i.id = si.identifier_id
                    WHERE si.source_id IN (
                        SELECT s2.source_id FROM internship.source_identifiers s2
                        JOIN internship.identifiers i ON i.id = s2.identifier_id WHERE {' OR '.join(clauses)})
                    ORDER BY si.source_id, si.identifier_type, i.value_norm"""),
                params,
            ).mappings().all()
            links = [dict(r) for r in rows]
            docs = {}
            ids = sorted({l["source_id"] for l in links})
            if ids:
                for r in conn.execute(
                    text("SELECT source_id, document_id FROM internship.document_sources WHERE source_id = ANY(:ids) ORDER BY source_id, linked_at, document_id"),
                    {"ids": ids},
                ):
                    docs.setdefault(r.source_id, []).append(r.document_id)
            return links, docs
    except Exception as exc:
        if _is_missing_source_table(exc):
            return [], {}
        raise


def link_document_to_new_source(document_id, label, identifiers, asserted_by="ingest"):
    """Creates a source for a live document with its identifiers and links, one transaction,
    idempotent (conflict targets are named so partial unique index violations raise; a document already linked to a source is left alone and its
    source_id returned). Returns the source_id, or None when the migration 006 tables do not exist
    yet (write skipped); any other DB error propagates. Never attaches to an existing source."""
    try:
        with get_engine().begin() as conn:
            existing = conn.execute(
                text("SELECT source_id FROM internship.document_sources WHERE document_id=:d ORDER BY linked_at, source_id LIMIT 1"),
                {"d": document_id},
            ).first()
            if existing is not None:
                return existing[0]
            source_id = conn.execute(
                text("INSERT INTO internship.sources(label) VALUES (:label) RETURNING id"), {"label": label},
            ).scalar()
            for i in identifiers:
                conn.execute(
                    text("""INSERT INTO internship.identifiers(type, value_norm, value_raw) VALUES (:t, :n, :r)
                        ON CONFLICT (type, value_norm) DO NOTHING"""),
                    {"t": i["type"], "n": i["value_norm"], "r": i["value_raw"]},
                )
                ident_id = conn.execute(
                    text("SELECT id FROM internship.identifiers WHERE type=:t AND value_norm=:n"),
                    {"t": i["type"], "n": i["value_norm"]},
                ).scalar()
                n = conn.execute(
                    text("""INSERT INTO internship.source_identifiers
                        (source_id, identifier_id, identifier_type, is_canonical, origin, asserted_by)
                        VALUES (:s, :i, :t, :c, :o, :by) ON CONFLICT (source_id, identifier_id) DO NOTHING"""),
                    {"s": source_id, "i": ident_id, "t": i["type"], "c": i["is_canonical"], "o": i["origin"], "by": asserted_by},
                ).rowcount
                if n != 1:
                    raise RuntimeError(f"source identity: source_identifiers insert affected {n} rows (expected 1) for {i['type']}:{i['value_norm']}; rolled back")
            n = conn.execute(
                text("INSERT INTO internship.document_sources(document_id, source_id) VALUES (:d, :s) ON CONFLICT (document_id, source_id) DO NOTHING"),
                {"d": document_id, "s": source_id},
            ).rowcount
            if n != 1:
                raise RuntimeError(f"source identity: document_sources insert affected {n} rows (expected 1) for {document_id}; rolled back")
            return source_id
    except Exception as exc:
        if _is_missing_source_table(exc):
            return None
        raise


def list_document_versions(document_id):
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("""SELECT version, provenance, adversarial_flags, authenticated, created_at, accepted_at, accepted_by
                FROM internship.document_versions WHERE document_id=:id ORDER BY version"""),
            {"id": document_id},
        ).mappings().all()
        return [dict(r) for r in rows]


def get_document_version(document_id, version):
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM internship.document_versions WHERE document_id=:id AND version=:v"),
            {"id": document_id, "v": version},
        ).mappings().first()
        if row is None:
            return None
        row = dict(row)
        if row["text_content"] is not None:
            row["text_content"] = bytes(row["text_content"]).decode("utf-8")
        return row


def accept_document_version(document_id, version, accepted_by):
    """Marks a staged version as accepted. Does NOT itself write to
    internship.documents/vectors -- main.py calls upsert_chunks separately
    (the exact same chunk/embed/upsert path every ingest already goes
    through) once this returns the accepted text, so accepting a version
    is never a second, divergent code path from normal ingestion."""
    version_row = get_document_version(document_id, version)
    if version_row is None:
        return None
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE internship.document_versions SET accepted_at=now(), accepted_by=:by WHERE document_id=:id AND version=:v"),
            {"by": accepted_by, "id": document_id, "v": version},
        )
    return version_row


def get_artifact(path):
    """Payload bytes stored at `path` in internship.artifacts, or None (p3m3 item #80)."""
    with get_engine().connect() as conn:
        row = conn.execute(text('SELECT payload FROM internship.artifacts WHERE path=:p'), {'p': path}).first()
    return bytes(row[0]) if row else None


def corpus_profile(max_titles=150):
    """Metadata the corpus description is written from (p3m3 item #80): titles (a stable sample), and counts by
    provenance type, licence, source host and publication year. Never chunk or document text."""
    with get_engine().connect() as conn:
        titles = [r[0] for r in conn.execute(text(
            "SELECT provenance->>'title' FROM internship.documents WHERE provenance->>'title' IS NOT NULL "
            "ORDER BY md5(document_id) LIMIT :n"), {'n': max_titles})]

        def counts(expr, n=8):
            return {str(r[0]): int(r[1]) for r in conn.execute(text(
                f"SELECT {expr} AS k, count(*) FROM internship.documents GROUP BY 1 ORDER BY 2 DESC LIMIT :n"),
                {'n': n}) if r[0]}
        years = conn.execute(text(
            "SELECT min(substring(provenance->>'published_at' from '^[0-9]{4}')), "
            "max(substring(provenance->>'published_at' from '^[0-9]{4}')) FROM internship.documents")).one()
        return {
            'titles': titles,
            'provenance_types': counts("provenance->>'provenance_type'"),
            'licences': counts("provenance->>'license'"),
            'source_hosts': counts("substring(coalesce(provenance->>'source_url', provenance->>'fetched_from') "
                                   "from '^https?://([^/]+)')"),
            'published_years': [years[0], years[1]],
        }


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
            # operators rather than silently changing the meaning of a metadata filter.
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
    with (engine or get_admin_engine()).begin() as conn:  # DDL: admin account only (item #47)
        # Partial indexes separate chunk and centroid search spaces.
        for suffix,name in [('chunks','week2_rag_corpus'),('documents','week2_rag_documents')]:
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS vectors_{suffix}_hnsw ON internship.vectors USING hnsw (embedding vector_l2_ops) WHERE collection_name='{name}'")
        conn.exec_driver_sql('ANALYZE internship.vectors')
