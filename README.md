# RNA-seq Ingestion Pipeline

Production-grade ingestion of sequencing-vendor JSON payloads into PostgreSQL: strict validation,
schema evolution, and idempotent loads, with design notes for an analytical star schema and a
target lakehouse platform.

[![CI](https://github.com/mfalcesr/omics-engineering-rnaseq-ingestion-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/mfalcesr/omics-engineering-rnaseq-ingestion-pipeline/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## The scenario

A translational-genomics lab receives processed RNA-seq results from multiple sequencing vendors as
JSON. The vendors use slightly different field names, payloads are sometimes re-delivered (a
reprocessing with better data), and some arrive malformed. This project ingests them reliably into a
shared PostgreSQL database, and sketches the analytical model and platform the lab would grow into.

It is deliberately three layers:

| Layer | What | Status |
|---|---|---|
| **Pipeline** | `parse → validate → normalise → load` into PostgreSQL | **Implemented + tested** (25 tests) |
| **Analytical model** | Kimball star schema for cross-study analysis | Design ([docs/gold_star_schema.md](docs/gold_star_schema.md)) |
| **Platform** | target lakehouse architecture + migration | Design ([docs/target_platform.md](docs/target_platform.md)) |

---

## Highlights

- **Strict validation** (Pydantic v2): a vendor sending `total_reads` as the string `"52381204"` is
  *rejected*, not silently coerced; every error is reported at once with its exact JSON path.
- **Atomic, all-or-nothing writes:** a malformed payload is written to a dead-letter log and the
  database is left byte-identical.
- **Schema evolution without migrations:** the ~10 queried fields are typed columns; everything else
  lands in a GIN-indexed `JSONB` catch-all; the full raw payload is kept for audit. New vendor fields
  cost zero migrations.
- **Idempotent loads:** one current row per sample (`upsert-if-newer`), plus an append-only audit log,
  so re-running the same payload is a safe no-op and a genuine reprocessing updates in place.
- **Two assay types:** bulk and single-cell RNA-seq, with different QC metrics and null TPM
  (Transcripts Per Million) for single-cell, handled by one nullable model.
- **Legacy-safe:** additive migrations only, plus a `v_sample_legacy` view pinning the original column
  contract, so existing consumers never break.
- **Tested:** 25 tests, 17 pure unit (no database) and 8 integration against real PostgreSQL, run in
  CI (Continuous Integration) via GitHub Actions.

The reasoning behind every choice is in [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Pipeline shape

```
JSON ─▶ parse ─▶ validate ─▶ normalise ─▶ load ─▶ PostgreSQL
        └──── pure, no database ────┘    └ one transaction ┘
```

The first three stages are pure functions (no database), so they test in milliseconds; only `load`
opens a transaction.

---

## Design highlights

**Analytical model (Gold layer).** A Kimball star schema with two fact tables at different grains:
`fct_gene_expression` (one row per sample x gene) and `fct_sample_qc` (one row per sample), so a
quality query never scans the huge expression fact. `dim_gene` is versioned by gene-model release,
because expression is only comparable within one annotation version. Full write-up in
[docs/gold_star_schema.md](docs/gold_star_schema.md).

![Analytical star schema](docs/images/task2_erd.png)

**Target platform.** A medallion lakehouse (Databricks or Microsoft Fabric) with an immutable raw
landing zone, a reverse-ETL sync back to the legacy database so existing dashboards survive migration,
and Nextflow kept for bioinformatics rather than rebuilt. Full write-up in
[docs/target_platform.md](docs/target_platform.md).

![Target platform architecture](docs/images/task3_architecture.png)

---

## Prerequisites

- Docker Desktop 20+ (`docker compose version`)
- Python 3.10+ (`python3 --version`; on Windows `py --version`)

---

## Quick start

```bash
# 1. Start PostgreSQL (schema + 5 baseline rows load automatically on first boot)
cp .env.example .env
docker compose up -d

# 2. Apply the additive, idempotent migration (safe to run repeatedly)
docker exec -i rnaseq-postgres psql -U rnaseq_user -d rnaseq_db < db/migrations/001_extend_sample.sql

# 3. Install
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 4. Ingest every fixture
.venv/bin/ingest load-dir sources/vendor_payloads --console
#   ...or one payload:
.venv/bin/ingest load-payload sources/vendor_payloads/RNA-BULK-006.json --console
```

On Windows, use `py -m venv .venv` and the `.venv\Scripts\` path in place of `.venv/bin/`.

**Entry point:** `ingest load-payload <file>` / `ingest load-dir <dir>`. Exit code is non-zero if any
payload is rejected, so CI and orchestrators can detect failure. `--json-logs` (default) emits
structured JSON with `payload_id` on every line; `--console` is the readable variant.

---

## What a run looks like

From a clean database (5 baseline rows) after ingesting all five fixtures:

```
[LOADED   ] RNA-BULK-006.json            insert
[LOADED   ] RNA-BULK-006_duplicate.json  update (reprocessing supersedes: pct_mapped 94.7 → 94.9, tool 2.2.1 → 2.2.2)
[LOADED   ] RNA-BULK-007.json            insert (second vendor's field names auto-mapped)
[REJECTED ] RNA-BULK-009_malformed.json  6 structured errors, nothing written
[LOADED   ] RNA-SC-008.json              insert (single-cell: null TPM, cell-level QC)

Summary: 4 loaded, 0 skipped, 1 rejected (5 payloads).
```

| Check | Before | After |
|---|---|---|
| `sample` rows | 5 | **8** (5 baseline + 3 vendor; the duplicate upserted in place) |
| `sample_expression` rows | 15 | **60** |
| `v_sample_legacy` rows | 5 | **8** (legacy contract intact) |
| `sample_payload_log` | 0 | 3 `loaded`, 1 `superseded`, 1 `rejected` |

The malformed payload leaves the database byte-identical apart from one `rejected` dead-letter row.

---

## Tests

```bash
.venv/bin/pytest -m "not integration"   # 17 unit tests, no database
.venv/bin/pytest                        # full suite, 25 tests, needs Docker up
```

- **`test_parse.py`** parsing/normalisation, alias mapping, tool/version split, single-cell null TPM.
- **`test_validation.py`** the malformed payload yields exactly six errors at the expected JSON paths.
- **`test_load.py`** *(integration)* happy path, schema-evolution aliases, single-cell, duplicate
  upsert + supersede, true replay = no-op, out-of-order delivery ignored, atomic rejection, legacy
  rows untouched. Skips cleanly if PostgreSQL is unreachable.

---

## Repository layout

```
.
├─ README.md · .env.example · .gitignore · pyproject.toml · docker-compose.yml
├─ .github/workflows/ci.yml            # unit + integration tests on push/PR
├─ db/
│  ├─ init.sql                         # base schema + baseline rows
│  └─ migrations/001_extend_sample.sql # additive, idempotent
├─ sources/vendor_payloads/*.json      # 5 example payloads
├─ src/rnaseq_ingest/
│  ├─ config.py        # env-var settings, DATABASE_URL composition (no hardcoded creds)
│  ├─ logging_conf.py  # structlog JSON, payload_id on every line
│  ├─ models.py        # Pydantic: parse + validate (strict, all-errors, JSON paths)
│  ├─ normalise.py     # alias handling, extra_metadata, tool/version split, row hash
│  ├─ load.py          # transaction, upsert-if-newer, expression swap, payload log
│  └─ cli.py           # `ingest load-payload` / `load-dir`
├─ tests/              # conftest (DB reset fixture) + unit + integration
└─ docs/               # DECISIONS.md, gold_star_schema.md, target_platform.md, deck.md/pdf
```

---

## Design docs

- **[docs/DECISIONS.md](docs/DECISIONS.md)** every trade-off as "chose X over Y because Z".
- **[docs/gold_star_schema.md](docs/gold_star_schema.md)** the analytical star schema (grain, SCD,
  example queries, ER diagram).
- **[docs/target_platform.md](docs/target_platform.md)** target platform (lakehouse picks,
  migration, governance).
- **[docs/deck.md](docs/deck.md)** a short overview deck (Marp). Export: `npx @marp-team/marp-cli docs/deck.md --pdf`.

---

## Tech stack

Python 3.10+ · Pydantic v2 · psycopg 3 · structlog · Typer · PostgreSQL 16 · Docker Compose ·
pytest · GitHub Actions.

---

## Configuration

Credentials come from the environment, never hardcoded. `config.py` composes the connection string
from `DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD`, or uses `DATABASE_URL` if set. Copy
`.env.example` to `.env` (git-ignored).

---

## License

MIT, see [LICENSE](LICENSE).
