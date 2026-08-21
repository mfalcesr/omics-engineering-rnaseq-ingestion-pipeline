"""Shared test fixtures.

* ``fixture_payloads``, loads the vendor JSON fixtures as dicts (no DB needed).
* ``db``, an integration fixture that applies the migration and resets the database to the
  five legacy seed rows before each test, so every DB test starts from a known baseline.

DB tests are marked ``integration`` and skip cleanly if PostgreSQL is unreachable, so the pure
unit tests (``pytest -m 'not integration'``) run with no Docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_DIR = REPO_ROOT / "sources" / "vendor_payloads"
MIGRATION = REPO_ROOT / "db" / "migrations" / "001_extend_sample.sql"

# Legacy seed data: mirrors db/init.sql so we can restore the baseline between tests.
_SEED_SAMPLES = [
    # (vendor_sample_id, payload_id, api_version, subject, study, tissue, assay, qc, reads, pct, created)
    (None, "legacy_seed_001", "1.0", "SUBJ-0001", "PRJ-RD-001", "lung", "bulk_rnaseq", True, 48241033, 91.2, "2024-03-12 08:41:00+00"),
    (None, "legacy_seed_002", "1.0", "SUBJ-0002", "PRJ-RD-001", "lung", "bulk_rnaseq", True, 51298741, 93.4, "2024-03-13 11:05:00+00"),
    (None, "legacy_seed_003", "1.0", "SUBJ-0003", "PRJ-IO-003", "peripheral_blood", "bulk_rnaseq", False, 8210044, 62.1, "2024-06-01 14:22:00+00"),
    (None, "legacy_seed_004", "1.0", "SUBJ-0004", "PRJ-IO-003", "tumor_biopsy", "bulk_rnaseq", True, 55138921, 95.8, "2024-06-02 09:17:00+00"),
    (None, "legacy_seed_005", "1.0", "SUBJ-0005", "PRJ-RD-002", "liver", "bulk_rnaseq", True, 49803217, 92.7, "2024-09-18 16:44:00+00"),
]
_SEED_EXPRESSION = [
    (1, "ENSG00000141510", 892, 35.61), (1, "ENSG00000012048", 211, 8.42),
    (1, "ENSG00000139618", 445, 17.76), (1, "ENSG00000171862", 1302, 51.97),
    (1, "ENSG00000136997", 2041, 81.44),
    (2, "ENSG00000141510", 1103, 42.11), (2, "ENSG00000012048", 298, 11.38),
    (2, "ENSG00000139618", 567, 21.63), (2, "ENSG00000171862", 1491, 56.88),
    (2, "ENSG00000136997", 2344, 89.42),
    (4, "ENSG00000141510", 3211, 112.44), (4, "ENSG00000012048", 789, 27.63),
    (4, "ENSG00000139618", 923, 32.31), (4, "ENSG00000171862", 445, 15.58),
    (4, "ENSG00000136997", 4521, 158.31),
]

SEED_SAMPLE_COUNT = len(_SEED_SAMPLES)


def load_payload(name: str) -> dict:
    """Read a vendor fixture by filename."""
    return json.loads((PAYLOAD_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def fixture_payloads() -> dict[str, dict]:
    """All five fixtures as a name→dict mapping (no database required)."""
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in PAYLOAD_DIR.glob("*.json")}


def apply_migration(conn) -> None:
    """Apply the additive migration statement-by-statement (idempotent).

    We strip ``--`` line comments (some contain semicolons), then split on ``;`` and run each
    statement under autocommit, psycopg3's default protocol rejects multi-statement strings.
    No ``--`` appears inside a string literal in this migration, so this is safe.
    """
    raw = MIGRATION.read_text(encoding="utf-8")
    no_comments = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    for fragment in no_comments.split(";"):
        stripped = fragment.strip()
        if not stripped or stripped.upper() in {"BEGIN", "COMMIT"}:
            continue
        conn.execute(stripped)


def _reset_database(conn) -> None:
    """Truncate the pipeline tables and restore the five legacy seed rows."""
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE sample_payload_log; "
            "TRUNCATE sample_expression, sample RESTART IDENTITY CASCADE;"
        )
        cur.executemany(
            "INSERT INTO sample (vendor_sample_id, payload_id, api_version, subject_id, study_id, "
            "tissue, assay_type, qc_passed, total_reads, pct_mapped, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [s + (s[-1],) for s in _SEED_SAMPLES],  # created_at == updated_at
        )
        cur.executemany(
            "INSERT INTO sample_expression (sample_id, gene_id, raw_counts, tpm) "
            "VALUES (%s, %s, %s, %s)",
            _SEED_EXPRESSION,
        )


@pytest.fixture
def db():
    """Yield a clean, migrated connection reset to the legacy baseline. Skips if no DB."""
    psycopg = pytest.importorskip("psycopg")
    from rnaseq_ingest.config import load_settings

    settings = load_settings()
    try:
        conn = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable ({exc}); start it with `docker compose up -d`.")

    with conn:
        # Ensure the additive migration is applied (idempotent).
        apply_migration(conn)
        _reset_database(conn)
        yield conn
