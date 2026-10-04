-- Migration 006 (p3m3 item #65): source identity. Additive and idempotent; admin-run.
-- Sources are the original works; identifiers are shared entities (source <-> identifier is many-to-many);
-- documents attach to a source. Precedence rule 1 is structural: a source has at most ONE canonical-original
-- identifier per type, and a canonical-original identifier value belongs to at most one source.
-- Grants to the app roles are issued by scripts/apply_source_identity_migration.py (role names come from .env).

CREATE SCHEMA IF NOT EXISTS internship;

CREATE TABLE IF NOT EXISTS internship.source_identifier_types (
    type text PRIMARY KEY,
    normaliser text NOT NULL,
    strength text NOT NULL CONSTRAINT source_identifier_types_strength_chk CHECK (strength IN ('strong','weak')),
    description text NOT NULL DEFAULT ''
);

INSERT INTO internship.source_identifier_types(type, normaliser, strength, description) VALUES
    ('arxiv',    'normalise_arxiv',    'strong', 'arXiv identifier, version suffix removed'),
    ('doi',      'normalise_doi',      'strong', 'Digital Object Identifier, lowercased, resolver prefix removed'),
    ('isbn',     'normalise_isbn',     'strong', 'ISBN-13, hyphens removed'),
    ('url',      'normalise_url',      'weak',   'Landing page or host URL; may be shared by several sources'),
    ('pmid',     'normalise_pmid',     'strong', 'PubMed identifier'),
    ('handle',   'normalise_handle',   'strong', 'Handle System identifier'),
    ('openalex', 'normalise_openalex', 'strong', 'OpenAlex work identifier'),
    ('other',    'normalise_other',    'weak',   'Any other identifier; never decisive')
ON CONFLICT (type) DO NOTHING;

CREATE TABLE IF NOT EXISTS internship.identifiers (
    id bigserial PRIMARY KEY,
    type text NOT NULL CONSTRAINT identifiers_type_fk REFERENCES internship.source_identifier_types(type),
    value_norm text NOT NULL,
    value_raw text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT identifiers_type_value_uq UNIQUE (type, value_norm),
    CONSTRAINT identifiers_id_type_uq UNIQUE (id, type)
);

CREATE TABLE IF NOT EXISTS internship.sources (
    id bigserial PRIMARY KEY,
    label text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- identifier_type is denormalised (kept consistent by the composite FK) so the partial unique indexes below can use it.
CREATE TABLE IF NOT EXISTS internship.source_identifiers (
    source_id bigint NOT NULL CONSTRAINT source_identifiers_source_fk REFERENCES internship.sources(id),
    identifier_id bigint NOT NULL,
    identifier_type text NOT NULL,
    is_canonical boolean NOT NULL,
    origin text NOT NULL CONSTRAINT source_identifiers_origin_chk CHECK (origin IN ('original','derived')),
    asserted_by text,
    asserted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_identifiers_pk PRIMARY KEY (source_id, identifier_id),
    CONSTRAINT source_identifiers_identifier_fk FOREIGN KEY (identifier_id, identifier_type)
        REFERENCES internship.identifiers(id, type)
);

CREATE UNIQUE INDEX IF NOT EXISTS source_identifiers_one_canonical_original_per_type
    ON internship.source_identifiers (source_id, identifier_type) WHERE is_canonical AND origin = 'original';
CREATE UNIQUE INDEX IF NOT EXISTS source_identifiers_canonical_original_identifier_uq
    ON internship.source_identifiers (identifier_id) WHERE is_canonical AND origin = 'original';
CREATE INDEX IF NOT EXISTS source_identifiers_identifier_idx ON internship.source_identifiers (identifier_id);

CREATE TABLE IF NOT EXISTS internship.document_sources (
    document_id text NOT NULL CONSTRAINT document_sources_document_fk REFERENCES internship.documents(document_id) ON DELETE CASCADE,
    source_id bigint NOT NULL CONSTRAINT document_sources_source_fk REFERENCES internship.sources(id),
    linked_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT document_sources_pk PRIMARY KEY (document_id, source_id)
);
CREATE INDEX IF NOT EXISTS document_sources_source_idx ON internship.document_sources (source_id);

INSERT INTO internship.schema_version(version) VALUES (6) ON CONFLICT DO NOTHING;
