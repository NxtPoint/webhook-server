# reingest_fix_v2.py
import os, json, gzip
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["DATABASE_URL"])

TABLES = [
    # table_name, needs_data_col?
    ("highlight", True),
    ("team_session", True),
    ("bounce_heatmap", True),
]

def ensure_table(conn, tbl: str, needs_data: bool):
    # Create table if missing (minimal schema)
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {tbl} (
          session_id INT NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE
        );
    """))
    # Add columns if missing
    if needs_data:
        conn.execute(text(f"""
            DO $$ BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='{tbl}' AND column_name='data'
              ) THEN
                ALTER TABLE {tbl} ADD COLUMN data JSONB;
              END IF;
            END $$;
        """))
    conn.execute(text(f"""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='{tbl}' AND column_name='id'
          ) THEN
            ALTER TABLE {tbl} ADD COLUMN id BIGSERIAL;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='{tbl}' AND column_name='created_at'
          ) THEN
            ALTER TABLE {tbl} ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
          END IF;
        END $$;
    """))

def dedupe(conn, tbl: str):
    # Keep the newest row per session_id (by created_at desc, then id desc)
    conn.execute(text(f"""
        WITH ranked AS (
          SELECT ctid
               , ROW_NUMBER() OVER (
                   PARTITION BY session_id
                   ORDER BY created_at DESC NULLS LAST, id DESC NULLS LAST
                 ) AS rn
          FROM {tbl}
        ),
        d AS (SELECT ctid FROM ranked WHERE rn > 1)
        DELETE FROM {tbl} t USING d
        WHERE t.ctid = d.ctid;
    """))

def add_unique(conn, tbl: str):
    # Add a unique constraint on session_id (if not already present)
    conn.execute(text(f"""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM   pg_indexes
            WHERE  tablename = '{tbl}'
            AND    indexname = '{tbl}_session_id_key'
          ) THEN
            BEGIN
              ALTER TABLE {tbl} ADD CONSTRAINT {tbl}_session_id_key UNIQUE(session_id);
            EXCEPTION WHEN duplicate_object THEN
              -- Another unique already exists; ignore
              NULL;
            END;
          END IF;
        END $$;
    """))

def run_schema_fixes():
    with engine.begin() as conn:
        for tbl, needs_data in TABLES:
            ensure_table(conn, tbl, needs_data)
            dedupe(conn, tbl)
            add_unique(conn, tbl)
    print("[Schema] Optional tables ensured, deduped, and unique constraints added.")

def load_raw_payload(conn, session_id):
    row = conn.execute(text("""
        SELECT payload_json, payload_gzip
        FROM raw_result
        WHERE session_id=:sid
        ORDER BY created_at DESC
        LIMIT 1
    """), {"sid": session_id}).first()
    if not row:
        raise RuntimeError("No raw_result found for session " + str(session_id))
    pj, gz = row[0], row[1]
    if pj is not None:
        return pj if isinstance(pj, dict) else json.loads(pj)
    if gz is not None:
        return json.loads(gzip.decompress(gz).decode("utf-8"))
    raise RuntimeError("raw_result had neither JSON nor GZIP")

def reingest(sid):
    from ingest_app import ingest_result_v2
    with engine.begin() as conn:
        payload = load_raw_payload(conn, sid)
        print("[Re-ingest] Payload keys:", list(payload.keys()))
        ingest_result_v2(conn, payload, replace=True, src_hint="raw://reingest")
        counts = conn.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM dim_rally            WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_bounce          WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_ball_position   WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid),
              (SELECT COUNT(*) FROM fact_swing           WHERE session_id=:sid)
        """), {"sid": sid}).fetchone()
    print("[Bronze] Rallies:", counts[0], "Bounces:", counts[1],
          "BallPos:", counts[2], "PlayerPos:", counts[3], "Swings:", counts[4])

def check_optional_counts(sid):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM session_confidences WHERE session_id=:sid) AS confidences,
              (SELECT COUNT(*) FROM thumbnail            WHERE session_id=:sid) AS thumbnails,
              (SELECT COUNT(*) FROM highlight            WHERE session_id=:sid) AS highlights,
              (SELECT COUNT(*) FROM bounce_heatmap       WHERE session_id=:sid) AS heatmap,
              (SELECT COUNT(*) FROM team_session         WHERE session_id=:sid) AS team_sessions
        """), {"sid": sid}).first()
    print("[Optional] confidences:", row[0],
          "thumbnails:", row[1],
          "highlights:", row[2],
          "heatmap:", row[3],
          "team_sessions:", row[4])

if __name__ == "__main__":
    print("[Start] Applying schema fixes…")
    run_schema_fixes()
    sid = 1174
    print("[Start] Re-ingesting session", sid, "…")
    reingest(sid)
    check_optional_counts(sid)
