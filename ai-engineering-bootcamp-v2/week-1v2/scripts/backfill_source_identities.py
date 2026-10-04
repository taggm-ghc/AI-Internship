"""Backfill source identities (item #65) from config/citation_metadata.json (and, with --from-db, provenance rows).

Dry-run by default: prints the sources, identifiers and document_sources it WOULD create. --apply writes them via
the admin engine in batches, in one transaction, idempotently (named ON CONFLICT targets, rowcounts checked). Identifier extraction is
done only by source_identity.extract_identifiers(provenance, metadata, trusted=True) -> [{type,value_norm,value_raw,is_canonical,origin}].
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / '.env')
from sqlalchemy import text

METADATA = BASE / 'config' / 'citation_metadata.json'
ARXIV_KEY = re.compile(r'^arxiv-(\d{4}\.\d{4,5})-')
ASSERTED_BY = 'backfill_source_identities'


def load_items(path=METADATA):
    return json.loads(Path(path).read_text())['items']


def augment_provenance(doc_id, prov, item):
    """Admin-verified hints for trusted extraction: an arXiv item's own URL (arxiv.org/abs/...) or its
    key prefix 'arxiv-<id>-' gives the canonical-original arXiv id of the original source."""
    prov = dict(prov or {})
    url = (item or {}).get('URL')
    if url and not prov.get('source_url'):
        prov['source_url'] = url
    m = ARXIV_KEY.match(doc_id)
    if m and not prov.get('arxiv_id'):
        prov['arxiv_id'] = m.group(1)
        prov['identifier_origin'] = 'original'
    return prov


def plan_backfill(items, provenance, extract, known_docs=None):
    """Pure. items: {document_id: csl item}; provenance: {document_id: dict} (may be empty).
    extract(prov, item) must be a trusted extraction. known_docs (set) drops documents absent from
    internship.documents BEFORE grouping so no orphan sources are created; they are reported as 'orphans'.
    Documents sharing a canonical-original identifier share one source; others get one source each."""
    candidates = sorted(set(items) | set(provenance))
    orphans = [d for d in candidates if known_docs is not None and d not in known_docs]
    docs = {}
    for doc_id in candidates:
        if doc_id in orphans:
            continue
        docs[doc_id] = extract(augment_provenance(doc_id, provenance.get(doc_id), items.get(doc_id)), items.get(doc_id)) or []
    parent = {d: d for d in docs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner = {}
    for d, ids in docs.items():
        for i in ids:
            if i['is_canonical'] and i['origin'] == 'original':
                key = (i['type'], i['value_norm'])
                if key in owner:
                    parent[find(d)] = find(owner[key])
                else:
                    owner[key] = d
    groups = {}
    for d in docs:
        groups.setdefault(find(d), []).append(d)
    sources, identifiers, links, doc_sources = [], {}, {}, []
    for idx, (root, members) in enumerate(sorted(groups.items())):
        sources.append({'ref': idx, 'label': root})
        for d in members:
            doc_sources.append({'document_id': d, 'source_ref': idx})
            for i in docs[d]:
                identifiers.setdefault((i['type'], i['value_norm']), i['value_raw'])
                links.setdefault((idx, i['type'], i['value_norm']), (i['is_canonical'], i['origin']))
    conflicts = _canonical_conflicts(links)
    counts = {}
    for (_, typ, _), (canon, origin) in links.items():
        if canon and origin == 'original':
            counts[typ] = counts.get(typ, 0) + 1
    return {'sources': sources, 'identifiers': identifiers, 'links': links,
            'document_sources': doc_sources, 'conflicts': conflicts, 'orphans': orphans,
            'canonical_original_counts': counts}


def _canonical_conflicts(links):
    """A source with two different canonical-original ids of one type would violate the partial unique index."""
    seen, bad = {}, []
    for (ref, typ, val), (canon, origin) in sorted(links.items()):
        if canon and origin == 'original':
            if (ref, typ) in seen and seen[(ref, typ)] != val:
                bad.append((ref, typ, seen[(ref, typ)], val))
            seen.setdefault((ref, typ), val)
    return bad


def read_db_provenance(engine):
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text('SELECT document_id, provenance FROM internship.documents'))}


def _exists(conn, sql, params):
    return conn.execute(text(sql), params).first() is not None


def apply_plan(conn, p):
    """Idempotent, fail-verbose: conflict targets are named; an insert that affects 0 rows must correspond to an
    identical existing row, otherwise raise (rolls the transaction back)."""
    for (typ, norm), raw in sorted(p['identifiers'].items()):
        conn.execute(text('INSERT INTO internship.identifiers(type,value_norm,value_raw) VALUES (:t,:n,:r) '
                          'ON CONFLICT (type,value_norm) DO NOTHING'), {'t': typ, 'n': norm, 'r': raw})
    refs = {}
    for s in p['sources']:
        members = [d['document_id'] for d in p['document_sources'] if d['source_ref'] == s['ref']]
        row = conn.execute(text('SELECT source_id FROM internship.document_sources WHERE document_id = ANY(:m) '
                                'ORDER BY linked_at, document_id LIMIT 1'), {'m': members}).fetchone()
        if row:
            refs[s['ref']] = row[0]
        else:
            refs[s['ref']] = conn.execute(text('INSERT INTO internship.sources(label) VALUES (:l) RETURNING id'),
                                          {'l': s['label']}).scalar_one()
    for (ref, typ, norm), (canon, origin) in sorted(p['links'].items()):
        r = {'s': refs[ref], 't': typ, 'n': norm, 'c': canon, 'o': origin, 'a': ASSERTED_BY}
        n = conn.execute(text(
            'INSERT INTO internship.source_identifiers(source_id,identifier_id,identifier_type,is_canonical,origin,asserted_by) '
            'SELECT :s, i.id, i.type, :c, :o, :a FROM internship.identifiers i WHERE i.type=:t AND i.value_norm=:n '
            'ON CONFLICT (source_id, identifier_id) DO NOTHING'), r).rowcount
        if n != 1 and not (n == 0 and _exists(conn,
                'SELECT 1 FROM internship.source_identifiers si JOIN internship.identifiers i ON i.id=si.identifier_id '
                'WHERE si.source_id=:s AND i.type=:t AND i.value_norm=:n AND si.is_canonical=:c AND si.origin=:o', r)):
            raise RuntimeError(f'source_identifiers insert affected {n} rows for source#{ref} {typ}:{norm}; no identical row exists')
    for d in p['document_sources']:
        r = {'d': d['document_id'], 's': refs[d['source_ref']]}
        n = conn.execute(text('INSERT INTO internship.document_sources(document_id,source_id) VALUES (:d, :s) '
                              'ON CONFLICT (document_id, source_id) DO NOTHING'), r).rowcount
        if n != 1 and not (n == 0 and _exists(conn, 'SELECT 1 FROM internship.document_sources WHERE document_id=:d AND source_id=:s', r)):
            raise RuntimeError(f'document_sources insert affected {n} rows for {r["d"]}; no identical row exists')


def print_plan(p):
    print(f'sources: {len(p["sources"])}  identifiers: {len(p["identifiers"])}  '
          f'source_identifiers: {len(p["links"])}  document_sources: {len(p["document_sources"])}')
    for (ref, typ, norm), (canon, origin) in sorted(p['links'].items())[:20]:
        print(f'  source#{ref} <- {typ}:{norm} canonical={canon} origin={origin}')
    if len(p['links']) > 20:
        print(f'  ... {len(p["links"]) - 20} more')
    print(f'canonical-original ids by type: {p["canonical_original_counts"]}')
    print(f'orphans (not in internship.documents, skipped): {len(p["orphans"])}')
    for d in p['orphans'][:20]:
        print(f'  orphan {d}')
    for c in p['conflicts']:
        print(f'  CONFLICT source#{c[0]} has two canonical-original {c[1]} ids: {c[2]} vs {c[3]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='write (default: dry run)')
    parser.add_argument('--from-db', action='store_true', help='also read provenance rows (read-only)')
    args = parser.parse_args()
    try:
        from source_identity import extract_identifiers
    except ImportError:
        raise SystemExit('source_identity.extract_identifiers is not available yet')
    try:
        engine = None
        provenance = {}
        if args.from_db or args.apply:
            from db import get_admin_engine
            engine = get_admin_engine()
        db_prov = read_db_provenance(engine) if engine is not None else None
        known = set(db_prov) if db_prov is not None else None
        if args.from_db:
            provenance = db_prov
        p = plan_backfill(load_items(), provenance, lambda prov, item: extract_identifiers(prov, item, trusted=True), known)
        print_plan(p)
        if p['conflicts']:
            raise SystemExit('Refusing to continue: resolve conflicts above (original canonical ids prevail; review needed)')
        if not args.apply:
            print('Dry run: nothing written. Re-run with --apply to write.')
            return
        with engine.begin() as conn:
            apply_plan(conn, p)
        print('Applied in one transaction.')
    except SystemExit:
        raise
    except Exception as exc:
        print(f'Failed: {type(exc).__name__} (connection details withheld); transaction rolled back', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
