CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS internship;
CREATE TABLE IF NOT EXISTS internship.schema_version (
    version integer PRIMARY KEY, installed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS internship.collections (
    name text PRIMARY KEY, revision bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS internship.documents (
    document_id text PRIMARY KEY, text_content bytea,
    metadata jsonb NOT NULL DEFAULT '{}', provenance jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS internship.vectors (
    collection_name text NOT NULL REFERENCES internship.collections(name),
    id text NOT NULL, document_id text NOT NULL, content bytea NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}', embedding vector(1536) NOT NULL,
    PRIMARY KEY(collection_name, id)
);
CREATE INDEX IF NOT EXISTS vectors_document_idx ON internship.vectors(collection_name, document_id);
CREATE TABLE IF NOT EXISTS internship.artifacts (
    path text PRIMARY KEY, sha256 text NOT NULL, payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS internship.events (
    id text PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL, payload_raw bytea,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_kind_created_idx ON internship.events(kind, created_at);
CREATE TABLE IF NOT EXISTS internship.provider_observations (
    provider text NOT NULL, model text NOT NULL, observed_at timestamptz NOT NULL,
    PRIMARY KEY(provider, model, observed_at)
);
INSERT INTO internship.schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;

-- Upgrade an interrupted first installation without dropping existing data.
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='internship' AND table_name='vectors' AND column_name='content' AND data_type='text') THEN
  ALTER TABLE internship.vectors ALTER COLUMN content TYPE bytea USING convert_to(content,'UTF8');
 END IF;
 IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='internship' AND table_name='documents' AND column_name='text_content' AND data_type='text') THEN
  ALTER TABLE internship.documents ALTER COLUMN text_content TYPE bytea USING convert_to(text_content,'UTF8');
 END IF;
END $$;
ALTER TABLE internship.events ADD COLUMN IF NOT EXISTS payload_raw bytea;
