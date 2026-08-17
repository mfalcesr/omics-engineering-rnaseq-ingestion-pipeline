"""Pydantic v2 models, the parse + validate stages (pure, no database).

Design notes (see docs/DECISIONS.md → D3, D4):

* **Strict integer types.** ``total_reads`` and ``raw_counts`` are ``StrictInt`` so a vendor
  string like ``"52381204"`` is *rejected* rather than silently coerced (lax Pydantic would
  hide the bug). Numeric-but-fractional fields accept ``int`` or ``float`` but never ``str``
  via the ``StrictNumber`` union.
* **Accumulate all errors.** Nothing short-circuits: one ``ValidationError`` carries every
  problem, and ``err["loc"]`` yields the JSON path the requirements ask for
  (e.g. ``expression.inline_vector.0.raw_counts``).
* **Declarative aliases.** Field-name variation across CROs (``cro`` vs ``sequencing_centre``,
  ``pct_mapped`` vs ``percent_mapped``) is handled by ``AliasChoices``, onboarding a CRO is a
  data change, not a code change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
)

# A number that may be integer or fractional, but must NOT be a string.
# (StrictInt|StrictFloat rejects "94.7"; plain float would coerce it.)
StrictNumber = Union[StrictInt, StrictFloat]

GENE_ID_PATTERN = r"^ENSG\d{11}$"


class QCMetrics(BaseModel):
    """QC block. Superset across bulk + single-cell; per-assay fields are nullable.

    ``extra="allow"`` captures vendor-specific QC keys (e.g. ``insert_size_median``) that we
    route to ``extra_metadata`` rather than dropping.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    total_reads: StrictInt | None = None
    pct_mapped: StrictNumber | None = Field(
        default=None,
        validation_alias=AliasChoices("pct_mapped", "percent_mapped"),
    )
    rin_score: StrictNumber | None = None
    pct_duplication: StrictNumber | None = None
    median_cv_coverage: StrictNumber | None = None
    # Single-cell specific
    n_cells_detected: StrictInt | None = None
    median_genes_per_cell: StrictInt | None = None
    median_umi_per_cell: StrictInt | None = None
    pct_mito: StrictNumber | None = None
    doublet_rate: StrictNumber | None = None

    @field_validator("pct_mapped")
    @classmethod
    def _pct_mapped_in_range(cls, v: float | None) -> float | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("pct_mapped must be between 0 and 100 (percentage of reads mapped)")
        return v


class ExpressionRow(BaseModel):
    """One gene of the inline expression vector."""

    model_config = ConfigDict(extra="allow")

    gene_id: str = Field(pattern=GENE_ID_PATTERN)  # empty / non-Ensembl id → rejected
    gene_symbol: str | None = None
    raw_counts: StrictInt = Field(ge=0)  # negative counts are physically invalid
    tpm: StrictNumber | None = None  # null for scRNA-seq


class SampleBlock(BaseModel):
    """The ``sample`` object of a vendor payload."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    vendor_sample_id: str
    subject_id: str  # required, a missing value is a hard rejection
    study_id: str
    tissue: str
    assay_type: Literal["bulk_rnaseq", "scrna_seq"]  # "wgs" etc. rejected
    qc_passed: bool | None = None
    qc_metrics: QCMetrics = Field(default_factory=QCMetrics)
    library_prep: dict[str, Any] | None = None
    sequencing_batch: str | None = None
    cro: str | None = Field(
        default=None,
        validation_alias=AliasChoices("cro", "sequencing_centre", "sequencing_center"),
    )
    instrument_model: str | None = None
    lab_notes: str | None = None


class ExpressionBlock(BaseModel):
    """The ``expression`` object of a vendor payload."""

    model_config = ConfigDict(extra="allow")

    matrix_url: str | None = None
    gene_model: str | None = None
    quantification_tool: str | None = None
    inline_vector: list[ExpressionRow]


class VendorPayload(BaseModel):
    """Top-level vendor API payload.

    ``extra="ignore"`` deliberately drops unknown top-level keys such as the ``_comment``
    hints embedded in the fixtures, a production parser ignores annotations it does not model.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    api_version: str
    payload_id: str
    generated_at: datetime
    sample: SampleBlock
    expression: ExpressionBlock


# ── Parsing + structured error reporting ─────────────────────────────────────


def parse_payload(raw: dict[str, Any]) -> VendorPayload:
    """Validate a raw dict into a :class:`VendorPayload`.

    Raises :class:`pydantic.ValidationError` accumulating *all* problems at once.
    """
    return VendorPayload.model_validate(raw)


def format_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Turn a ``ValidationError`` into a machine-readable, actionable error list.

    Each entry: ``{path, rule, value, message}`` where ``path`` is the JSON location
    (e.g. ``expression.inline_vector.0.raw_counts``).
    """
    errors: list[dict[str, Any]] = []
    for err in exc.errors():
        rule = err["type"]
        # For "missing" the input is the parent object, which is noise: report null instead.
        value = None if rule == "missing" else err.get("input")
        errors.append(
            {
                "path": ".".join(str(p) for p in err["loc"]),
                "rule": rule,
                "value": value,
                "message": err["msg"],
            }
        )
    return errors


def rejection_report(
    raw: dict[str, Any], exc: ValidationError
) -> dict[str, Any]:
    """Build the structured rejection document written to the dead-letter log and stdout."""
    errors = format_validation_errors(exc)
    sample = raw.get("sample") or {}
    return {
        "payload_id": raw.get("payload_id"),
        "vendor_sample_id": sample.get("vendor_sample_id"),
        "status": "rejected",
        "error_count": len(errors),
        "errors": errors,
    }
