#!/usr/bin/env bash
# Runs once, when the Postgres data volume is first initialised.
# Creates the SENTINEL roles (scripts/db_roles.sql) with passwords from the environment.
set -euo pipefail
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v migrator_password="$SENTINEL_DB_MIGRATOR_PASSWORD" \
  -v app_password="$SENTINEL_DB_APP_PASSWORD" \
  -v readonly_password="$SENTINEL_DB_READONLY_PASSWORD" \
  -f /sentinel/db_roles.sql
