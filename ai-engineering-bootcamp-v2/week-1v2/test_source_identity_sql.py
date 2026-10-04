"""Offline tests (no DB) for migration 006, its apply script and the backfill planner (item #65)."""
import importlib.util
import re
from pathlib import Path

BASE = Path(__file__).parent
SQL = (BASE / 'migrations' / '006_source_identity.sql').read_text()
CODE = re.sub(r'--[^\n]*', '', SQL)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, BASE / 'scripts' / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tables_and_idempotency():
    for t in ['source_identifier_types', 'identifiers', 'sources', 'source_identifiers', 'document_sources']:
        assert f'CREATE TABLE IF NOT EXISTS internship.{t} ' in CODE
    assert not re.search(r'CREATE TABLE (?!IF NOT EXISTS)', CODE)
    assert not re.search(r'CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)', CODE)
    assert 'ON CONFLICT (type) DO NOTHING' in CODE
    assert 'internship.schema_version(version) VALUES (6) ON CONFLICT DO NOTHING' in CODE


def test_seed_types():
    for t in ['arxiv', 'doi', 'isbn', 'url', 'pmid', 'handle', 'openalex', 'other']:
        assert re.search(rf"\('{t}',\s+'normalise_{t}'", CODE)


def test_constraints():
    assert "CHECK (origin IN ('original','derived'))" in CODE
    for c in ['identifiers_type_value_uq', 'source_identifiers_pk', 'document_sources_pk',
              'source_identifiers_identifier_fk', 'document_sources_document_fk']:
        assert c in CODE
    assert 'UNIQUE (type, value_norm)' in CODE
    assert 'ON DELETE CASCADE' in CODE


def test_partial_unique_index_rule_1():
    m = re.search(r'CREATE UNIQUE INDEX IF NOT EXISTS source_identifiers_one_canonical_original_per_type\s+'
                  r'ON internship\.source_identifiers \(source_id, identifier_type\) WHERE is_canonical AND origin = \'original\'', CODE)
    assert m


def test_no_roles_no_destructive_sql():
    assert not re.search(r'\b(CREATE|ALTER|DROP)\s+(ROLE|USER)\b', CODE, re.I)
    assert 'GRANT' not in CODE.upper()
    assert not re.search(r'\bDROP\b|\bDELETE FROM\b|\bTRUNCATE\b', CODE, re.I)
    assert set(re.findall(r'internship\.(\w+)', CODE)) <= {
        'source_identifier_types', 'identifiers', 'sources', 'source_identifiers', 'document_sources',
        'documents', 'schema_version'}


def test_grants_only_new_internship_tables():
    mod = _load('apply_source_identity_migration')
    grants = mod.grant_statements('rw_g', 'ro_g')
    new = {'source_identifier_types', 'identifiers', 'sources', 'source_identifiers', 'document_sources'}
    for g in grants:
        assert g.startswith('GRANT ') and 'ROLE' not in g
        target = re.search(r'ON (?:SEQUENCE )?internship\.(\w+)', g).group(1)
        assert target in new or target in ('identifiers_id_seq', 'sources_id_seq')
        assert not re.search(r'DELETE|ALL|TRUNCATE', g)
    assert 'GRANT SELECT ON internship.source_identifier_types TO "ro_g"' in grants
    assert not any('source_identifier_types TO "rw_g"' in g for g in grants)


def _stub(provenance, metadata):
    out = []
    if metadata and metadata.get('DOI'):
        out.append({'type': 'doi', 'value_norm': metadata['DOI'].lower(), 'value_raw': metadata['DOI'],
                    'is_canonical': True, 'origin': 'original'})
    if metadata and metadata.get('URL'):
        out.append({'type': 'url', 'value_norm': metadata['URL'], 'value_raw': metadata['URL'],
                    'is_canonical': False, 'origin': 'derived'})
    return out


def test_backfill_plan_groups_and_detects_conflict():
    mod = _load('backfill_source_identities')
    items = {'a': {'DOI': '10.1/X', 'URL': 'http://h/1'}, 'b': {'DOI': '10.1/x', 'URL': 'http://h/1'},
             'c': {'DOI': '10.2/y', 'URL': 'http://h/1'}}
    p = mod.plan_backfill(items, {}, _stub)
    assert len(p['sources']) == 2          # a and b share a DOI; c is distinct despite shared URL
    assert len(p['document_sources']) == 3
    assert len(p['identifiers']) == 3
    assert p['conflicts'] == []
    assert mod._canonical_conflicts({(0, 'doi', 'x'): (True, 'original'), (0, 'doi', 'y'): (True, 'original')})


def test_linked_at_column_for_primary_ordering():
    assert 'linked_at timestamptz NOT NULL DEFAULT now()' in CODE


def test_app_grants_least_privilege():
    mod = _load('apply_source_identity_migration')
    grants = mod.grant_statements('rw_g', 'ro_g')
    for g in grants:
        assert 'UPDATE' not in g
    assert 'GRANT INSERT, SELECT ON internship.sources TO "rw_g"' in grants
    assert 'GRANT USAGE ON SEQUENCE internship.sources_id_seq TO "rw_g"' in grants


def _stub_trusted(provenance, metadata):
    out = []
    if provenance and provenance.get('arxiv_id'):
        out.append({'type': 'arxiv', 'value_norm': provenance['arxiv_id'], 'value_raw': provenance['arxiv_id'],
                    'is_canonical': True, 'origin': 'original'})
    return out


def test_backfill_drops_orphans_before_grouping():
    mod = _load('backfill_source_identities')
    items = {'arxiv-2401.00001-x': {}, 'arxiv-2401.00002-y': {}}
    p = mod.plan_backfill(items, {}, _stub_trusted, known_docs={'arxiv-2401.00001-x'})
    assert p['orphans'] == ['arxiv-2401.00002-y']
    assert len(p['sources']) == 1 and [d['document_id'] for d in p['document_sources']] == ['arxiv-2401.00001-x']


def test_backfill_arxiv_from_key_and_url():
    import source_identity
    mod = _load('backfill_source_identities')
    items = {'arxiv-2401.00001-x': {'URL': 'https://arxiv.org/abs/2401.00001'},
             'arxiv-2401.00002-y': {}, 'blog': {'URL': 'https://blog.example.com/p'}}
    p = mod.plan_backfill(items, {}, lambda pr, it: source_identity.extract_identifiers(pr, it, trusted=True))
    assert p['canonical_original_counts'] == {'arxiv': 2}
    assert len(p['sources']) == 3


class _R:
    def __init__(self, rowcount=1, first=None):
        self.rowcount, self._f = rowcount, first

    def first(self):
        return self._f

    fetchone = first

    def scalar_one(self):
        return 1


class _C:
    def __init__(self, sb):
        self.sb, self.sqls = sb, []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sqls.append(sql)
        if 'INSERT INTO internship.source_identifiers' in sql:
            return _R(rowcount=self.sb)
        if sql.startswith('SELECT 1'):
            return _R(first=None)
        return _R()


def test_backfill_apply_fails_verbosely_on_swallowed_conflict():
    import pytest
    mod = _load('backfill_source_identities')
    plan = {'identifiers': {('doi', 'x'): 'x'}, 'sources': [{'ref': 0, 'label': 'a'}],
            'links': {(0, 'doi', 'x'): (True, 'original')}, 'document_sources': [{'document_id': 'a', 'source_ref': 0}]}
    c = _C(1)
    mod.apply_plan(c, plan)
    assert 'ON CONFLICT (source_id, identifier_id) DO NOTHING' in ' '.join(c.sqls)
    with pytest.raises(RuntimeError, match='no identical row'):
        mod.apply_plan(_C(0), plan)
