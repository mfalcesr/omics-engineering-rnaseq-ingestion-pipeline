"""Tests for the read-only API.

Two layers, matching the split the rest of the suite uses:

* **Unit** (the bulk): the database is replaced by :class:`FakeRepository` through
  ``app.dependency_overrides``, and the app is driven with ``httpx.ASGITransport``. That
  transport does not run the lifespan hook, so no connection pool is ever opened and these
  run in milliseconds with no Docker.
* **Integration** (one test): a real async pool against real PostgreSQL, asserting the
  baseline the ``db`` fixture guarantees. Skips cleanly when the database is unreachable.

The fake is a faithful in-memory implementation of the repository's *semantics* (keyset
order, null TPM, missing samples), not a stub that returns canned responses, so a contract
change in the endpoints fails here rather than only in integration.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from rnaseq_ingest.api import app, get_repository, lifespan

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only; the project has no trio dependency."""
    return "asyncio"


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


# ── In-memory test double ────────────────────────────────────────────────────

# Mirrors the shape the SQL returns: the five legacy seed rows (no vendor_sample_id, as in
# db/init.sql) followed by two pipeline-loaded samples, one bulk and one single-cell.
_SAMPLES: list[dict[str, Any]] = [
    {
        "sample_id": i,
        "vendor_sample_id": None,
        "subject_id": f"SUBJ-000{i}",
        "study_id": "PRJ-RD-001",
        "tissue": "lung",
        "assay_type": "bulk_rnaseq",
        "qc_passed": True,
        "total_reads": 48241033,
        "pct_mapped": 91.2,
        "rin_score": None,
        "sequencing_batch": None,
        "cro": None,
        "instrument_model": None,
        "gene_model": None,
        "tool": None,
        "tool_version": None,
        "vendor_generated_at": None,
        "created_at": _dt("2024-03-12 08:41:00"),
        "updated_at": _dt("2024-03-12 08:41:00"),
    }
    for i in range(1, 6)
] + [
    {
        "sample_id": 6,
        "vendor_sample_id": "RNA-BULK-006",
        "subject_id": "SUBJ-0006",
        "study_id": "PRJ-IO-003",
        "tissue": "tumor_biopsy",
        "assay_type": "bulk_rnaseq",
        "qc_passed": True,
        "total_reads": 52381204,
        "pct_mapped": 94.7,
        "rin_score": 8.4,
        "sequencing_batch": "BATCH-2025-Q4-01",
        "cro": "GenomicsFirst",
        "instrument_model": "NovaSeq X",
        "gene_model": "GRCh38/Ensembl110",
        "tool": "salmon",
        "tool_version": "1.10.1",
        "vendor_generated_at": _dt("2025-11-02 06:00:00"),
        "created_at": _dt("2025-11-02 07:00:00"),
        # Deliberately later than created_at: this row was superseded by a newer payload,
        # which is exactly the "current row" the lookup must return.
        "updated_at": _dt("2025-11-09 09:30:00"),
    },
    {
        "sample_id": 7,
        "vendor_sample_id": "RNA-SC-008",
        "subject_id": "SUBJ-0008",
        "study_id": "PRJ-IO-003",
        "tissue": "peripheral_blood",
        "assay_type": "scrna_seq",
        "qc_passed": True,
        "total_reads": 310442099,
        "pct_mapped": 88.1,
        "rin_score": None,
        "sequencing_batch": "BATCH-2025-Q4-02",
        "cro": "CellWorks",
        "instrument_model": "NovaSeq 6000",
        "gene_model": "GRCh38/Ensembl110",
        "tool": "cellranger",
        "tool_version": "7.2.0",
        "vendor_generated_at": _dt("2025-11-05 06:00:00"),
        "created_at": _dt("2025-11-05 07:00:00"),
        "updated_at": _dt("2025-11-05 07:00:00"),
    },
]

_EXPRESSION: dict[str, list[dict[str, Any]]] = {
    "RNA-BULK-006": [
        {"gene_id": "ENSG00000012048", "raw_counts": 211, "tpm": 8.42},
        {"gene_id": "ENSG00000141510", "raw_counts": 892, "tpm": 35.61},
    ],
    # Single-cell ships UMI counts, never TPM. The null must survive serialisation.
    "RNA-SC-008": [
        {"gene_id": "ENSG00000012048", "raw_counts": 47, "tpm": None},
        {"gene_id": "ENSG00000141510", "raw_counts": 193, "tpm": None},
    ],
}

_REJECTED: list[dict[str, Any]] = [
    {
        "payload_id": "pl_009_malformed",
        "vendor_sample_id": "RNA-BULK-009",
        "api_version": "2.1",
        "source_system": "GenomicsFirst",
        "received_at": _dt("2025-11-10 12:00:00"),
        "error_detail": {
            "error_count": 2,
            "errors": [
                {"loc": "qc.pct_mapped", "msg": "pct_mapped must be between 0 and 100"},
                {"loc": "expression.inline_vector.0.raw_counts", "msg": "Input should be valid"},
            ],
        },
    }
]


class FakeRepository:
    """In-memory stand-in with the same semantics as :class:`SampleRepository`."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    async def get_sample(self, vendor_sample_id: str) -> dict[str, Any] | None:
        return next(
            (s for s in _SAMPLES if s["vendor_sample_id"] == vendor_sample_id),
            None,
        )

    async def list_samples(self, after: int, limit: int) -> tuple[list[dict[str, Any]], int | None]:
        ordered = sorted(
            (s for s in _SAMPLES if s["sample_id"] > after),
            key=lambda s: s["sample_id"],
        )
        # Same probe-row trick as the SQL: take limit + 1 and let the helper decide.
        from rnaseq_ingest.api import _paginate

        return _paginate(ordered[: limit + 1], limit)

    async def sample_exists(self, vendor_sample_id: str) -> bool:
        return any(s["vendor_sample_id"] == vendor_sample_id for s in _SAMPLES)

    async def get_expression(self, vendor_sample_id: str) -> list[dict[str, Any]]:
        return list(_EXPRESSION.get(vendor_sample_id, []))

    async def list_rejected(self, limit: int) -> list[dict[str, Any]]:
        return _REJECTED[:limit]

    async def ping(self) -> bool:
        return self.healthy


@pytest.fixture
def client_factory():
    """Build a client whose app is wired to a given fake, cleaning up the override after."""

    def _build(repo: FakeRepository) -> AsyncClient:
        app.dependency_overrides[get_repository] = lambda: repo
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _build
    app.dependency_overrides.clear()


@pytest.fixture
async def client(client_factory):
    """The common case: a healthy fake database."""
    async with client_factory(FakeRepository()) as c:
        yield c


# ── Unit tests ───────────────────────────────────────────────────────────────


async def test_get_sample_returns_current_row(client):
    """Lookup returns the latest upserted row, with storage columns aliased to the contract."""
    resp = await client.get("/samples/RNA-BULK-006")

    assert resp.status_code == 200
    body = resp.json()
    assert body["vendor_sample_id"] == "RNA-BULK-006"
    assert body["study_id"] == "PRJ-IO-003"
    assert body["pct_mapped"] == 94.7
    # `quantification_tool` / `quantification_tool_version` are exposed under stable names.
    assert body["tool"] == "salmon"
    assert body["tool_version"] == "1.10.1"
    # The superseding payload's timestamp, i.e. the current row rather than the first load.
    assert body["updated_at"].startswith("2025-11-09")


async def test_unknown_sample_is_404(client):
    """Unknown IDs return 404 on both sample endpoints, never an empty 200.

    The expression endpoint is the interesting one: an existing sample with no rows (a QC
    failure) must still be a 200 with an empty list, so "no rows" cannot be used as a proxy
    for "no such sample".
    """
    for path in ("/samples/NOPE-999", "/samples/NOPE-999/expression"):
        resp = await client.get(path)
        assert resp.status_code == 404, path
        assert "NOPE-999" in resp.json()["detail"]


async def test_keyset_pagination_returns_every_sample_exactly_once(client):
    """Walking every page yields each sample once, in `sample_id` order."""
    seen: list[int] = []
    cursor = 0
    pages = 0

    while True:
        resp = await client.get("/samples", params={"after": cursor, "limit": 2})
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(s["sample_id"] for s in body["samples"])
        pages += 1
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]
        assert pages < 10, "pagination did not terminate"

    assert seen == sorted(seen), "pages were not returned in key order"
    assert len(seen) == len(set(seen)), "a sample appeared on two pages"
    assert seen == [s["sample_id"] for s in _SAMPLES]
    assert pages > 1, "the fixture must span several pages for this to prove anything"


@pytest.mark.parametrize("limit", [0, 501])
async def test_limit_out_of_range_is_rejected(client, limit):
    """`limit` outside 1..500 is a validation error, not a silent clamp."""
    resp = await client.get("/samples", params={"limit": limit})

    assert resp.status_code == 422


async def test_single_cell_expression_has_null_tpm(client):
    """Single-cell samples report UMI counts; the null TPM survives serialisation."""
    resp = await client.get("/samples/RNA-SC-008/expression")

    assert resp.status_code == 200
    values = resp.json()
    assert len(values) == 2
    assert all(v["tpm"] is None for v in values)
    assert all(v["raw_counts"] > 0 for v in values)
    # Bulk samples, by contrast, do carry TPM.
    bulk = (await client.get("/samples/RNA-BULK-006/expression")).json()
    assert all(v["tpm"] is not None for v in bulk)


async def test_rejected_payloads_are_listed(client):
    """The dead-letter endpoint returns rejections with their structured error report."""
    resp = await client.get("/payloads/rejected", params={"limit": 5})

    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["payload_id"] == "pl_009_malformed"
    assert rows[0]["error_detail"]["error_count"] == 2
    locs = [e["loc"] for e in rows[0]["error_detail"]["errors"]]
    assert "expression.inline_vector.0.raw_counts" in locs


async def test_health_is_503_when_database_is_down(client_factory):
    """An outage surfaces as 503, not as a 500 crash."""
    async with client_factory(FakeRepository(healthy=True)) as healthy:
        assert (await healthy.get("/health")).status_code == 200
        assert (await healthy.get("/health")).json() == {"status": "ok"}

    async with client_factory(FakeRepository(healthy=False)) as down:
        resp = await down.get("/health")

    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"].lower()


# ── Integration ──────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="psycopg async does not support the Windows ProactorEventLoop; run in WSL or Docker.",
)
async def test_api_against_real_postgres(db, monkeypatch):
    """Real async pool, health check and listing against the legacy baseline.

    The ``db`` fixture (tests/conftest.py) applies the migrations and resets the database to
    the five legacy seed rows, so the assertions below are exact rather than lower bounds.
    """
    import os

    from .conftest import SEED_SAMPLE_COUNT

    if not os.getenv("API_DATABASE_URL") and not os.getenv("API_DB_PASSWORD"):
        pytest.skip("Set API_DATABASE_URL (or API_DB_PASSWORD) to run the API integration test.")

    # The real lifespan hook opens the pool; ASGITransport alone would not run it.
    try:
        async with lifespan(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as real:
            health = await real.get("/health")
            assert health.status_code == 200

            page = await real.get("/samples", params={"limit": 500})
            assert page.status_code == 200
            body = page.json()
            assert len(body["samples"]) == SEED_SAMPLE_COUNT
            assert body["next_cursor"] is None
            # The legacy rows predate the vendor API and carry no vendor_sample_id.
            assert all(s["vendor_sample_id"] is None for s in body["samples"])
            assert [s["sample_id"] for s in body["samples"]] == sorted(
                s["sample_id"] for s in body["samples"]
            )
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable for the API role ({exc}).")
