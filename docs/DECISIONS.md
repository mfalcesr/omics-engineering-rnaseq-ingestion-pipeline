# Design Decisions

Every decision below is stated as **chose X over Y because Z**. This file records the reasoning and
trade-offs behind the pipeline; it is the spine of both the code and the overview deck.

---

## D1: Schema evolution: hybrid "promote + JSONB catch-all"

**Chose** typed columns for the ~10 fields we query or constrain (`sequencing_batch`, `cro`,
`instrument_model`, `matrix_url`, `gene_model`, `vendor_generated_at`, `lab_notes`, plus the QC
columns already in the schema) **+ a single `extra_metadata JSONB`** (GIN-indexed) for the long
tail **+ an append-only `sample_payload_log`** holding the raw payload byte-for-byte.

**Over:**
- **Column-per-field**, every new vendor field is a migration; breaks the "no migration per
  onboarding" goal.
- **JSONB-only / EAV**, no constraints, no types, poor query ergonomics; scientists end up writing
  `->>` casts and there is no legacy-consumer contract.

**Because** the vendor field set varies per assay and per CRO. The hybrid gives constraints and
indexes on the fields that matter, zero migrations for the tail (Postgres GIN-indexes JSONB so it
stays queryable), and a lossless audit copy. When a tail field becomes important it is *promoted*
to a column in a planned backfill, a deliberate migration, not a forced one.

**Additive guarantee.** The migration only `ADD COLUMN` (all nullable) and `CREATE TABLE`/`VIEW`.
Nothing is renamed, dropped, or narrowed, so the five legacy seed rows and the original column names
that downstream R scripts depend on are untouched. Residual risk: a consumer doing `SELECT *` into a
fixed-width data frame can still be surprised by new columns, so we ship **`v_sample_legacy`**, a
view pinned to the original column list, and tell consumers to read that.

> `flow_cell_id` and `insert_size_median` are deliberately left in `extra_metadata` (not promoted)
> to demonstrate the tail path works and is queryable: `extra_metadata->>'flow_cell_id'`.

---

## D2: Idempotency: upsert-current + append-only history

**Chose** one **current** row per `vendor_sample_id` (`ON CONFLICT (vendor_sample_id) DO UPDATE`),
guarded so a re-delivery only wins if it is newer, and **every** payload appended to
`sample_payload_log`.

**Over skip-on-duplicate** (would leave stale QC in the warehouse, exactly the "which number is
correct?" problem the organisation is trying to fix; the duplicate fixture is a *legitimate reprocessing*
with better data) **and over full versioning in `sample`** (breaks the one-row-per-sample assumption
that legacy dashboards *and* the schema's own `UNIQUE(vendor_sample_id)` both depend on).

**Because** auditability is *our* requirement and "one row per sample" is *our consumers'*
requirement, and those do not have to live in the same table. Upsert-current gives consumers a
single truth; the append-only log gives the audit trail and lets any historical state be rebuilt.
We get versioning **without** pushing version-awareness onto every consumer. (The analytical layer
can materialise an SCD2 dimension from that log.)

**Two flavours of idempotency, named explicitly:**
1. **Same `payload_id` seen again** → true replay → **no-op**, logged at INFO, exit 0. This is what
   makes orchestrator retries safe.
2. **Same `vendor_sample_id`, new `payload_id`** → **upsert if newer**, logged at WARNING with a
   diff of the changed QC fields; the previously-applied payload row is demoted to `superseded`.

**Out-of-order guard.** `WHERE EXCLUDED.vendor_generated_at > sample.vendor_generated_at`, an older
re-delivery arriving late is logged as `skipped` (`reason=not_newer`) and does **not** overwrite the
current row. (Verified by `test_out_of_order_older_delivery_is_ignored`.)

**Expression rows: delete-then-insert** for the `sample_id` inside the same transaction, not
per-gene upsert, a reprocessing may cover a *different* gene set, and per-gene upsert would leave
orphan genes from the old run silently mixed into the new one. Deleting is safe because the raw
payload is preserved in the log. `UNIQUE(sample_id, gene_id)` backstops either way. At 20k genes
this becomes `COPY` into a temp table + swap (see D6).

---

## D3: Validation: accumulate all errors, reject atomically

**Chose** Pydantic v2 with **`StrictInt`** on `total_reads`/`raw_counts` and a `StrictInt|StrictFloat`
union on fractional fields, plus field validators for the physical rules (`0 ≤ pct_mapped ≤ 100`,
`raw_counts ≥ 0`, `gene_id` matches `^ENSG\d{11}$`, `assay_type ∈ {bulk_rnaseq, scrna_seq}`).

**Over lax coercion.** The trap in the malformed fixture: lax Pydantic coerces the string
`"52381204"` to an int and the error *disappears*. `StrictInt` rejects it. (Verified by
`test_string_total_reads_is_rejected_not_coerced`.)

**Nothing short-circuits.** One `ValidationError` carries all six problems at once, and `err["loc"]`
gives the JSON path the requirements ask for. The emitted rejection is machine-readable:

```json
{"payload_id": "pay_20251221_zz000001", "vendor_sample_id": "RNA-BULK-009",
 "status": "rejected", "error_count": 6,
 "errors": [{"path": "expression.inline_vector.0.raw_counts", "rule": "greater_than_equal",
             "value": -99, "message": "Input should be greater than or equal to 0"}, ...]}
```

**Atomic.** The rejection is written to `sample_payload_log` with `ingest_status='rejected'` (a
**dead-letter table**, nothing is dropped silently), and **nothing else touches the DB**. Validation
runs before any `sample`/`sample_expression` write, all inside one transaction.

**Layered defence.** App-level validation gives the lab an actionable report; the DB `CHECK`
constraints already in the schema (`pct_mapped 0–100`, `assay_type` allow-list) are the backstop.
The CHECKs alone would catch three of the six errors, but they would return a Postgres error string
instead of an actionable report, and would fail on the first one rather than reporting all six.

---

## D4: Field-name normalisation: declarative alias map

**Chose** Pydantic `AliasChoices` (`cro` ← {`cro`, `sequencing_centre`, `sequencing_center`};
`pct_mapped` ← {`pct_mapped`, `percent_mapped`}) **over** `if "cro" in payload` branching.

**Because** onboarding CRO #3 becomes a *data change* (add an alias), not a *code change*, and the
alias list is a readable artefact a bioinformatician can review. (Verified by
`test_second_cro_field_aliases_persist`: `percent_mapped=96.1` and `sequencing_centre='SeqCore Labs'`
land in the normalised columns.)

**Known gap (named, not silently ignored):** we normalise field *names* but not yet field *values*, `peripheral_blood_mononuclear_cell` vs the seed's `peripheral_blood` should map to a controlled
vocabulary (UBERON/EFO for tissue). That is a lookup table we would add next; flagged rather than
hidden, because "JSONB must not become the place where problems go to hide."

---

## D5: Layering: parse → validate → normalise → load

**Chose** pure functions for `parse`, `validate`, `normalise` with the database touched **only** in
`load`.

**Because** the first three stages are unit-testable without Postgres (17 fast unit tests, no
Docker) and they are the part that survives a platform migration, the parser, alias map,
and validators are the durable IP. This separation *is* the answer to "how would the pipeline change inside
the platform": the pure core is kept, the `psycopg`/transaction plumbing is delegated to dbt +
Dagster.

---

## D6: Big matrix handling (design note)

The fixtures carry a 15-gene inline vector; production carries 20k genes × N samples via `matrix_url`
(`.h5`). **Do not** row-load that into the OLTP database.

- **Land** the `.h5` unchanged in object storage, content-addressed by checksum, partitioned
  `study/sample/run`; record `matrix_url` + checksum + `gene_model` on `sample`.
- **Convert** to Parquet partitioned by study and gene block; expose via DuckDB / warehouse external
  tables. Load path is `COPY` into an unlogged staging table then swap, or write Parquet directly.
- **Failure modes:** S3 unavailable → the metadata row still lands with `matrix_status='pending'` and
  a separate retriable task fetches the blob (metadata availability is decoupled from blob
  availability); checksum mismatch → quarantine, never overwrite.
- **scRNA-seq** `.h5` is a sparse cell × gene matrix, it is **not** flattened into a relational
  table; it stays native for Scanpy/Seurat, and only per-sample aggregates land in the warehouse.

At 100+ samples/day × 20k genes that is ~2M rows/day, Postgres row-wise is the wrong target;
per-sample parallelism in the orchestrator + Parquet/warehouse is the right one.

---

## Repo-hygiene fixes made along the way

- **`.env.example` template** so credentials are composed from the environment and never live in
  git; `.gitignore` excludes `.env`. No credentials in the repository.
- **Redundant constraint spotted:** the schema has both `vendor_sample_id VARCHAR(128) UNIQUE` and
  `UNIQUE(vendor_sample_id, payload_id)`. The second is redundant given the first, and the
  single-column UNIQUE is precisely what makes versioning-in-place impossible, which is *why* D2 is
  upsert-current. (The legacy seed rows survive only because Postgres permits multiple NULLs in a
  UNIQUE column.)
- **Under-scaled column:** `median_cv_coverage NUMERIC(5,2)` stores a CV ratio (0.38–0.41); values
  like 0.405 would round. Minor, would widen to `NUMERIC(6,4)` in a future additive migration.
