# db_init.py
# Purpose: Lossless Bronze ingestion for SportAI top-level JSON "towers".
# - Leaves your submission_context logic untouched.
# - Ensures team_session, highlight, bounce_heatmap, session_confidences, thumbnail
#   are fully and correctly populated for every session.
# - Can backfill from raw_result if needed.

import os
import json
from typing import Any, Dict, Optional, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Connection

# ---- Engine -----------------------------------------------------------------

def get_engine() -> Engine:
    # Prefer SQLALCHEMY_DATABASE_URI if you’ve set it; else DATABASE_URL
    uri = os.getenv("SQLALCHEMY_DATABASE_URI") or os.getenv("DATABASE_URL")
    if not uri:
        raise RuntimeError("No DB URL found. Set SQLALCHEMY_DATABASE_URI or DATABASE_URL.")
    return create_engine(uri, pool_pre_ping=True, future=True)


# ---- Helpers ----------------------------------------------------------------

def _as_list(obj: Any) -> Iterable[Dict]:
    """Return obj as a list of dicts if possible; else empty list."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, (dict, list, str, int, float, bool)) or x is None]
    return []

def _as_dict(obj: Any) -> Dict:
    """Return obj as a dict if possible; else empty dict."""
    return obj if isinstance(obj, dict) else {}

def _json(obj: Any) -> str:
    """Safe JSON dump for SQL params."""
    return json.dumps(obj, ensure_ascii=False)


# ---- Insert / Upsert Blocks --------------------------------------------------

def upsert_team_sessions(conn: Connection, session_id: int, payload: Dict) -> int:
    """team_sessions[] -> team_session(session_id, data)"""
    arr = payload.get("team_sessions") or payload.get("team_session") or []
    count = 0
    for item in _as_list(arr):
        # One DB row per element (not unique), so simple insert
        conn.execute(
            text("""
                INSERT INTO team_session (session_id, data)
                VALUES (:sid, CAST(:data AS JSONB))
            """),
            {"sid": session_id, "data": _json(item)},
        )
        count += 1
    return count

def upsert_highlights(conn: Connection, session_id: int, payload: Dict) -> int:
    """highlights[] -> highlight(session_id, data)"""
    arr = payload.get("highlights") or payload.get("highlight") or []
    count = 0
    for item in _as_list(arr):
        conn.execute(
            text("""
                INSERT INTO highlight (session_id, data)
                VALUES (:sid, CAST(:data AS JSONB))
            """),
            {"sid": session_id, "data": _json(item)},
        )
        count += 1
    return count

def upsert_bounce_heatmap(conn: Connection, session_id: int, payload: Dict) -> bool:
    """bounce_heatmap{} -> bounce_heatmap(session_id PK, heatmap)"""
    hm = _as_dict(payload.get("bounce_heatmap"))
    if not hm:
        return False
    conn.execute(
        text("""
            INSERT INTO bounce_heatmap (session_id, heatmap)
            VALUES (:sid, CAST(:hm AS JSONB))
            ON CONFLICT (session_id) DO UPDATE
              SET heatmap = EXCLUDED.heatmap
        """),
        {"sid": session_id, "hm": _json(hm)},
    )
    return True

def upsert_session_confidences(conn: Connection, session_id: int, payload: Dict) -> bool:
    """confidences{} -> session_confidences(session_id PK, data)"""
    conf = _as_dict(payload.get("confidences") or payload.get("confidence"))
    if not conf:
        return False
    conn.execute(
        text("""
            INSERT INTO session_confidences (session_id, data)
            VALUES (:sid, CAST(:data AS JSONB))
            ON CONFLICT (session_id) DO UPDATE
              SET data = EXCLUDED.data
        """),
        {"sid": session_id, "data": _json(conf)},
    )
    return True

def upsert_thumbnail_crops(conn: Connection, session_id: int, payload: Dict) -> bool:
    """thumbnails{crops}/thumbnail{crops} -> thumbnail(session_id PK, crops)"""
    thumbs = payload.get("thumbnails") or payload.get("thumbnail") or {}
    if isinstance(thumbs, dict):
        crops = thumbs.get("crops") or thumbs.get("crop") or thumbs
    else:
        crops = thumbs  # allow rare cases where it's already the crops object/array
    if not crops:
        return False
    conn.execute(
        text("""
            INSERT INTO thumbnail (session_id, crops)
            VALUES (:sid, CAST(:crops AS JSONB))
            ON CONFLICT (session_id) DO UPDATE
              SET crops = EXCLUDED.crops
        """),
        {"sid": session_id, "crops": _json(crops)},
    )
    return True

def upsert_submission_context(conn: Connection, session_id: int, payload: Dict) -> bool:
    """
    submission_context{} -> submission_context(session_id PK, data)
    NOTE: You said this is already perfect; keeping logic equivalent.
    """
    sub_ctx = _as_dict(payload.get("submission_context") or payload.get("submission"))
    if not sub_ctx:
        return False
    conn.execute(
        text("""
            INSERT INTO submission_context (session_id, data)
            VALUES (:sid, CAST(:data AS JSONB))
            ON CONFLICT (session_id) DO UPDATE
              SET data = EXCLUDED.data
        """),
        {"sid": session_id, "data": _json(sub_ctx)},
    )
    return True


# ---- Orchestrators -----------------------------------------------------------

def ingest_bronze_towers_for_session(conn: Connection, session_id: int, payload: Dict) -> Dict[str, Any]:
    """
    Idempotent ingest of all SportAI JSON towers for a single session.
    Returns a small summary dict (counts/flags) for verification.
    """
    summary = {
        "session_id": session_id,
        "team_session_rows": 0,
        "highlight_rows": 0,
        "has_bounce_heatmap": False,
        "has_confidences": False,
        "has_thumbnails": False,
        "has_submission_context": False,
    }

    # Order doesn’t matter, but we keep it stable.
    summary["team_session_rows"] = upsert_team_sessions(conn, session_id, payload)
    summary["highlight_rows"] = upsert_highlights(conn, session_id, payload)
    summary["has_bounce_heatmap"] = upsert_bounce_heatmap(conn, session_id, payload)
    summary["has_confidences"] = upsert_session_confidences(conn, session_id, payload)
    summary["has_thumbnails"] = upsert_thumbnail_crops(conn, session_id, payload)
    # Leave as-is per your note
    summary["has_submission_context"] = upsert_submission_context(conn, session_id, payload)

    return summary


def backfill_bronze_towers_from_raw_result(conn: Connection, only_session_id: Optional[int] = None) -> Iterable[Dict[str, Any]]:
    """
    Replays the Bronze tower inserts from raw_result.payload_json
    across all (or one) sessions. Yields per-session summaries.
    """
    if only_session_id is None:
        q = text("""
            SELECT session_id, payload_json
            FROM raw_result
            WHERE payload_json IS NOT NULL
            ORDER BY session_id, created_at
        """)
        rows = conn.execute(q).mappings().all()
    else:
        q = text("""
            SELECT session_id, payload_json
            FROM raw_result
            WHERE session_id = :sid
              AND payload_json IS NOT NULL
            ORDER BY created_at
        """)
        rows = conn.execute(q, {"sid": only_session_id}).mappings().all()

    seen = set()
    for r in rows:
        sid = int(r["session_id"])
        # process each session once (latest payload wins)
        if sid in seen:
            continue
        seen.add(sid)
        payload = dict(r["payload_json"])
        yield ingest_bronze_towers_for_session(conn, sid, payload)


# ---- CLI Entrypoints ---------------------------------------------------------

def run_for_single_session(session_id: int, payload_json_str: str) -> None:
    """
    Use this to ingest a single payload you just received.
    Example:
        python db_init.py single 1072 "$(cat payload.json)"
    """
    engine = get_engine()
    payload = json.loads(payload_json_str)
    with engine.begin() as conn:
        summary = ingest_bronze_towers_for_session(conn, session_id, payload)
    print(json.dumps(summary, indent=2))


def run_backfill(optional_session_id: Optional[int] = None) -> None:
    """
    Backfill from raw_result for all sessions or one session.
    Example (all):  python db_init.py backfill
    Example (one):  python db_init.py backfill 1072
    """
    engine = get_engine()
    with engine.begin() as conn:
        out = list(backfill_bronze_towers_from_raw_result(conn, optional_session_id))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python db_init.py single <session_id> '<payload_json_string>'")
        print("  python db_init.py backfill [<session_id>]")
        raise SystemExit(1)

    cmd = args[0].lower()

    if cmd == "single":
        if len(args) < 3:
            raise SystemExit("Usage: python db_init.py single <session_id> '<payload_json_string>'")
        sid = int(args[1])
        payload_str = args[2]
        run_for_single_session(sid, payload_str)

    elif cmd == "backfill":
        sid_opt = int(args[1]) if len(args) >= 2 else None
        run_backfill(sid_opt)

    else:
        raise SystemExit(f"Unknown command: {cmd}")
