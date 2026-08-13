-- Legacy schema (pre-API-standardisation baseline)
--
-- This reflects the state of the database before the sequencing-vendor API
-- ingestion pipeline was built. Data was historically entered via ad-hoc
-- Python and R scripts.
--
-- Note: new columns, indexes, and tables may be added freely; existing columns
-- are not renamed or dropped, because downstream R scripts and dashboards
-- depend on them.

-- ── Core tables ──────────────────────────────────────────────────────────────

CREATE TABLE sample (
    sample_id                  SERIAL       PRIMARY KEY,
    vendor_sample_id           VARCHAR(128) UNIQUE,
    payload_id                 VARCHAR(64)  NOT NULL,
    api_version                VARCHAR(10),
    subject_id                 VARCHAR(64)  NOT NULL,
    study_id                   VARCHAR(64)  NOT NULL,
    tissue                     VARCHAR(128) NOT NULL,
    assay_type                 VARCHAR(64)  NOT NULL CHECK (assay_type IN ('bulk_rnaseq', 'scrna_seq')),
    qc_passed                  BOOLEAN,
    total_reads                BIGINT,
    pct_mapped                 NUMERIC(5,2) CHECK (pct_mapped >= 0 AND pct_mapped <= 100),
    rin_score                  NUMERIC(3,1),
    pct_duplication            NUMERIC(5,2),
    median_cv_coverage         NUMERIC(5,2),
    -- Single-cell specific QC metrics
    n_cells_detected           INTEGER,
    median_genes_per_cell      INTEGER,
    median_umi_per_cell        INTEGER,
    pct_mito                   NUMERIC(5,2),
    doublet_rate               NUMERIC(5,4),
    -- Pipeline metadata
    quantification_tool        VARCHAR(128),
    quantification_tool_version VARCHAR(32),
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sample_study           ON sample(study_id);
CREATE INDEX idx_sample_subject         ON sample(subject_id);
CREATE INDEX idx_sample_vendor_id       ON sample(vendor_sample_id);
CREATE INDEX idx_sample_payload_id      ON sample(payload_id);
CREATE UNIQUE INDEX idx_sample_idempotency ON sample(vendor_sample_id, payload_id);

CREATE TABLE sample_expression (
    id         BIGSERIAL    PRIMARY KEY,
    sample_id  INTEGER      NOT NULL REFERENCES sample(sample_id) ON DELETE CASCADE,
    gene_id    VARCHAR(32)  NOT NULL,
    raw_counts INTEGER      NOT NULL,
    tpm        NUMERIC(12,4),
    UNIQUE (sample_id, gene_id)
);

CREATE INDEX idx_expr_gene ON sample_expression(gene_id);

-- ── Seed data ────────────────────────────────────────────────────────────────
-- Five samples curated manually before the API was standardised.
-- These represent the pre-pipeline baseline and have no vendor_sample_id
-- or raw_payload; those are new concepts the pipeline introduces.

INSERT INTO sample (vendor_sample_id, payload_id, api_version, subject_id, study_id, tissue, assay_type, qc_passed, total_reads, pct_mapped, created_at, updated_at)
VALUES
    (NULL, 'legacy_seed_001', '1.0', 'SUBJ-0001', 'PRJ-RD-001', 'lung',                 'bulk_rnaseq', true,  48241033,  91.2, '2024-03-12 08:41:00+00', '2024-03-12 08:41:00+00'),
    (NULL, 'legacy_seed_002', '1.0', 'SUBJ-0002', 'PRJ-RD-001', 'lung',                 'bulk_rnaseq', true,  51298741,  93.4, '2024-03-13 11:05:00+00', '2024-03-13 11:05:00+00'),
    (NULL, 'legacy_seed_003', '1.0', 'SUBJ-0003', 'PRJ-IO-003', 'peripheral_blood',     'bulk_rnaseq', false,  8210044,  62.1, '2024-06-01 14:22:00+00', '2024-06-01 14:22:00+00'),
    (NULL, 'legacy_seed_004', '1.0', 'SUBJ-0004', 'PRJ-IO-003', 'tumor_biopsy',         'bulk_rnaseq', true,  55138921,  95.8, '2024-06-02 09:17:00+00', '2024-06-02 09:17:00+00'),
    (NULL, 'legacy_seed_005', '1.0', 'SUBJ-0005', 'PRJ-RD-002', 'liver',                'bulk_rnaseq', true,  49803217,  92.7, '2024-09-18 16:44:00+00', '2024-09-18 16:44:00+00');

INSERT INTO sample_expression (sample_id, gene_id, raw_counts, tpm)
VALUES
    -- SUBJ-0001 (PRJ-RD-001, lung)
    (1, 'ENSG00000141510',  892,  35.61),   -- TP53
    (1, 'ENSG00000012048',  211,   8.42),   -- BRCA1
    (1, 'ENSG00000139618',  445,  17.76),   -- BRCA2
    (1, 'ENSG00000171862', 1302,  51.97),   -- PTEN
    (1, 'ENSG00000136997', 2041,  81.44),   -- MYC
    -- SUBJ-0002 (PRJ-RD-001, lung)
    (2, 'ENSG00000141510', 1103,  42.11),
    (2, 'ENSG00000012048',  298,  11.38),
    (2, 'ENSG00000139618',  567,  21.63),
    (2, 'ENSG00000171862', 1491,  56.88),
    (2, 'ENSG00000136997', 2344,  89.42),
    -- SUBJ-0004 (PRJ-IO-003, tumor_biopsy) — SUBJ-0003 failed QC, no expression rows
    (4, 'ENSG00000141510', 3211, 112.44),
    (4, 'ENSG00000012048',  789,  27.63),
    (4, 'ENSG00000139618',  923,  32.31),
    (4, 'ENSG00000171862',  445,  15.58),
    (4, 'ENSG00000136997', 4521, 158.31);
