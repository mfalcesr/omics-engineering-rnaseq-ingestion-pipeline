-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 001, extend `sample` for schema evolution, idempotency, audit
--
-- Design: ADDITIVE and IDEMPOTENT only.
--   * Every statement is `IF NOT EXISTS` / `CREATE OR REPLACE`, so re-running is a no-op.
--   * We only ADD nullable columns and CREATE objects. Nothing is renamed, dropped, or
--     narrowed, the legacy columns and the five seed rows that downstream R scripts and
--     dashboards depend on are untouched.
--
-- Why these columns (see docs/DECISIONS.md → D1 "hybrid promote + JSONB catch-all"):
--   The vendor field set changes per assay and per CRO. Rather than a migration per field
--   (column-per-field) or an untyped dumping ground (JSONB-only / EAV), we PROMOTE the ~10
--   fields we actually query or constrain to typed columns, and keep the long tail in a
--   single GIN-indexed `extra_metadata JSONB`. The full original payload is preserved
--   byte-for-byte in the append-only `sample_payload_log` audit table.
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── 1. Promote high-value vendor fields to typed columns ─────────────────────
ALTER TABLE sample
    ADD COLUMN IF NOT EXISTS sequencing_batch    VARCHAR(64),   -- needed for batch-effect analysis downstream
    ADD COLUMN IF NOT EXISTS cro                 VARCHAR(128),  -- normalised from cro / sequencing_centre
    ADD COLUMN IF NOT EXISTS instrument_model    VARCHAR(128),
    ADD COLUMN IF NOT EXISTS matrix_url          TEXT,          -- reproducibility: full HDF5 matrix location
    ADD COLUMN IF NOT EXISTS gene_model          VARCHAR(64),   -- e.g. GRCh38/Ensembl110, comparability key
    ADD COLUMN IF NOT EXISTS vendor_generated_at TIMESTAMPTZ,   -- payload `generated_at`; drives upsert-if-newer
    ADD COLUMN IF NOT EXISTS lab_notes           TEXT,          -- free text (flagged as PHI-leak risk in the platform design)
    ADD COLUMN IF NOT EXISTS extra_metadata      JSONB NOT NULL DEFAULT '{}'::jsonb, -- long-tail catch-all
    ADD COLUMN IF NOT EXISTS ingested_by         VARCHAR(64),   -- audit: which process/user loaded the row
    ADD COLUMN IF NOT EXISTS source_row_hash     CHAR(64);      -- sha256 of normalised payload; change detection

-- ── 2. Indexes for the promoted fields ───────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_sample_extra_gin ON sample USING GIN (extra_metadata);
CREATE INDEX IF NOT EXISTS idx_sample_batch     ON sample (sequencing_batch);

-- ── 3. Append-only payload log = audit trail + dead-letter table ──────────────
-- One row per payload EVER seen (loaded, skipped replay, superseded, or rejected).
-- Holds the raw payload verbatim so any historical state can be rebuilt and every
-- rejection is retained for the lab to inspect (nothing is dropped silently).
CREATE TABLE IF NOT EXISTS sample_payload_log (
    payload_id        VARCHAR(64) PRIMARY KEY,
    vendor_sample_id  VARCHAR(128),
    api_version       VARCHAR(10),
    source_system     VARCHAR(64),
    received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingest_status     VARCHAR(16) NOT NULL
                      CHECK (ingest_status IN ('loaded', 'skipped', 'superseded', 'rejected')),
    error_detail      JSONB,        -- structured validation errors for rejected payloads
    raw_payload       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payload_log_vendor ON sample_payload_log (vendor_sample_id);
CREATE INDEX IF NOT EXISTS idx_payload_log_status ON sample_payload_log (ingest_status);

-- ── 4. Legacy consumer contract ──────────────────────────────────────────────
-- Downstream R / dashboards should read this pinned column list rather than `SELECT *`,
-- so future additive columns can never surprise a fixed-width consumer.
CREATE OR REPLACE VIEW v_sample_legacy AS
SELECT sample_id, subject_id, study_id, tissue, assay_type, qc_passed,
       total_reads, pct_mapped, rin_score, created_at
FROM sample;

-- NOTE: `sample_expression` intentionally needs NO change. `gene_symbol` from the payload is
-- deliberately NOT stored here, a gene symbol is annotation, not measurement, and belongs in a
-- `dim_gene` keyed by gene_model release (see docs/gold_star_schema.md). Storing it per-row would
-- duplicate mutable annotation across millions of fact rows.

COMMIT;
