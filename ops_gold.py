# ops_gold.py
from sqlalchemy import text

def build_gold(engine):
    ddl = [
        "DROP TABLE IF EXISTS point_log_tbl;",
        "CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log;",
        "DROP TABLE IF EXISTS point_summary_tbl;",
        "CREATE TABLE point_summary_tbl AS SELECT * FROM vw_point_summary;",
        "CREATE INDEX IF NOT EXISTS ix_pl_session ON point_log_tbl(session_uid, point_number, shot_number);",
        "CREATE INDEX IF NOT EXISTS ix_ps_session ON point_summary_tbl(session_uid, point_number);",
    ]
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
