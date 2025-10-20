# db_init.py
# Unified Bronze ingestion for SportAI payloads (all towers + dim/fact).
# - Exposes module-level `engine` for import by other modules.
# - Schema-aware: maps JSON keys to existing table columns; leftovers -> JSONB meta if present.
# - Idempotent upserts where a table is 1-row-per-session.

import os
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Connection

# ------------------------------------------------------------------------------
# ENGINE (module-level)  --> fixes "cannot import name 'engine'" error
# ------------------------------------------------------------------------------
def _get_db_uri() -> str:
    uri = os.getenv("SQLALCHEMY_DATABASE_URI") or os.getenv("DATABASE_URL")
    if not uri:
        raise RuntimeError("Set SQLALCHEMY_DATABASE_URI or DATABASE_URL")
    return uri

engine: Engine = create_engine(_get_db_uri(), pool_pre_ping=True, future=True)

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
JSONB_META_CANDIDATES = ("meta", "data", "payload", "extra", "annotations")

def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []

def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def _json(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False)

def _castable_json(value: Any) -> Tuple[Any, Optional[str]]:
    """
    Returns (param_value, cast_hint).
    If value should be inserted as JSONB, returns (json_str, 'JSONB').
    Otherwise (value, None).
    """
    if isinstance(value, (dict, list)):
        return _json(value), "JSONB"
    return value, None

# ------------------------------------------------------------------------------
# Schema Introspection & Dynamic Inserts
# ------------------------------------------------------------------------------
def get_table_columns(conn: Connection, table: str, schema: Optional[str] = None) -> List[str]:
    q = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = :tname
          AND (:tschema IS NULL OR table_schema = :tschema)
        ORDER BY ordinal_position
    """)
    rows = conn.execute(q, {"tname": table, "tschema": schema}).fetchall()
    return [r[0] for r in rows]

def detect_jsonb_meta_column(columns: List[str]) -> Optional[str]:
    # Prefer 'meta' when available; otherwise first candidate that exists
    for c in JSONB_META_CANDIDATES:
        if c in columns:
            return c
    return None

def insert_dynamic(
    conn: Connection,
    table: str,
    payload: Dict[str, Any],
    fixed_fields: Dict[str, Any],
    schema: Optional[str] = None,
    upsert_on: Optional[List[str]] = None,
    skip_if_all_null: bool = False,
) -> None:
    """
    Inserts a row into `table`:
      - Intersects JSON keys with actual columns to form the row
      - Adds `fixed_fields` (e.g., session_id, player_id) overriding JSON
      - Unmapped keys go into JSONB meta column if present
      - upsert_on: list of column names to use for ON CONFLICT
    """
    cols = get_table_columns(conn, table, schema)
    meta_col = detect_jsonb_meta_column(cols)

    # merge fixed_fields first (they win)
    row: Dict[str, Any] = {}
    row.update({k: v for k, v in payload.items() if k in cols})
    row.update({k: v for k, v in fixed_fields.items() if k in cols})

    # meta packing for leftovers
    leftovers = {}
    if meta_col:
        for k, v in payload.items():
            if k not in row and k != meta_col:
                leftovers[k] = v
        if leftovers:
            row[meta_col] = leftovers

    if skip_if_all_null and all(v is None for v in row.values()):
        return

    # Build SQL dynamically with JSONB casts where needed
    col_names = list(row.keys())
    if not col_names:
        return
    placeholders = []
    params: Dict[str, Any] = {}
    casts: Dict[str, str] = {}

    for i, c in enumerate(col_names, 1):
        p = f"p{i}"
        val, cast_hint = _castable_json(row[c])
        params[p] = val
        placeholders.append(f":{p}::jsonb" if cast_hint == "JSONB" else f":{p}")
        if cast_hint:
            casts[c] = cast_hint

    full_table = f"{schema}.{table}" if schema else table
    insert_sql = f"INSERT INTO {full_table} ({', '.join(col_names)}) VALUES ({', '.join(placeholders)})"

    if upsert_on:
        set_cols = [c for c in col_names if c not in upsert_on]
        if set_cols:
            set_clause = ", ".join([f"{c}=EXCLUDED.{c}" for c in set_cols])
            insert_sql += f" ON CONFLICT ({', '.join(upsert_on)}) DO UPDATE SET {set_clause}"
        else:
            insert_sql += f" ON CONFLICT ({', '.join(upsert_on)}) DO NOTHING"

    conn.execute(text(insert_sql), params)

# ------------------------------------------------------------------------------
# Tower Upserts (1-row-per-session where relevant)
# ------------------------------------------------------------------------------
def upsert_bounce_heatmap(conn: Connection, session_id: int, root: Dict[str, Any]) -> None:
    table = "bounce_heatmap"
    cols = get_table_columns(conn, table)
    if "session_id" not in cols:
        return
    obj = _as_dict(root.get("bounce_heatmap"))
    if not obj:
        return
    # map to 'heatmap' if exists; else pack into meta-like col
    payload = {}
    if "heatmap" in cols:
        payload["heatmap"] = obj
    meta_col = detect_jsonb_meta_column(cols)
    if not payload and meta_col:
        payload[meta_col] = obj
    insert_dynamic(conn, table, payload, {"session_id": session_id}, upsert_on=["session_id"])

def upsert_session_confidences(conn: Connection, session_id: int, root: Dict[str, Any]) -> None:
    table = "session_confidences"
    cols = get_table_columns(conn, table)
    if "session_id" not in cols:
        return
    obj = _as_dict(root.get("confidences") or root.get("confidence"))
    if not obj:
        return
    payload = {}
    if "data" in cols:
        payload["data"] = obj
    meta_col = detect_jsonb_meta_column(cols)
    if not payload and meta_col:
        payload[meta_col] = obj
    insert_dynamic(conn, table, payload, {"session_id": session_id}, upsert_on=["session_id"])

def upsert_thumbnail(conn: Connection, session_id: int, root: Dict[str, Any]) -> None:
    table = "thumbnail"
    cols = get_table_columns(conn, table)
    if "session_id" not in cols:
        return
    thumbs = root.get("thumbnails") or root.get("thumbnail")
    crops = None
    if isinstance(thumbs, dict):
        crops = thumbs.get("crops") or thumbs.get("crop") or thumbs
    else:
        crops = thumbs
    if crops is None:
        return
    payload = {}
    if "crops" in cols:
        payload["crops"] = crops
    meta_col = detect_jsonb_meta_column(cols)
    if not payload and meta_col:
        payload[meta_col] = thumbs
    insert_dynamic(conn, table, payload, {"session_id": session_id}, upsert_on=["session_id"])

def insert_team_sessions(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "team_session"
    arr = _as_list(root.get("team_sessions") or root.get("team_session"))
    if not arr:
        return 0
    cols = get_table_columns(conn, table)
    cnt = 0
    for item in arr:
        payload = {}
        if "data" in cols:
            payload["data"] = item
        insert_dynamic(conn, table, payload, {"session_id": session_id})
        cnt += 1
    return cnt

def insert_highlights(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "highlight"
    arr = _as_list(root.get("highlights") or root.get("highlight"))
    if not arr:
        return 0
    cols = get_table_columns(conn, table)
    cnt = 0
    for item in arr:
        payload = {}
        if "data" in cols:
            payload["data"] = item
        insert_dynamic(conn, table, payload, {"session_id": session_id})
        cnt += 1
    return cnt

# ------------------------------------------------------------------------------
# Dim tables
# ------------------------------------------------------------------------------
def upsert_dim_session(conn: Connection, session_id: int, root: Dict[str, Any]) -> None:
    table = "dim_session"
    cols = get_table_columns(conn, table)
    if "session_id" not in cols:
        return
    payload = {}
    # If the schema has these columns, populate them:
    for k in ("session_uid", "session_uid_d", "created_at", "play_d"):
        if k in cols and k in root:
            payload[k] = root.get(k)
    # Pack top-level meta if present
    meta = _as_dict(root.get("meta") or root.get("metadata"))
    if "meta" in cols and meta:
        payload["meta"] = meta
    # Required PK
    insert_dynamic(conn, table, payload, {"session_id": session_id}, upsert_on=["session_id"])

def insert_dim_players(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "dim_player"
    cols = get_table_columns(conn, table)
    players = _as_list(root.get("players"))
    if not players:
        return 0
    cnt = 0
    for p in players:
        fixed = {"session_id": session_id}
        # Common expected fields if present
        for k in ("player_id", "id", "label", "full_name", "handedness", "team", "server", "server_label", "player_label"):
            if k in p:
                fixed.setdefault("player_id", p.get("player_id") or p.get("id"))
                if "full_name" in cols and "full_name" in p:
                    fixed["full_name"] = p["full_name"]
                if "player_label" in cols and (p.get("label") or p.get("player_label")):
                    fixed["player_label"] = p.get("label") or p.get("player_label")

        payload = {}
        # swing_type_distribution frequently appears under each player
        std = p.get("swing_type_distribution") or p.get("swing_types")
        if "swing_type_distribution" in cols and std is not None:
            payload["swing_type_distribution"] = std

        insert_dynamic(conn, table, payload or p, fixed)
        cnt += 1
    return cnt

def insert_dim_rallies(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "dim_rally"
    cols = get_table_columns(conn, table)
    rallies = _as_list(root.get("rallies"))
    if not rallies:
        return 0
    cnt = 0
    for idx, r in enumerate(rallies):
        fixed = {"session_id": session_id}
        if "rally_index" in cols:
            fixed["rally_index"] = idx
        insert_dynamic(conn, table, r, fixed)
        cnt += 1
    return cnt

# ------------------------------------------------------------------------------
# Fact tables
# ------------------------------------------------------------------------------
def insert_fact_swings(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    """
    Try two sources:
      1) top-level 'swings' array (if SportAI provides it)
      2) per-rally 'shots' or 'swings' inside rallies[]
    Any unknown fields get packed into table's JSONB meta column.
    """
    table = "fact_swing"
    cols = get_table_columns(conn, table)

    def _insert_one(s: Dict[str, Any], fixed_extra: Dict[str, Any]) -> None:
        fixed = {"session_id": session_id}
        fixed.update(fixed_extra)
        # Normalize a couple of common keys if present
        alias_map = {
            "type": "swing_type",
            "speed": "ball_speed",
        }
        payload = {}
        for src, dst in alias_map.items():
            if src in s and dst in cols:
                payload[dst] = s[src]
        insert_dynamic(conn, table, payload or s, fixed)

    cnt = 0
    # Path 1: top-level swings
    for s in _as_list(root.get("swings")):
        _insert_one(_as_dict(s), {})
        cnt += 1

    # Path 2: rallies[][] -> shots/swings
    rallies = _as_list(root.get("rallies"))
    for ridx, r in enumerate(rallies):
        shots = _as_list(_as_dict(r).get("shots") or _as_dict(r).get("swings") or [])
        for sidx, s in enumerate(shots):
            _insert_one(_as_dict(s), {"rally_index": ridx, "shot_index": sidx})
            cnt += 1

    return cnt

def insert_fact_bounces(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "fact_bounce"
    bounces = _as_list(_as_dict(root.get("ball") or {}).get("bounces"))
    cnt = 0
    for idx, b in enumerate(bounces):
        insert_dynamic(conn, table, _as_dict(b), {"session_id": session_id, "bounce_index": idx})
        cnt += 1
    return cnt

def insert_fact_ball_positions(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "fact_ball_position"
    positions = _as_list(_as_dict(root.get("ball") or {}).get("positions"))
    cnt = 0
    for idx, pos in enumerate(positions):
        insert_dynamic(conn, table, _as_dict(pos), {"session_id": session_id, "position_index": idx})
        cnt += 1
    return cnt

def insert_fact_player_positions(conn: Connection, session_id: int, root: Dict[str, Any]) -> int:
    table = "fact_player_position"
    cnt = 0
    for p in _as_list(root.get("players")):
        pid = p.get("player_id") or p.get("id")
        positions = _as_list(_as_dict(p).get("positions"))
        for idx, pos in enumerate(positions):
            insert_dynamic(
                conn,
                table,
                _as_dict(pos),
                {"session_id": session_id, "player_id": pid, "position_index": idx},
            )
            cnt += 1
    return cnt

# ------------------------------------------------------------------------------
# Submission Context (frontend form) — you said it's perfect; keep upsert
# ------------------------------------------------------------------------------
def upsert_submission_context(conn: Connection, session_id: int, root: Dict[str, Any]) -> None:
    table = "submission_context"
    cols = get_table_columns(conn, table)
    if "session_id" not in cols:
        return
    sub_ctx = _as_dict(root.get("submission_context") or root.get("submission"))
    if not sub_ctx:
        return
    payload = {}
    if "data" in cols:
        payload["data"] = sub_ctx
    insert_dynamic(conn, table, payload, {"session_id": session_id}, upsert_on=["session_id"])

# ------------------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------------------
def ingest_all_for_session(conn: Connection, session_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Complete Bronze ingest for one session across all towers + dim/fact.
    Returns a summary for quick verification.
    """
    summary = {"session_id": session_id}

    # Dims first
    upsert_dim_session(conn, session_id, payload)
    summary["players"] = insert_dim_players(conn, session_id, payload)
    summary["rallies"] = insert_dim_rallies(conn, session_id, payload)

    # Facts
    summary["swings"] = insert_fact_swings(conn, session_id, payload)
    summary["ball_bounces"] = insert_fact_bounces(conn, session_id, payload)
    summary["ball_positions"] = insert_fact_ball_positions(conn, session_id, payload)
    summary["player_positions"] = insert_fact_player_positions(conn, session_id, payload)

    # Towers
    summary["team_sessions"] = insert_team_sessions(conn, session_id, payload)
    summary["highlights"] = insert_highlights(conn, session_id, payload)
    upsert_bounce_heatmap(conn, session_id, payload)
    upsert_session_confidences(conn, session_id, payload)
    upsert_thumbnail(conn, session_id, payload)
    upsert_submission_context(conn, session_id, payload)

    return summary

# ------------------------------------------------------------------------------
# Backfill from raw_result
# ------------------------------------------------------------------------------
def backfill_from_raw_result(conn: Connection, only_session_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if only_session_id is None:
        q = text("""
            SELECT session_id, payload_json
            FROM raw_result
            WHERE payload_json IS NOT NULL
            ORDER BY session_id, created_at DESC
        """)
        rows = conn.execute(q).mappings().all()
    else:
        q = text("""
            SELECT session_id, payload_json
            FROM raw_result
            WHERE session_id = :sid AND payload_json IS NOT NULL
            ORDER BY created_at DESC
        """)
        rows = conn.execute(q, {"sid": only_session_id}).mappings().all()

    seen = set()
    summaries: List[Dict[str, Any]] = []
    for r in rows:
        sid = int(r["session_id"])
        if sid in seen:
            continue
        seen.add(sid)
        payload = dict(r["payload_json"])
        summaries.append(ingest_all_for_session(conn, sid, payload))
    return summaries

# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------
def run_single(session_id: int, payload_json_str: str) -> None:
    payload = json.loads(payload_json_str)
    with engine.begin() as conn:
        out = ingest_all_for_session(conn, session_id, payload)
    print(json.dumps(out, indent=2))

def run_backfill(session_id: Optional[int] = None) -> None:
    with engine.begin() as conn:
        out = backfill_from_raw_result(conn, session_id)
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
        run_single(int(args[1]), args[2])
    elif cmd == "backfill":
        sid = int(args[1]) if len(args) >= 2 else None
        run_backfill(sid)
    else:
        raise SystemExit(f"Unknown command: {cmd}")
