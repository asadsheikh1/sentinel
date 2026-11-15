-- SENTINEL database roles. Run ONCE per database as a superuser (idempotent).
--
--   psql -v ON_ERROR_STOP=1 \
--        -v migrator_password=... -v app_password=... -v readonly_password=... \
--        -d sentinel -f scripts/db_roles.sql
--
-- Roles (docs/2-architecture/4-database.md):
--   sentinel_migrator  owns the schema; runs Alembic migrations (DDL)
--   sentinel_app       the API and workers: DML only, and no UPDATE/DELETE on audit_log
--   sentinel_readonly  reporting and evaluation: SELECT only, no session/api-key hashes

\set ON_ERROR_STOP on

SELECT 'CREATE ROLE sentinel_migrator' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_migrator') \gexec
SELECT 'CREATE ROLE sentinel_app'      WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_app') \gexec
SELECT 'CREATE ROLE sentinel_readonly' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_readonly') \gexec

ALTER ROLE sentinel_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'migrator_password';
ALTER ROLE sentinel_app      WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER ROLE sentinel_readonly WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'readonly_password';

-- Extensions need superuser (pgvector is not a "trusted" extension).
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- The migrator owns the database, and therefore the public schema (PG15+: pg_database_owner).
SELECT format('ALTER DATABASE %I OWNER TO sentinel_migrator', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO sentinel_app, sentinel_readonly', current_database()) \gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sentinel_app, sentinel_readonly;

-- Every table/sequence the migrator creates is automatically usable by the app
-- and readable by readonly. Migrations then narrow these (e.g. audit_log).
ALTER DEFAULT PRIVILEGES FOR ROLE sentinel_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sentinel_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sentinel_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO sentinel_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sentinel_migrator IN SCHEMA public
  GRANT SELECT ON TABLES TO sentinel_readonly;
