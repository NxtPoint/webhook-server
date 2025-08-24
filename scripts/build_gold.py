# scripts/build_gold.py
import os
from sqlalchemy import create_engine, text

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    raise SystemExit("Set DATABASE_URL env var (e.g. postgres://user:pass@host:5432/db)")

engine = create_engine(DB_URL, pool_pre_ping=True)

DDL = [
    # Rebuild point_log_tbl
    "DROP TABLE IF EXISTS point_log_tbl;",
    "CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log;",
    # Rebuild point_summary_tbl
    "DROP TABLE IF EXISTS point_summary_tbl;",
    "CREATE TABLE point_summary_tbl AS SELECT * FROM vw_point_summary;",
    # Indexes for PBI performance
    "CREATE INDEX IF NOT EXISTS ix_pl_session ON point_log_tbl(session_uid, point_number, shot_number);",
    "CREATE INDEX IF NOT EXISTS ix_ps_session ON point_summary_tbl(session_uid, point_number);",
]

with engine.begin() as conn:
    for stmt in DDL:
        conn.execute(text(stmt))

print("Gold tables rebuilt: point_log_tbl, point_summary_tbl")
