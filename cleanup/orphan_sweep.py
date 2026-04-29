"""Orphan sweep — periodic mop-up for the soft-delete cascade.

Two passes:

  1. Soft-deleted parents: any bronze.submission_context row with
     deleted_at IS NOT NULL whose child rows still exist (e.g. because a
     worker raced past the live DELETE endpoint).
  2. True orphans: child rows whose task_id has no matching
     submission_context row at all (early test inserts, race conditions
     that left the parent un-created).

Endpoint: POST /ops/orphan-sweep — OPS_KEY-gated. Optional body:
  {"dry_run": true}     → report counts only, change nothing
  {"include_orphans": false}  → run pass 1 only

The endpoint is idempotent. Repeated calls will trim any new leftovers and
otherwise return all-zeros. NEVER touches billing.* — the match was a real
billing event regardless of whether its bronze rows survive.
"""

from __future__ import annotations

import logging
from typing import Iterable

from flask import Blueprint, jsonify, request, Response
from sqlalchemy import text as sql_text

from db_init import engine

log = logging.getLogger(__name__)

bp = Blueprint("orphan_sweep", __name__)


# Cascade mirror — keep in sync with client_api.delete_match.
# Tables keyed directly on task_id (text uuid).
_CHILD_TABLES = (
    ("tennis_coach", "coach_cache"),

    ("silver", "point_detail"),
    ("silver", "practice_detail"),
    ("silver", "technique_summary"),
    ("silver", "technique_features_enriched"),
    ("silver", "technique_kinetic_chain_analysis"),
    ("silver", "technique_pose_timeline"),

    ("ml_analysis", "match_analytics"),
    ("ml_analysis", "serve_events"),

    ("bronze", "technique_pose_3d"),
    ("bronze", "technique_pose_2d"),
    ("bronze", "technique_wrist_speed"),
    ("bronze", "technique_kinetic_chain"),
    ("bronze", "technique_feature_categories"),
    ("bronze", "technique_features"),
    ("bronze", "technique_analysis_metadata"),

    ("bronze", "raw_result_chunk"),
    ("bronze", "raw_result"),
    ("bronze", "player"),
    ("bronze", "player_swing"),
    ("bronze", "rally"),
    ("bronze", "ball_position"),
    ("bronze", "ball_bounce"),
    ("bronze", "unmatched_field"),
    ("bronze", "debug_event"),
    ("bronze", "player_position"),
    ("bronze", "session"),
    ("bronze", "session_confidences"),
    ("bronze", "thumbnail"),
    ("bronze", "highlight"),
    ("bronze", "team_session"),
    ("bronze", "bounce_heatmap"),
)


def _table_exists(conn, schema: str, table: str) -> bool:
    return conn.execute(sql_text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :s AND table_name = :t"
    ), {"s": schema, "t": table}).scalar() is not None


def _column_exists(conn, schema: str, table: str, column: str) -> bool:
    return conn.execute(sql_text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
    ), {"s": schema, "t": table, "c": column}).scalar() is not None


def _guard_ops() -> bool:
    """Mirror upload_app._guard — header-only OPS_KEY auth."""
    import os
    import hmac
    expected = (os.getenv("OPS_KEY") or "").strip()
    if not expected:
        return False
    candidates = [
        request.headers.get("X-Ops-Key"),
        request.headers.get("X-OPS-Key"),
    ]
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        candidates.append(auth.split(None, 1)[1])
    for c in candidates:
        if c and hmac.compare_digest(c.strip(), expected):
            return True
    return False


def _sweep_soft_deleted(conn, dry_run: bool) -> dict:
    """Pass 1 — child rows whose parent submission_context.deleted_at is set."""
    if not _column_exists(conn, "bronze", "submission_context", "deleted_at"):
        return {"task_ids": 0, "rows_deleted": {}, "skipped": "deleted_at column missing"}

    kill_ids = [str(r[0]) for r in conn.execute(sql_text(
        "SELECT task_id::text FROM bronze.submission_context "
        "WHERE deleted_at IS NOT NULL"
    )).fetchall()]
    if not kill_ids:
        return {"task_ids": 0, "rows_deleted": {}}

    counts: dict = {}
    for schema, table in _CHILD_TABLES:
        if not _table_exists(conn, schema, table):
            continue
        try:
            if dry_run:
                n = conn.execute(sql_text(
                    f"SELECT count(*) FROM {schema}.{table} "
                    f"WHERE task_id::text = ANY(:ids)"
                ), {"ids": kill_ids}).scalar() or 0
            else:
                res = conn.execute(sql_text(
                    f"DELETE FROM {schema}.{table} "
                    f"WHERE task_id::text = ANY(:ids)"
                ), {"ids": kill_ids})
                n = res.rowcount or 0
            if n:
                counts[f"{schema}.{table}"] = n
        except Exception as e:
            counts[f"{schema}.{table}"] = f"error: {e.__class__.__name__}"[:80]

    # ml_analysis detection rows are keyed on job_id, resolve via task_id.
    if _table_exists(conn, "ml_analysis", "video_analysis_jobs"):
        for det in ("ball_detections", "player_detections"):
            if not _table_exists(conn, "ml_analysis", det):
                continue
            try:
                if dry_run:
                    n = conn.execute(sql_text(
                        f"SELECT count(*) FROM ml_analysis.{det} WHERE job_id IN ("
                        "SELECT job_id FROM ml_analysis.video_analysis_jobs "
                        "WHERE task_id::text = ANY(:ids))"
                    ), {"ids": kill_ids}).scalar() or 0
                else:
                    res = conn.execute(sql_text(
                        f"DELETE FROM ml_analysis.{det} WHERE job_id IN ("
                        "SELECT job_id FROM ml_analysis.video_analysis_jobs "
                        "WHERE task_id::text = ANY(:ids))"
                    ), {"ids": kill_ids})
                    n = res.rowcount or 0
                if n:
                    counts[f"ml_analysis.{det}"] = n
            except Exception as e:
                counts[f"ml_analysis.{det}"] = f"error: {e.__class__.__name__}"[:80]

        # video_analysis_jobs itself.
        try:
            if dry_run:
                n = conn.execute(sql_text(
                    "SELECT count(*) FROM ml_analysis.video_analysis_jobs "
                    "WHERE task_id::text = ANY(:ids)"
                ), {"ids": kill_ids}).scalar() or 0
            else:
                res = conn.execute(sql_text(
                    "DELETE FROM ml_analysis.video_analysis_jobs "
                    "WHERE task_id::text = ANY(:ids)"
                ), {"ids": kill_ids})
                n = res.rowcount or 0
            if n:
                counts["ml_analysis.video_analysis_jobs"] = n
        except Exception as e:
            counts["ml_analysis.video_analysis_jobs"] = f"error: {e.__class__.__name__}"[:80]

    return {"task_ids": len(kill_ids), "rows_deleted": counts}


def _sweep_orphans(conn, dry_run: bool) -> dict:
    """Pass 2 — child rows whose task_id has no matching submission_context."""
    counts: dict = {}
    for schema, table in _CHILD_TABLES:
        if not _table_exists(conn, schema, table):
            continue
        if not _column_exists(conn, schema, table, "task_id"):
            continue
        try:
            if dry_run:
                n = conn.execute(sql_text(
                    f"SELECT count(*) FROM {schema}.{table} t "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM bronze.submission_context sc "
                    "  WHERE sc.task_id::text = t.task_id::text)"
                )).scalar() or 0
            else:
                res = conn.execute(sql_text(
                    f"DELETE FROM {schema}.{table} t "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM bronze.submission_context sc "
                    "  WHERE sc.task_id::text = t.task_id::text)"
                ))
                n = res.rowcount or 0
            if n:
                counts[f"{schema}.{table}"] = n
        except Exception as e:
            counts[f"{schema}.{table}"] = f"error: {e.__class__.__name__}"[:80]
    return {"rows_deleted": counts}


@bp.post("/ops/orphan-sweep")
def orphan_sweep():
    if not _guard_ops():
        return Response("Forbidden", 403)

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", False))
    include_orphans = bool(body.get("include_orphans", True))

    try:
        with engine.begin() as conn:
            soft = _sweep_soft_deleted(conn, dry_run=dry_run)
            orph = _sweep_orphans(conn, dry_run=dry_run) if include_orphans else None
    except Exception as e:
        log.exception("orphan-sweep failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500

    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "soft_deleted_pass": soft,
        "orphan_pass": orph,
    })
