"""Loader, the only stage that touches the database.

One transaction per payload. The flow (see docs/DECISIONS.md → D2, D3):

    with tx:
        if payload_id already seen        -> SKIPPED  (true replay, safe orchestrator retry)
        validate                          -> on error: write dead-letter row, REJECTED
        upsert sample (ON CONFLICT ...     WHERE newer)
            no row updated (older re-send) -> SKIPPED
            inserted                       -> LOADED
            updated                        -> LOADED (+ mark prior payload SUPERSEDED, log diff)
        replace expression rows for sample_id
        append to sample_payload_log

Everything is all-or-nothing: nothing is written unless the whole payload is valid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .models import parse_payload, rejection_report
from .normalise import NormalisedSample, normalise

# Columns written on both INSERT and (except the conflict key) UPDATE.
_SAMPLE_COLUMNS = [
    "vendor_sample_id", "payload_id", "api_version", "subject_id", "study_id",
    "tissue", "assay_type", "qc_passed", "total_reads", "pct_mapped", "rin_score",
    "pct_duplication", "median_cv_coverage", "n_cells_detected", "median_genes_per_cell",
    "median_umi_per_cell", "pct_mito", "doublet_rate", "quantification_tool",
    "quantification_tool_version", "sequencing_batch", "cro", "instrument_model",
    "matrix_url", "gene_model", "vendor_generated_at", "lab_notes", "extra_metadata",
    "ingested_by", "source_row_hash",
]

# QC fields whose changes we surface in the "superseded" log diff.
_DIFF_FIELDS = [
    "pct_mapped", "rin_score", "pct_duplication", "median_cv_coverage",
    "total_reads", "qc_passed", "quantification_tool_version",
]


@dataclass
class IngestOutcome:
    """Result of ingesting a single payload."""

    payload_id: str | None
    vendor_sample_id: str | None
    status: str  # loaded | skipped | superseded | rejected
    reason: str | None = None
    sample_id: int | None = None
    inserted: bool | None = None
    expression_rows: int = 0
    duration_ms: float = 0.0
    diff: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None  # structured errors when rejected

    @property
    def ok(self) -> bool:
        """True unless the payload was rejected (drives the CLI exit code)."""
        return self.status != "rejected"


def _jsonable(v: Any) -> Any:
    """Coerce DB scalars (NUMERIC → Decimal) to JSON-friendly types for the diff/log."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _build_upsert_sql() -> str:
    cols = ", ".join(_SAMPLE_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _SAMPLE_COLUMNS)
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _SAMPLE_COLUMNS if c != "vendor_sample_id"
    )
    return (
        f"INSERT INTO sample ({cols}, created_at, updated_at) "
        f"VALUES ({placeholders}, NOW(), NOW()) "
        f"ON CONFLICT (vendor_sample_id) DO UPDATE SET {updates}, updated_at = NOW() "
        # Upsert-if-newer guard: an out-of-order older re-delivery must not overwrite.
        f"WHERE EXCLUDED.vendor_generated_at > sample.vendor_generated_at "
        f"RETURNING sample_id, (xmax = 0) AS inserted"
    )


_UPSERT_SQL = _build_upsert_sql()


def _sample_params(ns: NormalisedSample, actor: str) -> dict[str, Any]:
    params = {c: getattr(ns, c) for c in _SAMPLE_COLUMNS if c not in {"extra_metadata", "ingested_by"}}
    params["extra_metadata"] = Jsonb(ns.extra_metadata)
    params["ingested_by"] = actor
    return params


def _fetch_existing_diff_row(cur, vendor_sample_id: str) -> dict[str, Any] | None:
    cols = ", ".join(_DIFF_FIELDS)
    cur.execute(
        f"SELECT {cols} FROM sample WHERE vendor_sample_id = %s", (vendor_sample_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(_DIFF_FIELDS, row))


def ingest_payload(
    conn: psycopg.Connection,
    raw: dict[str, Any],
    actor: str,
    logger,
) -> IngestOutcome:
    """Ingest one raw payload dict. Returns an :class:`IngestOutcome`; never partially writes."""
    start = time.perf_counter()
    payload_id = raw.get("payload_id")
    vendor_sample_id = (raw.get("sample") or {}).get("vendor_sample_id")
    log = logger.bind(payload_id=payload_id, vendor_sample_id=vendor_sample_id)

    def _elapsed() -> float:
        return round((time.perf_counter() - start) * 1000, 2)

    with conn.transaction():
        with conn.cursor() as cur:
            # ── 1. Exact replay? Same payload_id already recorded → no-op. ─────────
            cur.execute(
                "SELECT ingest_status FROM sample_payload_log WHERE payload_id = %s",
                (payload_id,),
            )
            if cur.fetchone() is not None:
                log.info("replay_ignored", duration_ms=_elapsed())
                return IngestOutcome(payload_id, vendor_sample_id, "skipped",
                                     reason="replay", duration_ms=_elapsed())

            # ── 2. Validate. On failure: dead-letter row only, reject atomically. ──
            try:
                payload = parse_payload(raw)
            except ValidationError as exc:
                report = rejection_report(raw, exc)
                cur.execute(
                    "INSERT INTO sample_payload_log "
                    "(payload_id, vendor_sample_id, api_version, source_system, "
                    " ingest_status, error_detail, raw_payload) "
                    "VALUES (%s, %s, %s, %s, 'rejected', %s, %s)",
                    (payload_id, vendor_sample_id, raw.get("api_version"),
                     (raw.get("sample") or {}).get("cro")
                     or (raw.get("sample") or {}).get("sequencing_centre"),
                     Jsonb(report), Jsonb(raw)),
                )
                log.warning("payload_rejected", error_count=report["error_count"],
                            errors=report["errors"], duration_ms=_elapsed())
                return IngestOutcome(payload_id, vendor_sample_id, "rejected",
                                     report=report, duration_ms=_elapsed())

            ns = normalise(payload)

            # Snapshot the current row (if any) so we can diff a supersede.
            prev = _fetch_existing_diff_row(cur, ns.vendor_sample_id)

            # ── 3. Upsert-if-newer. ───────────────────────────────────────────────
            cur.execute(_UPSERT_SQL, _sample_params(ns, actor))
            upsert_row = cur.fetchone()

            if upsert_row is None:
                # Conflict existed but the guard rejected it → this payload is older/equal.
                cur.execute(
                    "INSERT INTO sample_payload_log "
                    "(payload_id, vendor_sample_id, api_version, source_system, "
                    " ingest_status, raw_payload) VALUES (%s, %s, %s, %s, 'skipped', %s)",
                    (payload_id, ns.vendor_sample_id, ns.api_version, ns.source_system, Jsonb(raw)),
                )
                log.warning("superseded_ignored", reason="not_newer_than_current",
                            duration_ms=_elapsed())
                return IngestOutcome(payload_id, ns.vendor_sample_id, "skipped",
                                     reason="not_newer", duration_ms=_elapsed())

            sample_id, inserted = upsert_row

            diff: dict[str, Any] = {}
            if not inserted:
                # A newer re-delivery replaced the current row. Compute the QC diff and
                # demote the previously-applied payload(s) for this sample to 'superseded'.
                new_vals = {f: getattr(ns, f) for f in _DIFF_FIELDS}
                if prev:
                    diff = {
                        f: {"old": _jsonable(prev[f]), "new": _jsonable(new_vals[f])}
                        for f in _DIFF_FIELDS
                        if str(prev[f]) != str(new_vals[f])
                    }
                cur.execute(
                    "UPDATE sample_payload_log SET ingest_status = 'superseded' "
                    "WHERE vendor_sample_id = %s AND ingest_status = 'loaded'",
                    (ns.vendor_sample_id,),
                )
                log.warning("sample_superseded", sample_id=sample_id, diff=diff)

            # ── 4. Replace expression rows (delete-then-insert; new run may cover a
            #        different gene set, so per-gene upsert could leave orphans). ─────
            cur.execute("DELETE FROM sample_expression WHERE sample_id = %s", (sample_id,))
            if ns.expression:
                cur.executemany(
                    "INSERT INTO sample_expression (sample_id, gene_id, raw_counts, tpm) "
                    "VALUES (%s, %s, %s, %s)",
                    [(sample_id, gid, rc, tpm) for (gid, rc, tpm) in ns.expression],
                )

            # ── 5. Append to the audit / payload log. ─────────────────────────────
            cur.execute(
                "INSERT INTO sample_payload_log "
                "(payload_id, vendor_sample_id, api_version, source_system, "
                " ingest_status, raw_payload) VALUES (%s, %s, %s, %s, 'loaded', %s)",
                (payload_id, ns.vendor_sample_id, ns.api_version, ns.source_system, Jsonb(raw)),
            )

            log.info(
                "payload_loaded",
                sample_id=sample_id,
                inserted=inserted,
                expression_rows=len(ns.expression),
                duration_ms=_elapsed(),
            )
            return IngestOutcome(
                payload_id, ns.vendor_sample_id,
                status="loaded",
                reason="insert" if inserted else "update",
                sample_id=sample_id,
                inserted=inserted,
                expression_rows=len(ns.expression),
                duration_ms=_elapsed(),
                diff=diff,
            )
