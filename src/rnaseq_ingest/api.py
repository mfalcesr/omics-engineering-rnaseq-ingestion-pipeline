"""Read-only async HTTP API over the ingested RNA-seq database.

The pipeline's write path is untouched: the ``ingest`` CLI keeps loading payloads as
``rnaseq_user``. This module only ever reads, and it connects as ``rnaseq_api``, a
SELECT-only role (see ``db/migrations/002_api_readonly_role.sql``). The two paths share a
database and nothing else.

Design notes (see docs/DECISIONS.md for the ingestion-side equivalents):

* **Async end to end.** Handlers are ``async def`` and the driver is psycopg 3's
  ``AsyncConnectionPool``. A blocking driver call inside ``async def`` would stall the event
  loop and therefore every other in-flight request, so there are none.
* **Concurrency where it pays.** ``/samples/{id}/expression`` needs two independent queries
  (does the sample exist, and what are its values). ``asyncio.gather`` runs them together, so
  latency is the slower of the two rather than their sum. The cost is two pooled connections
  for the lifetime of that request, which is why ``API_POOL_MAX_SIZE`` exists.
* **Contract decoupled from storage.** Every physical column name is confined to the SQL
  block below and aliased into the response models (``quantification_tool AS tool``). The
  legacy columns cannot be renamed, but the public contract is free to be tidier, and a
  future column rename is a one-line change here instead of a breaking API change.
* **Testable by design.** Endpoints receive a repository through ``Depends``; the unit tests
  swap in an in-memory fake via ``dependency_overrides`` and never open a connection.

Platform note: psycopg's async mode does not work on the default Windows event loop
(``ProactorEventLoop``). Run the server on Linux, macOS, WSL or Docker. The unit tests are
unaffected, as they never open a real pool.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Protocol
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# SQL, the only place that knows physical column names
#
# Verified against db/init.sql and db/migrations/001_extend_sample.sql. Note that
# `sample.sample_id` is a SERIAL surrogate key and `vendor_sample_id` is the vendor's
# business key, nullable because the five legacy seed rows predate the vendor API.
# ─────────────────────────────────────────────────────────────────────────────

# Columns shared by the single-sample lookup and the paged listing.
_SAMPLE_COLUMNS = """
    sample_id,
    vendor_sample_id,
    subject_id,
    study_id,
    tissue,
    assay_type,
    qc_passed,
    total_reads,
    pct_mapped,
    rin_score,
    sequencing_batch,
    cro,
    instrument_model,
    gene_model,
    quantification_tool         AS tool,
    quantification_tool_version AS tool_version,
    vendor_generated_at,
    created_at,
    updated_at
"""

SQL_GET_SAMPLE = f"""
    SELECT {_SAMPLE_COLUMNS}
    FROM sample
    WHERE vendor_sample_id = %(vendor_sample_id)s
"""

# Keyset pagination on the primary key: it is never NULL (vendor_sample_id can be) and it is
# already the PK index, so the scan never sorts. OFFSET would skip or repeat rows when the
# loader inserts a sample mid-walk; `sample_id > after` cannot. We ask for one row more than
# requested to learn whether a further page exists without a second COUNT query.
SQL_LIST_SAMPLES = f"""
    SELECT {_SAMPLE_COLUMNS}
    FROM sample
    WHERE sample_id > %(after)s
    ORDER BY sample_id
    LIMIT %(limit_plus_one)s
"""

SQL_SAMPLE_EXISTS = """
    SELECT 1 AS present
    FROM sample
    WHERE vendor_sample_id = %(vendor_sample_id)s
"""

# `tpm` is NULL for single-cell samples: the vendor ships UMI counts, and a TPM computed from
# them would be a fabrication. The null survives all the way to the JSON response.
SQL_GET_EXPRESSION = """
    SELECT e.gene_id,
           e.raw_counts,
           e.tpm
    FROM sample_expression e
    JOIN sample s ON s.sample_id = e.sample_id
    WHERE s.vendor_sample_id = %(vendor_sample_id)s
    ORDER BY e.gene_id
"""

# The dead-letter view: every payload the loader refused, newest first, with the structured
# validation report that explains why. `payload_id` breaks ties so the order is total.
SQL_LIST_REJECTED = """
    SELECT payload_id,
           vendor_sample_id,
           api_version,
           source_system,
           received_at,
           error_detail
    FROM sample_payload_log
    WHERE ingest_status = 'rejected'
    ORDER BY received_at DESC, payload_id DESC
    LIMIT %(limit)s
"""


# ─────────────────────────────────────────────────────────────────────────────
# Response models, the public contract
# ─────────────────────────────────────────────────────────────────────────────


class Sample(BaseModel):
    """One sample as the loader currently holds it (upsert-if-newer keeps it current)."""

    sample_id: int = Field(description="Internal surrogate key; also the pagination cursor.")
    vendor_sample_id: str | None = Field(
        default=None,
        description="Vendor business key. Null for the legacy rows that predate the vendor API.",
    )
    subject_id: str
    study_id: str
    tissue: str
    assay_type: str
    qc_passed: bool | None = None
    total_reads: int | None = None
    pct_mapped: float | None = None
    rin_score: float | None = None
    sequencing_batch: str | None = None
    cro: str | None = None
    instrument_model: str | None = None
    gene_model: str | None = None
    tool: str | None = Field(default=None, description="Quantification tool name.")
    tool_version: str | None = None
    vendor_generated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SamplePage(BaseModel):
    """One page of samples plus the cursor that fetches the next one."""

    samples: list[Sample]
    next_cursor: int | None = Field(
        default=None,
        description="Pass as `after` to fetch the next page. Null when this is the last page.",
    )


class ExpressionValue(BaseModel):
    """One gene's measurement for a sample."""

    gene_id: str
    raw_counts: int
    tpm: float | None = Field(
        default=None,
        description="Null for single-cell samples, which report UMI counts rather than TPM.",
    )


class RejectedPayload(BaseModel):
    """A payload the loader rejected, kept for root-cause analysis."""

    payload_id: str
    vendor_sample_id: str | None = None
    api_version: str | None = None
    source_system: str | None = None
    received_at: datetime
    error_detail: dict[str, Any] | None = Field(
        default=None,
        description="Structured validation report: `error_count` plus the per-field `errors`.",
    )


class Health(BaseModel):
    """Liveness probe payload."""

    status: str


# ─────────────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────────────


class Repository(Protocol):
    """What the endpoints need. The real implementation talks to PostgreSQL; tests fake it."""

    async def get_sample(self, vendor_sample_id: str) -> dict[str, Any] | None: ...

    async def list_samples(self, after: int, limit: int) -> tuple[list[dict[str, Any]], int | None]:
        ...

    async def sample_exists(self, vendor_sample_id: str) -> bool: ...

    async def get_expression(self, vendor_sample_id: str) -> list[dict[str, Any]]: ...

    async def list_rejected(self, limit: int) -> list[dict[str, Any]]: ...

    async def ping(self) -> bool: ...


class SampleRepository:
    """Async data access over a psycopg 3 connection pool.

    Each method borrows a connection for exactly one query and returns it immediately, so a
    slow consumer cannot pin a connection while it serialises a response.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _fetch_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def get_sample(self, vendor_sample_id: str) -> dict[str, Any] | None:
        return await self._fetch_one(SQL_GET_SAMPLE, {"vendor_sample_id": vendor_sample_id})

    async def list_samples(self, after: int, limit: int) -> tuple[list[dict[str, Any]], int | None]:
        rows = await self._fetch_all(
            SQL_LIST_SAMPLES, {"after": after, "limit_plus_one": limit + 1}
        )
        return _paginate(rows, limit)

    async def sample_exists(self, vendor_sample_id: str) -> bool:
        row = await self._fetch_one(SQL_SAMPLE_EXISTS, {"vendor_sample_id": vendor_sample_id})
        return row is not None

    async def get_expression(self, vendor_sample_id: str) -> list[dict[str, Any]]:
        return await self._fetch_all(SQL_GET_EXPRESSION, {"vendor_sample_id": vendor_sample_id})

    async def list_rejected(self, limit: int) -> list[dict[str, Any]]:
        return await self._fetch_all(SQL_LIST_REJECTED, {"limit": limit})

    async def ping(self) -> bool:
        """True if the database answers. Any driver error means unhealthy, not a 500."""
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - any failure to reach the DB is a failed health check
            return False


def _paginate(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int | None]:
    """Trim the probe row and derive the next cursor.

    The query asked for ``limit + 1`` rows. If it got them, a further page exists and the
    cursor is the last *returned* row's key; otherwise this was the final page.
    """
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1]["sample_id"] if has_more and page else None
    return page, next_cursor


# ─────────────────────────────────────────────────────────────────────────────
# Connection string
# ─────────────────────────────────────────────────────────────────────────────


def database_url() -> str:
    """The API's own connection string, resolved from the environment.

    ``API_DATABASE_URL`` wins if set. Otherwise the URL is composed from the same ``DB_*``
    variables the ingest CLI uses, with the user swapped for ``rnaseq_api`` and the password
    taken from ``API_DB_PASSWORD``. Reusing those variables means a developer who already has
    a working ``.env`` only has to add the API password.
    """
    override = os.getenv("API_DATABASE_URL")
    if override:
        return override

    password = os.getenv("API_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "Set API_DATABASE_URL, or API_DB_PASSWORD so the URL can be composed from DB_*. "
            "See .env.example."
        )
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "rnaseq_db")
    user = os.getenv("API_DB_USER", "rnaseq_api")
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{name}"


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pool at startup, close it at shutdown.

    ``wait=True`` with a timeout makes startup fail loudly when the database is unreachable.
    A container that crash-loops is easy to spot; one that starts happily and then 500s on
    every request is not.

    Connections are opened read-only with a statement timeout, which is defence in depth: the
    ``rnaseq_api`` role already carries both settings, but a misconfigured role would
    otherwise be invisible from here.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=database_url(),
        min_size=1,
        max_size=int(os.environ.get("API_POOL_MAX_SIZE", "10")),
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
        },
    )
    await pool.open(wait=True, timeout=10.0)
    app.state.repo = SampleRepository(pool)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="RNA-seq Read API",
    version="0.1.0",
    summary="Read-only access to ingested RNA-seq samples, expression values and rejections.",
    description=(
        "A read path over the database filled by the `ingest` CLI. Every write stays with the "
        "CLI; this service connects as a SELECT-only role. Try any endpoint below."
    ),
    lifespan=lifespan,
)


def get_repository(request: Request) -> Repository:
    """Hand the request the live repository.

    Unit tests override this with ``app.dependency_overrides`` and never touch a database.
    The 503 covers the case where the app is serving before the lifespan hook has run.
    """
    repo = getattr(request.app.state, "repo", None)
    if repo is None:  # pragma: no cover - lifespan always sets this in production
        raise HTTPException(status_code=503, detail="Database pool is not ready")
    return repo


# FastAPI's current idiom: the dependency lives in the annotation, not in a default argument.
# It also keeps the signatures readable and avoids a mutable-looking default.
RepoDep = Annotated[Repository, Depends(get_repository)]


@app.get("/health", response_model=Health, tags=["ops"])
async def health(repo: RepoDep) -> Any:
    """Liveness probe for Docker, Kubernetes or Cloud Run.

    Returns 503 rather than raising, so an outage reads as "this instance is unhealthy"
    instead of "this instance crashed".
    """
    if not await repo.ping():
        raise HTTPException(status_code=503, detail="Database is unreachable")
    return {"status": "ok"}


@app.get("/samples", response_model=SamplePage, tags=["samples"])
async def list_samples(
    repo: RepoDep,
    after: Annotated[
        int,
        Query(ge=0, description="Cursor: return samples with a `sample_id` greater than this."),
    ] = 0,
    limit: Annotated[int, Query(ge=1, le=500, description="Page size, 1 to 500.")] = 50,
) -> Any:
    """One page of samples, ordered by `sample_id`, plus the cursor for the next page."""
    rows, next_cursor = await repo.list_samples(after=after, limit=limit)
    return {"samples": rows, "next_cursor": next_cursor}


@app.get("/samples/{vendor_sample_id}", response_model=Sample, tags=["samples"])
async def get_sample(
    vendor_sample_id: str,
    repo: RepoDep,
) -> Any:
    """The current row for one sample, as kept by the upsert-if-newer loader."""
    row = await repo.get_sample(vendor_sample_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No sample with id {vendor_sample_id!r}")
    return row


@app.get(
    "/samples/{vendor_sample_id}/expression",
    response_model=list[ExpressionValue],
    tags=["samples"],
)
async def get_expression(
    vendor_sample_id: str,
    repo: RepoDep,
) -> Any:
    """Gene expression values for one sample.

    The existence check and the value query are independent, so they run concurrently: a
    sample with no rows is a real state (QC failures carry none), and it must be told apart
    from a sample that does not exist at all.
    """
    exists, rows = await asyncio.gather(
        repo.sample_exists(vendor_sample_id),
        repo.get_expression(vendor_sample_id),
    )
    if not exists:
        raise HTTPException(status_code=404, detail=f"No sample with id {vendor_sample_id!r}")
    return rows


@app.get("/payloads/rejected", response_model=list[RejectedPayload], tags=["payloads"])
async def list_rejected_payloads(
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size, 1 to 200.")] = 20,
) -> Any:
    """The most recent dead-letter payloads with their validation errors, newest first."""
    return await repo.list_rejected(limit=limit)
