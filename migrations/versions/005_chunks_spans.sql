-- 005 — Chunks + spans
--
-- Chunks are the retrieval unit. Each carries enough citation info that the synthesis
-- pipeline can pinpoint provenance without fetching the original. Spans are sentence-
-- (or smaller) granularity for UI highlighting in the original document.
-- Both chunk_id and span_id are content-derived hashes so re-runs converge.

CREATE TABLE chunks (
  chunk_id          uuid PRIMARY KEY,                        -- content hash
  doc_id            uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  tenant_id         uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  ordinal           int  NOT NULL,
  text              text NOT NULL,
  text_tsv          tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED,
  token_count       int,

  block_ids         uuid[] NOT NULL,                         -- which CDM blocks this chunk spans
  source_locator    jsonb NOT NULL,                          -- aggregate locator (e.g., {pages: [3,4], char_range: ...})

  -- vector index linkage (vectors live in Qdrant chunks_collection)
  qdrant_point_id   uuid NOT NULL,
  embed_model       text NOT NULL,                           -- e.g., bge-m3-int8
  embed_model_version text NOT NULL,

  -- denormalized for fast filter pushdown (mirrored to Qdrant payload)
  scope_ids         uuid[] NOT NULL DEFAULT '{}',
  processing_profile text NOT NULL,
  valid_time_start  timestamptz,
  valid_time_end    timestamptz,

  pipeline_version  text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chunks_doc_idx          ON chunks(doc_id, ordinal);
CREATE INDEX chunks_tenant_idx       ON chunks(tenant_id);
CREATE INDEX chunks_text_tsv_gin     ON chunks USING gin (text_tsv);
CREATE INDEX chunks_scope_gin        ON chunks USING gin (scope_ids);
CREATE INDEX chunks_qdrant_point_idx ON chunks(qdrant_point_id);

-- Spans: sentence-level (or smaller) units within a chunk, for UI highlighting and
-- precise citation in the original source (page+bbox for PDFs, timestamps for audio).
-- Stored separately so they're fetched only when the UI asks; not indexed in Qdrant.
CREATE TABLE spans (
  span_id           uuid PRIMARY KEY,                        -- content hash
  chunk_id          uuid NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  doc_id            uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  tenant_id         uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  block_id          uuid REFERENCES canonical_blocks(block_id) ON DELETE SET NULL,

  ordinal           int  NOT NULL,
  text              text NOT NULL,
  offset_in_chunk   int4range NOT NULL,                       -- char offsets within chunk.text

  source_locator    jsonb NOT NULL,
  -- pdf:   {page, char_start_in_page, char_end_in_page, bbox}
  -- text:  {section_path, char_start_in_doc, char_end_in_doc}
  -- audio: {t_start_ms, t_end_ms, segment_id, speaker?}

  pipeline_version  text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX spans_chunk_idx ON spans(chunk_id, ordinal);
CREATE INDEX spans_doc_idx   ON spans(doc_id);
CREATE INDEX spans_block_idx ON spans(block_id) WHERE block_id IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('005_chunks_spans');
