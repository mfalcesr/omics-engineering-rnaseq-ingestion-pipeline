"""Integration tests, exercise the loader against a real PostgreSQL database.

Run with Docker up:  `docker compose up -d` then `pytest -m integration`.
Skipped automatically if the database is unreachable.
"""

from __future__ import annotations

import pytest

from rnaseq_ingest.load import ingest_payload
from rnaseq_ingest.logging_conf import get_logger

from .conftest import SEED_SAMPLE_COUNT, load_payload

pytestmark = pytest.mark.integration

_LOG = get_logger()


def _ingest(conn, name):
    return ingest_payload(conn, load_payload(name), actor="pytest", logger=_LOG)


def _scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


# -- Happy path + schema evolution ────────────────────────────────────


def test_happy_path_loads_one_sample_and_15_genes(db):
    out = _ingest(db, "RNA-BULK-006.json")
    assert out.status == "loaded" and out.inserted is True
    assert out.expression_rows == 15

    assert _scalar(db, "SELECT COUNT(*) FROM sample") == SEED_SAMPLE_COUNT + 1
    row = db.execute(
        "SELECT pct_mapped, assay_type, cro, sequencing_batch, gene_model "
        "FROM sample WHERE vendor_sample_id = 'RNA-BULK-006'"
    ).fetchone()
    assert float(row[0]) == 94.7
    assert row[1] == "bulk_rnaseq"
    assert row[2] == "GenomicsFirst"
    assert row[3] == "BATCH-2025-Q4-01"
    assert row[4] == "GRCh38/Ensembl110"
    assert _scalar(db, "SELECT COUNT(*) FROM sample_expression WHERE sample_id = %s",
                   (out.sample_id,)) == 15
    assert _scalar(db, "SELECT COUNT(*) FROM sample_payload_log "
                       "WHERE vendor_sample_id = 'RNA-BULK-006' AND ingest_status = 'loaded'") == 1


def test_second_cro_field_aliases_persist(db):
    out = _ingest(db, "RNA-BULK-007.json")
    assert out.status == "loaded"
    row = db.execute(
        "SELECT pct_mapped, cro, instrument_model, "
        "       extra_metadata->>'flow_cell_id', extra_metadata->>'insert_size_median' "
        "FROM sample WHERE vendor_sample_id = 'RNA-BULK-007'"
    ).fetchone()
    assert float(row[0]) == 96.1          # percent_mapped → pct_mapped
    assert row[1] == "SeqCore Labs"        # sequencing_centre → cro
    assert row[2] == "Illumina NovaSeq 6000"
    assert row[3] == "HC5JLDRXY"           # tail field queryable from JSONB
    assert row[4] == "183"


def test_single_cell_has_null_tpm_and_cell_qc(db):
    out = _ingest(db, "RNA-SC-008.json")
    assert out.status == "loaded" and out.expression_rows == 15
    # All TPM null for scRNA-seq.
    assert _scalar(db, "SELECT COUNT(*) FROM sample_expression "
                       "WHERE sample_id = %s AND tpm IS NOT NULL", (out.sample_id,)) == 0
    row = db.execute(
        "SELECT rin_score, n_cells_detected, median_genes_per_cell, pct_mito, doublet_rate "
        "FROM sample WHERE vendor_sample_id = 'RNA-SC-008'"
    ).fetchone()
    assert row[0] is None                  # no RIN for single-cell
    assert row[1] == 4812
    assert row[2] == 2341
    assert float(row[3]) == 4.2
    assert float(row[4]) == 0.031


# -- Idempotency ──────────────────────────────────────────────────────


def test_duplicate_upserts_in_place_and_supersedes(db):
    _ingest(db, "RNA-BULK-006.json")
    out2 = _ingest(db, "RNA-BULK-006_duplicate.json")
    assert out2.status == "loaded" and out2.inserted is False

    # Still exactly one row for the sample: no duplicate.
    assert _scalar(db, "SELECT COUNT(*) FROM sample WHERE vendor_sample_id = 'RNA-BULK-006'") == 1
    row = db.execute(
        "SELECT pct_mapped, quantification_tool_version, matrix_url "
        "FROM sample WHERE vendor_sample_id = 'RNA-BULK-006'"
    ).fetchone()
    assert float(row[0]) == 94.9                        # revised QC won
    assert row[1] == "2.2.2"                            # reprocessed tool version
    assert row[2].endswith("expression_matrix_v2.h5")  # revised matrix location

    # Expression replaced with the new counts (TP53 1245 → 1247), still 15 rows.
    assert _scalar(db, "SELECT COUNT(*) FROM sample_expression WHERE sample_id = %s",
                   (out2.sample_id,)) == 15
    assert _scalar(db, "SELECT raw_counts FROM sample_expression "
                       "WHERE sample_id = %s AND gene_id = 'ENSG00000141510'",
                   (out2.sample_id,)) == 1247

    # The diff was captured, and both payloads are logged with distinct statuses.
    assert out2.diff["pct_mapped"] == {"old": 94.7, "new": 94.9} or \
        (float(out2.diff["pct_mapped"]["old"]) == 94.7 and float(out2.diff["pct_mapped"]["new"]) == 94.9)
    statuses = {r[0] for r in db.execute(
        "SELECT ingest_status FROM sample_payload_log WHERE vendor_sample_id = 'RNA-BULK-006'"
    ).fetchall()}
    assert statuses == {"loaded", "superseded"}


def test_true_replay_same_payload_is_noop(db):
    _ingest(db, "RNA-BULK-006_duplicate.json")
    before_updated = _scalar(db, "SELECT updated_at FROM sample WHERE vendor_sample_id = 'RNA-BULK-006'")
    out = _ingest(db, "RNA-BULK-006_duplicate.json")  # exact same payload_id again
    assert out.status == "skipped" and out.reason == "replay"
    after_updated = _scalar(db, "SELECT updated_at FROM sample WHERE vendor_sample_id = 'RNA-BULK-006'")
    assert before_updated == after_updated  # nothing changed
    assert _scalar(db, "SELECT COUNT(*) FROM sample_payload_log "
                       "WHERE vendor_sample_id = 'RNA-BULK-006'") == 1


def test_out_of_order_older_delivery_is_ignored(db):
    _ingest(db, "RNA-BULK-006_duplicate.json")   # newer (generated 2025-12-19)
    out = _ingest(db, "RNA-BULK-006.json")        # older (generated 2025-12-18)
    assert out.status == "skipped" and out.reason == "not_newer"
    # Current row still reflects the newer payload.
    assert float(_scalar(db, "SELECT pct_mapped FROM sample WHERE vendor_sample_id = 'RNA-BULK-006'")) == 94.9


# -- Atomic rejection ─────────────────────────────────────────────────


def test_malformed_rejected_atomically(db):
    before = _scalar(db, "SELECT COUNT(*) FROM sample")
    out = _ingest(db, "RNA-BULK-009_malformed.json")
    assert out.status == "rejected"
    assert out.report["error_count"] == 6

    # Database is byte-identical apart from the dead-letter row: no sample written.
    assert _scalar(db, "SELECT COUNT(*) FROM sample") == before
    assert _scalar(db, "SELECT COUNT(*) FROM sample WHERE vendor_sample_id = 'RNA-BULK-009'") == 0
    dead = db.execute(
        "SELECT ingest_status, error_detail->>'error_count' FROM sample_payload_log "
        "WHERE payload_id = 'pay_20251221_zz000001'"
    ).fetchone()
    assert dead[0] == "rejected" and dead[1] == "6"


# ── Legacy contract preserved ────────────────────────────────────────────────


def test_legacy_rows_untouched_and_view_present(db):
    for name in ("RNA-BULK-006.json", "RNA-BULK-007.json", "RNA-SC-008.json"):
        _ingest(db, name)
    assert _scalar(db, "SELECT COUNT(*) FROM v_sample_legacy") == SEED_SAMPLE_COUNT + 3
    # A legacy seed row is unchanged.
    row = db.execute(
        "SELECT subject_id, pct_mapped FROM sample WHERE payload_id = 'legacy_seed_001'"
    ).fetchone()
    assert row[0] == "SUBJ-0001" and float(row[1]) == 91.2
