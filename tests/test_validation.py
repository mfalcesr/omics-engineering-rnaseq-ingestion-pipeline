"""Validation tests, the malformed payload must surface all six errors at once."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rnaseq_ingest.models import format_validation_errors, parse_payload, rejection_report

from .conftest import load_payload

# The six intentional errors and the JSON path each should be reported at.
EXPECTED_ERROR_PATHS = {
    "sample.subject_id",                      # 1. missing required field
    "sample.qc_metrics.total_reads",          # 2. string instead of int (StrictInt rejects)
    "sample.qc_metrics.pct_mapped",           # 3. 105.3 > 100
    "sample.assay_type",                      # 4. unsupported "wgs"
    "expression.inline_vector.0.raw_counts",  # 5. negative counts
    "expression.inline_vector.1.gene_id",     # 6. empty gene id
}


@pytest.fixture
def malformed():
    return load_payload("RNA-BULK-009_malformed.json")


def test_malformed_raises_with_all_six_errors(malformed):
    with pytest.raises(ValidationError) as exc_info:
        parse_payload(malformed)
    errors = format_validation_errors(exc_info.value)
    assert len(errors) == 6, f"expected 6 errors, got {len(errors)}: {errors}"
    assert {e["path"] for e in errors} == EXPECTED_ERROR_PATHS


def test_string_total_reads_is_rejected_not_coerced(malformed):
    """The trap: lax Pydantic would coerce "52381204" to int and hide the bug."""
    with pytest.raises(ValidationError) as exc_info:
        parse_payload(malformed)
    total_reads_errors = [
        e for e in format_validation_errors(exc_info.value)
        if e["path"] == "sample.qc_metrics.total_reads"
    ]
    assert len(total_reads_errors) == 1
    assert total_reads_errors[0]["rule"].startswith("int_type")
    assert total_reads_errors[0]["value"] == "52381204"


def test_rejection_report_shape(malformed):
    with pytest.raises(ValidationError) as exc_info:
        parse_payload(malformed)
    report = rejection_report(malformed, exc_info.value)
    assert report["status"] == "rejected"
    assert report["error_count"] == 6
    assert report["payload_id"] == "pay_20251221_zz000001"
    assert report["vendor_sample_id"] == "RNA-BULK-009"
    for err in report["errors"]:
        assert set(err) == {"path", "rule", "value", "message"}


def test_pct_mapped_range_message(malformed):
    with pytest.raises(ValidationError) as exc_info:
        parse_payload(malformed)
    pct = [e for e in format_validation_errors(exc_info.value) if e["path"] == "sample.qc_metrics.pct_mapped"]
    assert pct and "between 0 and 100" in pct[0]["message"]
