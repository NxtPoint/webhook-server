-- Drop legacy silver.vw_point_base view (no longer used).
-- Run on Render shell: psql $DATABASE_URL -f migrations/drop_vw_point_base.sql
-- Safe: uses IF EXISTS so it's idempotent.

DROP VIEW IF EXISTS silver.vw_point_base CASCADE;
