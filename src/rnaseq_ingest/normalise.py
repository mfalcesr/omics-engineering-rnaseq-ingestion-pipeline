"""Normalisation, the third pure stage.

Takes a validated :class:`VendorPayload` and produces a flat, database-ready
:class:`NormalisedSample`:

* promoted fields → typed columns,
* everything else (library prep, vendor-specific QC keys, unmodelled sample keys) → a single
  ``extra_metadata`` dict that lands in the JSONB catch-all column,
* the quantification tool string is split into tool name + version,
* a ``source_row_hash`` is computed for cheap change detection.

No database access here, this keeps parse/validate/normalise unit-testable without Postgres
and reusable inside the target platform.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import VendorPayload

# Splits "STAR+featureCounts v2.2.1" → ("STAR+featureCounts", "2.2.1")
#    and "Cell Ranger 7.1.0"        → ("Cell Ranger", "7.1.0")
_TOOL_VERSION_RE = re.compile(r"^(?P<tool>.+?)[\s]+v?(?P<version>\d[\w.\-]*)$")


@dataclass
class NormalisedSample:
    """Flat, DB-ready representation of one payload."""

    # Identity / provenance
    vendor_sample_id: str
    payload_id: str
    api_version: str
    vendor_generated_at: datetime
    source_system: str | None
    # Core metadata (legacy columns)
    subject_id: str
    study_id: str
    tissue: str
    assay_type: str
    qc_passed: bool | None
    # QC metrics
    total_reads: int | None
    pct_mapped: float | None
    rin_score: float | None
    pct_duplication: float | None
    median_cv_coverage: float | None
    n_cells_detected: int | None
    median_genes_per_cell: int | None
    median_umi_per_cell: int | None
    pct_mito: float | None
    doublet_rate: float | None
    # Pipeline metadata
    quantification_tool: str | None
    quantification_tool_version: str | None
    # Promoted evolution columns
    sequencing_batch: str | None
    cro: str | None
    instrument_model: str | None
    matrix_url: str | None
    gene_model: str | None
    lab_notes: str | None
    # Catch-all + change detection
    extra_metadata: dict[str, Any] = field(default_factory=dict)
    source_row_hash: str = ""
    # Expression rows: list of (gene_id, raw_counts, tpm)
    expression: list[tuple[str, int, float | None]] = field(default_factory=list)


def split_tool_version(tool_string: str | None) -> tuple[str | None, str | None]:
    """Split a combined tool string into (name, version)."""
    if not tool_string:
        return None, None
    m = _TOOL_VERSION_RE.match(tool_string.strip())
    if not m:
        return tool_string.strip(), None
    return m.group("tool").strip(), m.group("version")


def _build_extra_metadata(payload: VendorPayload) -> dict[str, Any]:
    """Assemble the JSONB catch-all from unmodelled/tail fields.

    Kept flat and human-readable so a bioinformatician can query it via ``->>``.
    """
    extra: dict[str, Any] = {}
    sample = payload.sample

    if sample.library_prep:
        extra["library_prep"] = sample.library_prep

    # Unmodelled sample-level keys (e.g. flow_cell_id from SeqCore Labs).
    for k, v in (sample.model_extra or {}).items():
        extra[k] = v

    # Vendor-specific QC keys we do not promote (e.g. insert_size_median).
    for k, v in (payload.sample.qc_metrics.model_extra or {}).items():
        extra[k] = v

    # Unmodelled expression-block keys (rare; kept for losslessness).
    expr_extra = payload.expression.model_extra or {}
    if expr_extra:
        extra["expression_extra"] = expr_extra

    return extra


def _hash_source(sample_fields: dict[str, Any], expression: list[tuple]) -> str:
    """Deterministic sha256 over the normalised content, for change detection."""
    canonical = json.dumps(
        {"sample": sample_fields, "expression": expression},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalise(payload: VendorPayload) -> NormalisedSample:
    """Flatten a validated payload into a :class:`NormalisedSample`."""
    s = payload.sample
    qc = s.qc_metrics
    expr = payload.expression

    tool, tool_version = split_tool_version(expr.quantification_tool)
    extra_metadata = _build_extra_metadata(payload)
    expression_rows = [(r.gene_id, r.raw_counts, r.tpm) for r in expr.inline_vector]

    ns = NormalisedSample(
        vendor_sample_id=s.vendor_sample_id,
        payload_id=payload.payload_id,
        api_version=payload.api_version,
        vendor_generated_at=payload.generated_at,
        source_system=s.cro,
        subject_id=s.subject_id,
        study_id=s.study_id,
        tissue=s.tissue,
        assay_type=s.assay_type,
        qc_passed=s.qc_passed,
        total_reads=qc.total_reads,
        pct_mapped=qc.pct_mapped,
        rin_score=qc.rin_score,
        pct_duplication=qc.pct_duplication,
        median_cv_coverage=qc.median_cv_coverage,
        n_cells_detected=qc.n_cells_detected,
        median_genes_per_cell=qc.median_genes_per_cell,
        median_umi_per_cell=qc.median_umi_per_cell,
        pct_mito=qc.pct_mito,
        doublet_rate=qc.doublet_rate,
        quantification_tool=tool,
        quantification_tool_version=tool_version,
        sequencing_batch=s.sequencing_batch,
        cro=s.cro,
        instrument_model=s.instrument_model,
        matrix_url=expr.matrix_url,
        gene_model=expr.gene_model,
        lab_notes=s.lab_notes,
        extra_metadata=extra_metadata,
        expression=expression_rows,
    )

    # Hash over the salient content (identity excluded so re-deliveries with the same data hash equal).
    ns.source_row_hash = _hash_source(
        {
            "vendor_sample_id": ns.vendor_sample_id,
            "subject_id": ns.subject_id,
            "study_id": ns.study_id,
            "tissue": ns.tissue,
            "assay_type": ns.assay_type,
            "qc_passed": ns.qc_passed,
            "total_reads": ns.total_reads,
            "pct_mapped": ns.pct_mapped,
            "quantification_tool_version": ns.quantification_tool_version,
        },
        expression_rows,
    )
    return ns
