-- 004 — Documents + canonical blocks
--
-- Documents is the spine. Every captured artifact is a document, regardless of source
-- (file, voice note, slack message, agent output, calendar event).
-- Canonical blocks form the lossless, structured representation that extraction reads
-- forever — independent of the original format.

CREATE TABLE documents (
  doc_id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                    uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  source_id                    uuid REFERENCES sources(source_id) ON DELETE SET NULL,

  -- capture context
  source_channel               text NOT NULL,
  captured_by                  text NOT NULL DEFAULT 'user'
                                 CHECK (captured_by IN ('user','agent','system')),
  thread_id                    uuid,                       -- groups bursts of related captures

  -- storage refs (object store URIs)
  raw_uri                      text NOT NULL,              -- s3://woxpas-raw/{sha256}
  raw_iv                       bytea,                      -- per-object IV for client-side encryption
  canonical_uri                text,                       -- s3://woxpas-canonical/{doc}/v{n}.json
  canonical_iv                 bytea,

  -- content identity
  sha256                       bytea NOT NULL,
  mime                         text,
  size_bytes                   bigint,
  filename                     text,
  title                        text,
  language                     text,
  page_count                   int,
  duration_ms                  int,

  -- pipeline routing
  processing_profile           text NOT NULL DEFAULT 'mixed_note'
                                 CHECK (processing_profile IN (
                                   'thought','voice_note','mixed_note',
                                   'artefact_lyric','artefact_contract','artefact_reference',
                                   'book','paper','email','calendar_event','manual','code',
                                   'transcript','correspondence','custom'
                                 )),
  processing_profile_version   text NOT NULL DEFAULT '0.1.0',
  pipeline_version             text NOT NULL,
  status                       text NOT NULL DEFAULT 'received'
                                 CHECK (status IN (
                                   'received','quarantined','rejected','extracting',
                                   'normalized','summarizing','chunking','embedding',
                                   'indexed','failed'
                                 )),

  -- AV
  av_result                    text CHECK (av_result IN (NULL,'clean','infected','error','skipped')),
  av_signature                 text,
  av_scanned_at                timestamptz,

  -- summary (tier-zero index)
  summary                      text,
  summary_embedding_ref        uuid,                       -- pointer into Qdrant summaries_collection
  summary_model_version        text,
  summary_profile_version      text,
  summary_generated_at         timestamptz,
  topics_inferred              text[] DEFAULT '{}',
  doc_type_inferred            text,
  language_detected            text,

  -- bitemporal
  system_time                  tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time                   tstzrange,                  -- when content is "about" — extracted later

  -- decay / surfacing
  last_seen_at                 timestamptz,
  last_mentioned_at            timestamptz,
  last_accessed_at             timestamptz,
  access_count                 int NOT NULL DEFAULT 0,
  decay_score                  real NOT NULL DEFAULT 1.0,
  momentum_score               real NOT NULL DEFAULT 0.0,
  surface_score                real NOT NULL DEFAULT 0.0,

  -- supersession (Type-1 conflict / revision chain)
  supersedes_doc_id            uuid REFERENCES documents(doc_id) ON DELETE SET NULL,
  supersedes_candidate_doc_id  uuid REFERENCES documents(doc_id) ON DELETE SET NULL,
  superseded_by_doc_id         uuid REFERENCES documents(doc_id) ON DELETE SET NULL,
  revision_chain               uuid[] DEFAULT '{}',

  -- external sync (calendar, slack threads, mcp source ids)
  external_refs                jsonb NOT NULL DEFAULT '[]'::jsonb,

  -- archive / soft delete
  archived_at                  timestamptz,
  deleted_at                   timestamptz,

  created_at                   timestamptz NOT NULL DEFAULT now(),
  updated_at                   timestamptz NOT NULL DEFAULT now()
);

-- Hot-path indexes (covering common filter combinations)
CREATE INDEX documents_tenant_status_idx       ON documents(tenant_id, status)
                                                 WHERE deleted_at IS NULL;
CREATE INDEX documents_tenant_profile_idx      ON documents(tenant_id, processing_profile)
                                                 WHERE deleted_at IS NULL;
CREATE INDEX documents_tenant_channel_idx      ON documents(tenant_id, source_channel)
                                                 WHERE deleted_at IS NULL;
CREATE INDEX documents_thread_idx              ON documents(thread_id)
                                                 WHERE thread_id IS NOT NULL;
CREATE INDEX documents_sha256_idx              ON documents(tenant_id, sha256);
CREATE INDEX documents_supersedes_idx          ON documents(supersedes_doc_id)
                                                 WHERE supersedes_doc_id IS NOT NULL;
CREATE INDEX documents_topics_gin              ON documents USING gin (topics_inferred);
CREATE INDEX documents_system_time_gist        ON documents USING gist (system_time);
CREATE INDEX documents_valid_time_gist         ON documents USING gist (valid_time)
                                                 WHERE valid_time IS NOT NULL;
CREATE INDEX documents_decay_idx               ON documents(tenant_id, decay_score DESC)
                                                 WHERE deleted_at IS NULL;
CREATE INDEX documents_external_refs_gin       ON documents USING gin (external_refs jsonb_path_ops);
CREATE INDEX documents_filename_trgm           ON documents USING gin (filename gin_trgm_ops)
                                                 WHERE filename IS NOT NULL;
CREATE INDEX documents_title_trgm              ON documents USING gin (title gin_trgm_ops)
                                                 WHERE title IS NOT NULL;

-- Canonical Document Model (CDM) blocks. Lossless structured representation.
-- Block IDs are content-derived hashes so re-ingestion is idempotent.
CREATE TABLE canonical_blocks (
  block_id        uuid PRIMARY KEY,                          -- content hash, NOT auto
  doc_id          uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  ordinal         int  NOT NULL,
  type            text NOT NULL
                    CHECK (type IN (
                      'heading','paragraph','list_item','table','table_row',
                      'caption','footnote','quote','code','math',
                      'audio_segment','image_caption','signature_block','metadata'
                    )),
  level           int,                                       -- heading depth, list nesting
  parent_block_id uuid REFERENCES canonical_blocks(block_id) ON DELETE CASCADE,

  text            text,
  attrs           jsonb NOT NULL DEFAULT '{}'::jsonb,        -- type-specific (table cells, code lang, list_marker, etc.)

  -- precise origin in source (discriminated union by doc kind)
  source_locator  jsonb NOT NULL,
  -- pdf:    {page, char_start_in_page, char_end_in_page, bbox: [x1,y1,x2,y2]}
  -- text:   {section_path: [...], char_start, char_end}
  -- audio:  {segment_id, t_start_ms, t_end_ms, speaker?}
  -- email:  {part_id, char_start, char_end}

  pipeline_version text NOT NULL,

  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX canonical_blocks_doc_idx      ON canonical_blocks(doc_id, ordinal);
CREATE INDEX canonical_blocks_parent_idx   ON canonical_blocks(parent_block_id)
                                             WHERE parent_block_id IS NOT NULL;
CREATE INDEX canonical_blocks_type_idx     ON canonical_blocks(doc_id, type);
CREATE INDEX canonical_blocks_text_trgm    ON canonical_blocks USING gin (text gin_trgm_ops)
                                             WHERE text IS NOT NULL;

-- Now we can add the deferred FK from doc_scopes.
ALTER TABLE doc_scopes
  ADD CONSTRAINT doc_scopes_doc_fk
  FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE;

INSERT INTO schema_migrations (version) VALUES ('004_documents');
