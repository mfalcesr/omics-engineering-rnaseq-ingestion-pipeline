"""Pure unit tests for parse + normalise, no database required."""

from __future__ import annotations

import pytest

from rnaseq_ingest.models import parse_payload
from rnaseq_ingest.normalise import normalise, split_tool_version

from .conftest import load_payload

VALID_FIXTURES = [
    "RNA-BULK-006.json",
    "RNA-BULK-007.json",
    "RNA-SC-008.json",
    "RNA-BULK-006_duplicate.json",
]


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_valid_payloads_parse(name):
    payload = parse_payload(load_payload(name))
    assert payload.payload_id
    assert len(payload.expression.inline_vector) == 15


def test_happy_path_normalisation():
    ns = normalise(parse_payload(load_payload("RNA-BULK-006.json")))
    assert ns.vendor_sample_id == "RNA-BULK-006"
    assert ns.cro == "GenomicsFirst"
    assert ns.source_system == "GenomicsFirst"
    assert ns.pct_mapped == 94.7
    assert ns.total_reads == 52381204
    assert ns.assay_type == "bulk_rnaseq"
    assert ns.quantification_tool == "STAR+featureCounts"
    assert ns.quantification_tool_version == "2.2.1"
    assert ns.gene_model == "GRCh38/Ensembl110"
    assert ns.extra_metadata["library_prep"]["kit"] == "TruSeq Stranded mRNA"
    assert len(ns.expression) == 15


def test_field_name_aliases_second_cro():
    """RNA-BULK-007 uses `sequencing_centre` and `percent_mapped`, must normalise."""
    ns = normalise(parse_payload(load_payload("RNA-BULK-007.json")))
    assert ns.cro == "SeqCore Labs"  # from `sequencing_centre`
    assert ns.pct_mapped == 96.1  # from `percent_mapped`
    assert ns.instrument_model == "Illumina NovaSeq 6000"
    # Unmodelled tail fields are preserved in extra_metadata, not dropped.
    assert ns.extra_metadata["flow_cell_id"] == "HC5JLDRXY"
    assert ns.extra_metadata["insert_size_median"] == 183
    assert ns.extra_metadata["library_prep"]["adapter_trimming_tool"] == "Trimmomatic-0.39"


def test_single_cell_payload():
    """RNA-SC-008: single-cell QC set, null TPM, 401M reads (needs BIGINT)."""
    ns = normalise(parse_payload(load_payload("RNA-SC-008.json")))
    assert ns.assay_type == "scrna_seq"
    assert ns.total_reads == 401823441
    assert ns.rin_score is None
    assert ns.n_cells_detected == 4812
    assert ns.median_genes_per_cell == 2341
    assert ns.pct_mito == 4.2
    assert ns.doublet_rate == 0.031
    assert ns.quantification_tool == "Cell Ranger"
    assert ns.quantification_tool_version == "7.1.0"
    assert all(tpm is None for (_gid, _rc, tpm) in ns.expression)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("STAR+featureCounts v2.2.1", ("STAR+featureCounts", "2.2.1")),
        ("Cell Ranger 7.1.0", ("Cell Ranger", "7.1.0")),
        ("STAR+featureCounts v2.2.2", ("STAR+featureCounts", "2.2.2")),
        (None, (None, None)),
        ("SomeToolNoVersion", ("SomeToolNoVersion", None)),
    ],
)
def test_split_tool_version(raw, expected):
    assert split_tool_version(raw) == expected


def test_reprocessing_hash_differs_from_original():
    """The duplicate has changed QC/counts, so its content hash must differ."""
    original = normalise(parse_payload(load_payload("RNA-BULK-006.json")))
    dup = normalise(parse_payload(load_payload("RNA-BULK-006_duplicate.json")))
    assert original.vendor_sample_id == dup.vendor_sample_id
    assert original.source_row_hash != dup.source_row_hash
