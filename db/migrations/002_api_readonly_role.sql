-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 002, least-privilege role for the read API
--
-- Design: ADDITIVE and IDEMPOTENT, exactly like 001. Re-running is a no-op.
--
-- The ingest CLI keeps writing as `rnaseq_user`. The API connects as `rnaseq_api`,
-- which can SELECT and nothing else. A bug in the read path therefore cannot write,
-- delete, or alter anything: the loader's idempotency guarantees (upsert-if-newer,
-- expression swap, append-only payload log) stay the loader's business alone.
--
-- The password arrives as a psql variable so it never enters the repository:
--
--   export API_DB_PASSWORD='choose-a-strong-password'
--   docker exec -i rnaseq-postgres psql -U rnaseq_user -d rnaseq_db \
--     -v api_pw="$API_DB_PASSWORD" < db/migrations/002_api_readonly_role.sql
--
-- Note: this file is deliberately NOT wrapped in BEGIN/COMMIT. CREATE ROLE cannot run
-- inside a transaction block in some managed-Postgres configurations, and tests/conftest.py
-- applies migrations statement-by-statement under autocommit anyway.
-- ─────────────────────────────────────────────────────────────────────────────

\set ON_ERROR_STOP on

-- ── 1. The role ──────────────────────────────────────────────────────────────
-- CREATE ROLE has no IF NOT EXISTS, so branch on the catalogue. The password is
-- set on every run, which makes the migration safe to re-run after a rotation.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'rnaseq_api') THEN
        CREATE ROLE rnaseq_api LOGIN;
    END IF;
END
$$;

ALTER ROLE rnaseq_api WITH LOGIN PASSWORD :'api_pw';

-- Belt and braces: even if a future grant is too generous, every transaction this
-- role opens starts read-only, and no single statement may run longer than 5 seconds.
ALTER ROLE rnaseq_api SET default_transaction_read_only = on;
ALTER ROLE rnaseq_api SET statement_timeout = '5s';

-- ── 2. Grants, SELECT only ───────────────────────────────────────────────────
-- Tables live in `public` (db/init.sql creates them without a schema qualifier).
GRANT CONNECT ON DATABASE rnaseq_db TO rnaseq_api;
GRANT USAGE   ON SCHEMA   public     TO rnaseq_api;

GRANT SELECT ON sample             TO rnaseq_api;
GRANT SELECT ON sample_expression  TO rnaseq_api;
GRANT SELECT ON sample_payload_log TO rnaseq_api;
GRANT SELECT ON v_sample_legacy    TO rnaseq_api;

-- Future additive tables should be readable too, without editing this file again.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO rnaseq_api;

-- ── 3. Explicitly withhold everything else ───────────────────────────────────
-- No sequence access: the API never inserts, so it never needs nextval().
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM rnaseq_api;
-- PUBLIC can create objects in `public` on PostgreSQL < 15; close that off for this role.
REVOKE CREATE ON SCHEMA public FROM rnaseq_api;
