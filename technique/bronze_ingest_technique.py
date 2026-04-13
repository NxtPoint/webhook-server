# technique/bronze_ingest_technique.py — Extract technique API response into bronze tables.
#
# Called after the technique API returns a "done" payload.
# Mirrors ingest_bronze.py patterns: idempotent, replace-on-reingest,
# each section extracted into its own table.

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from sqlalchemy import text as sql_text

log = logging.getLogger(__name__)


def ingest_technique_bronze(
    conn,
    payload: Dict[str, Any],
    task_id: str,
    replace: bool = True,
) -> Dict[str, Any]:
    """
    Extract technique API response JSON into bronze.technique_* tables.

    Args:
        conn: SQLAlchemy connection (inside a transaction).
        payload: The full API response dict (status == "done").
        task_id: The task identifier.
        replace: If True, delete existing rows for this task_id first.

    Returns:
        Dict with counts of inserted rows per table.
    """
    counts = {}

    if replace:
        for table in (
            "technique_analysis_metadata",
            "technique_features",
            "technique_feature_categories",
            "technique_kinetic_chain",
            "technique_wrist_speed",
            "technique_pose_2d",
            "technique_pose_3d",
        ):
            conn.execute(
                sql_text(f"DELETE FROM bronze.{table} WHERE task_id = :t"),
                {"t": task_id},
            )

    # ── 1. Metadata ────────────────────────────────────────────
    uid = payload.get("uid") or ""
    status = payload.get("status") or ""
    warnings = payload.get("warnings") or []
    errors = payload.get("errors") or []

    # Extract request metadata from raw_meta stored in submission_context
    result = payload.get("result") or {}

    conn.execute(sql_text("""
        INSERT INTO bronze.technique_analysis_metadata
            (task_id, uid, status, warnings, errors, raw_meta)
        VALUES (:task_id, :uid, :status, :warnings, :errors, :raw_meta)
        ON CONFLICT (task_id) DO UPDATE SET
            uid = EXCLUDED.uid,
            status = EXCLUDED.status,
            warnings = EXCLUDED.warnings,
            errors = EXCLUDED.errors,
            raw_meta = EXCLUDED.raw_meta
    """), {
        "task_id": task_id,
        "uid": uid,
        "status": status,
        "warnings": json.dumps(warnings),
        "errors": json.dumps(errors),
        "raw_meta": json.dumps({
            k: payload[k] for k in payload
            if k not in ("result", "video_entry_2D_json", "video_entry_3D_json")
        }),
    })
    counts["technique_analysis_metadata"] = 1

    # ── 2. Features ────────────────────────────────────────────
    features = result.get("features") or []
    for feat in features:
        conn.execute(sql_text("""
            INSERT INTO bronze.technique_features
                (task_id, feature_name, feature_human_readable, level, score,
                 value, observation, suggestion, feature_categories,
                 highlight_joints, highlight_limbs, event_name,
                 event_timestamp, event_frame_nr, score_ranges, value_ranges)
            VALUES
                (:task_id, :feature_name, :feature_human_readable, :level, :score,
                 :value, :observation, :suggestion, :feature_categories,
                 :highlight_joints, :highlight_limbs, :event_name,
                 :event_timestamp, :event_frame_nr, :score_ranges, :value_ranges)
        """), {
            "task_id": task_id,
            "feature_name": feat.get("feature_name"),
            "feature_human_readable": feat.get("feature_human_readable"),
            "level": feat.get("level"),
            "score": _as_float(feat.get("score")),
            "value": _as_float(feat.get("value")),
            "observation": feat.get("observation"),
            "suggestion": feat.get("suggestion"),
            "feature_categories": json.dumps(feat.get("feature_categories")) if feat.get("feature_categories") else None,
            "highlight_joints": json.dumps(feat.get("highlight_joints")) if feat.get("highlight_joints") else None,
            "highlight_limbs": json.dumps(feat.get("highlight_limbs")) if isinstance(feat.get("highlight_limbs"), dict) else None,
            "event_name": feat.get("event_name"),
            "event_timestamp": _as_float(feat.get("event_timestamp")),
            "event_frame_nr": _as_int(feat.get("event_frame_nr")),
            "score_ranges": json.dumps(feat.get("score_ranges")) if feat.get("score_ranges") else None,
            "value_ranges": json.dumps(feat.get("value_ranges")) if feat.get("value_ranges") else None,
        })
    counts["technique_features"] = len(features)

    # ── 3. Feature categories ──────────────────────────────────
    feature_categories = result.get("feature_categories") or {}
    cat_count = 0
    for cat_name, cat_data in feature_categories.items():
        cat_score = None
        cat_features = None
        raw = cat_data

        if isinstance(cat_data, dict):
            cat_score = _as_float(cat_data.get("score") or cat_data.get("category_score"))
            cat_features = cat_data.get("features") or cat_data.get("feature_names")
            raw = cat_data
        elif isinstance(cat_data, (int, float)):
            cat_score = float(cat_data)
            raw = {"score": cat_data}

        conn.execute(sql_text("""
            INSERT INTO bronze.technique_feature_categories
                (task_id, category_name, category_score, feature_names, raw_data)
            VALUES (:task_id, :category_name, :category_score, :feature_names, :raw_data)
        """), {
            "task_id": task_id,
            "category_name": cat_name,
            "category_score": cat_score,
            "feature_names": json.dumps(cat_features) if cat_features else None,
            "raw_data": json.dumps(raw),
        })
        cat_count += 1
    counts["technique_feature_categories"] = cat_count

    # ── 4. Kinetic chain ───────────────────────────────────────
    kinetic_chain = result.get("kinetic_chain") or {}
    speed_dict = kinetic_chain.get("speed_dict") or kinetic_chain
    seg_count = 0

    if isinstance(speed_dict, dict):
        for seg_name, seg_data in speed_dict.items():
            if seg_name in ("speed_dict",):
                continue

            peak_speed = None
            peak_ts = None
            plot_vals = None

            if isinstance(seg_data, dict):
                peak_speed = _as_float(seg_data.get("peak_speed") or seg_data.get("peak"))
                peak_ts = _as_float(seg_data.get("peak_timestamp") or seg_data.get("peak_time"))
                plot_vals = seg_data.get("plot_values") or seg_data.get("values")
            elif isinstance(seg_data, (int, float)):
                peak_speed = float(seg_data)

            conn.execute(sql_text("""
                INSERT INTO bronze.technique_kinetic_chain
                    (task_id, segment_name, peak_speed, peak_timestamp, plot_values, raw_data)
                VALUES (:task_id, :segment_name, :peak_speed, :peak_timestamp, :plot_values, :raw_data)
            """), {
                "task_id": task_id,
                "segment_name": seg_name,
                "peak_speed": peak_speed,
                "peak_timestamp": peak_ts,
                "plot_values": json.dumps(plot_vals) if plot_vals else None,
                "raw_data": json.dumps(seg_data) if seg_data else None,
            })
            seg_count += 1
    counts["technique_kinetic_chain"] = seg_count

    # ── 5. Wrist speed ─────────────────────────────────────────
    wrist_speed = result.get("wrist_speed")
    if wrist_speed:
        conn.execute(sql_text("""
            INSERT INTO bronze.technique_wrist_speed (task_id, raw_data)
            VALUES (:task_id, :raw_data)
            ON CONFLICT (task_id) DO UPDATE SET raw_data = EXCLUDED.raw_data
        """), {
            "task_id": task_id,
            "raw_data": json.dumps(wrist_speed),
        })
        counts["technique_wrist_speed"] = 1
    else:
        counts["technique_wrist_speed"] = 0

    # ── 6. Pose 2D ─────────────────────────────────────────────
    pose_2d = payload.get("video_entry_2D_json")
    if pose_2d:
        frame_count = len(pose_2d) if isinstance(pose_2d, (list, dict)) else None
        conn.execute(sql_text("""
            INSERT INTO bronze.technique_pose_2d (task_id, frame_count, raw_data)
            VALUES (:task_id, :frame_count, :raw_data)
            ON CONFLICT (task_id) DO UPDATE SET
                frame_count = EXCLUDED.frame_count,
                raw_data = EXCLUDED.raw_data
        """), {
            "task_id": task_id,
            "frame_count": frame_count,
            "raw_data": json.dumps(pose_2d),
        })
        counts["technique_pose_2d"] = 1
    else:
        counts["technique_pose_2d"] = 0

    # ── 7. Pose 3D ─────────────────────────────────────────────
    pose_3d = payload.get("video_entry_3D_json")
    if pose_3d:
        frame_count = len(pose_3d) if isinstance(pose_3d, (list, dict)) else None
        conn.execute(sql_text("""
            INSERT INTO bronze.technique_pose_3d (task_id, frame_count, raw_data)
            VALUES (:task_id, :frame_count, :raw_data)
            ON CONFLICT (task_id) DO UPDATE SET
                frame_count = EXCLUDED.frame_count,
                raw_data = EXCLUDED.raw_data
        """), {
            "task_id": task_id,
            "frame_count": frame_count,
            "raw_data": json.dumps(pose_3d),
        })
        counts["technique_pose_3d"] = 1
    else:
        counts["technique_pose_3d"] = 0

    log.info(
        "TECHNIQUE BRONZE INGEST task_id=%s counts=%s",
        task_id, counts,
    )
    return {"task_id": task_id, "counts": counts}


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
