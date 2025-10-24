import os, json, gzip
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["DATABASE_URL"])

DDL = [
# --- highlight ---
"""
CREATE TABLE IF NOT EXISTS highlight (
  session_id INT NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE
);
""",
"""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='highlight' AND column_name='data'
  ) THEN
    ALTER TABLE highlight ADD COLUMN data JSONB;
  END IF;
END $$;
""",
"""
DO $$ BEGIN
  -- make session_id unique so ON CONFLICT works
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE tablename='highlight' AND indexname='highlight_session_id_key'
  ) THEN
    BEGIN
      ALTER TABLE highlight ADD CONSTRAINT highlight_session_id_key UNIQUE (session_id);
    EXCEPTION WHEN duplicate_table THEN
      -- if a PK/unique already exists under another name, ignore
      NULL;
    END;
  END IF;
END $$;
""",

# --- team_session ---
"""
CREATE TABLE IF NOT EXISTS team_session (
  session_id INT NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE
);
""",
"""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='team_session' AND column_name='data'
  ) THEN
    ALTER TABLE team_session ADD COLUMN data JSONB;
  END IF;
END $$;
""",
"""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE tablename='team_session' AND indexname='team_session_session_id_key'
  ) THEN
    BEGIN
      ALTER TABLE team_session ADD CONSTRAINT team_session_session_id_key UNIQUE (session_id);
    EXCEPTION WHEN duplicate_table THEN
      NULL;
    END;
  END IF;
END $$;
""",

# --- bounce_heatmap ---
"""
CREATE TABLE IF NOT EXISTS bounce_heatmap (
  session_id INT NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE
);
""",
"""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='bounce_heatmap' AND column_name='data'
  ) THEN
    ALTER TABLE bounce_heatmap ADD COLUMN data JSONB;
  END IF;
END $$;
""",
"""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE tablename='bounce_heatmap' AND indexname='bounce_heatmap_session_id_key'
  ) THEN
    BEGIN
      ALTER TABLE bounce_heatmap ADD CONSTRAINT bounce_heatmap_session_id_key UNIQUE (session_id);
    EXCEPTION WHEN duplicate_table THEN
      NULL;
    END;
  END IF;
END $$;
"""
]

def run_schema_fixes():
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))
    print("[Schema] Optional tables ensured + unique constraints added.")

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
    raise RuntimeError("raw_result had neither JSON nor GZIP data")

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
    print("[Done] Bronze counts -> Rallies:", counts[0], "Bounces:", counts[1],
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
    print("[Optional counts] confidences:", row[0],
          "thumbnails:", row[1],
          "highlights:", row[2],
          "heatmap:", row[3],
          "team_sessions:", row[4])

print("[Start] Applying schema fixes…")
run_schema_fixes()
sid = 1174
print("[Start] Re-ingesting session", sid, "…")
reingest(sid)
check_optional_counts(sid)

