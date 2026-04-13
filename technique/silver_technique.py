# technique/silver_technique.py — Build silver-layer analytical tables for technique analysis.
#
# Silver tables are populated by Python (not SQL views) from bronze.technique_* tables.
# Pattern mirrors build_silver_v2.py: ensure schema → delete existing → insert enriched rows.
#
# Tables:
#   silver.technique_summary          — per-analysis summary with overall scores
#   silver.technique_features_enriched — features joined with categories + enrichments
#   silver.technique_kinetic_chain_analysis — peak speeds, sequencing, timing
#   silver.technique_pose_timeline    — consolidated 2D/3D pose data
#   silver.technique_trends           — cross-session trends (email-scoped)

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from sqlalchemy import text as sql_text

log = logging.getLogger(__name__)


# ============================================================================
# SCHEMA DDL
# ============================================================================

_SILVER_TABLES_DDL = [
    # ── technique_summary ──────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS silver.technique_summary (
        id              BIGSERIAL PRIMARY KEY,
        task_id         TEXT NOT NULL,
        email           TEXT,
        sport           TEXT,
        swing_type      TEXT,
        dominant_hand   TEXT,
        player_height_mm INT,
        analysis_status TEXT,
        feature_count   INT,
        category_count  INT,
        overall_score   DOUBLE PRECISION,
        level           TEXT,
        top_strength    TEXT,
        top_improvement TEXT,
        warnings        JSONB,
        errors          JSONB,
        analysed_at     TIMESTAMPTZ,
        created_at      TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_silver_technique_summary_task ON silver.technique_summary (task_id)",

    # ── technique_features_enriched ────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS silver.technique_features_enriched (
        id                  BIGSERIAL PRIMARY KEY,
        task_id             TEXT NOT NULL,
        feature_name        TEXT,
        feature_human_readable TEXT,
        level               TEXT,
        score               DOUBLE PRECISION,
        value               DOUBLE PRECISION,
        observation         TEXT,
        suggestion          TEXT,
        category_name       TEXT,
        category_score      DOUBLE PRECISION,
        score_vs_category   DOUBLE PRECISION,
        event_name          TEXT,
        event_timestamp     DOUBLE PRECISION,
        event_frame_nr      INT,
        score_ranges        JSONB,
        value_ranges        JSONB,
        highlight_joints    JSONB,
        highlight_limbs     JSONB,
        created_at          TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_silver_technique_feat_task ON silver.technique_features_enriched (task_id)",

    # ── technique_kinetic_chain_analysis ───────────────────────
    """
    CREATE TABLE IF NOT EXISTS silver.technique_kinetic_chain_analysis (
        id              BIGSERIAL PRIMARY KEY,
        task_id         TEXT NOT NULL,
        segment_name    TEXT,
        peak_speed      DOUBLE PRECISION,
        peak_timestamp  DOUBLE PRECISION,
        peak_order      INT,
        speed_delta_prev DOUBLE PRECISION,
        time_delta_prev DOUBLE PRECISION,
        is_sequential   BOOLEAN,
        plot_values     JSONB,
        created_at      TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_silver_technique_kc_task ON silver.technique_kinetic_chain_analysis (task_id)",

    # ── technique_pose_timeline ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS silver.technique_pose_timeline (
        id              BIGSERIAL PRIMARY KEY,
        task_id         TEXT NOT NULL,
        frame_nr        INT,
        has_2d          BOOLEAN DEFAULT FALSE,
        has_3d          BOOLEAN DEFAULT FALSE,
        confidence_2d   DOUBLE PRECISION,
        confidence_3d   DOUBLE PRECISION,
        bbox_2d         JSONB,
        bbox_3d         JSONB,
        keypoints_2d    JSONB,
        keypoints_3d    JSONB,
        created_at      TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_silver_technique_pose_task ON silver.technique_pose_timeline (task_id, frame_nr)",

    # ── technique_trends ───────────────────────────────────────
    # Cross-session: one row per (email, swing_type, feature_name, task_id).
    # Populated from silver.technique_summary + silver.technique_features_enriched.
    """
    CREATE TABLE IF NOT EXISTS silver.technique_trends (
        id              BIGSERIAL PRIMARY KEY,
        email           TEXT NOT NULL,
        swing_type      TEXT,
        feature_name    TEXT,
        task_id         TEXT NOT NULL,
        score           DOUBLE PRECISION,
        value           DOUBLE PRECISION,
        level           TEXT,
        analysed_at     TIMESTAMPTZ,
        created_at      TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_silver_technique_trends_email ON silver.technique_trends (email, swing_type, feature_name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_silver_technique_trends ON silver.technique_trends (email, swing_type, feature_name, task_id)",
]


def ensure_silver_schema(engine) -> None:
    """Create silver technique tables idempotently."""
    with engine.begin() as conn:
        conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS silver"))
        for ddl in _SILVER_TABLES_DDL:
            conn.execute(sql_text(ddl))
    log.info("[technique_silver] schema ensured")


# ============================================================================
# BUILD PIPELINE
# ============================================================================

def build_silver_technique(task_id: str, engine, replace: bool = True) -> Dict[str, Any]:
    """
    Build all silver technique tables for a given task_id.

    Steps:
      1. Read bronze technique tables
      2. Build technique_summary
      3. Build technique_features_enriched
      4. Build technique_kinetic_chain_analysis
      5. Build technique_pose_timeline
      6. Update technique_trends (cross-session)

    Returns dict with row counts.
    """
    ensure_silver_schema(engine)
    counts = {}

    with engine.begin() as conn:
        if replace:
            for tbl in (
                "silver.technique_summary",
                "silver.technique_features_enriched",
                "silver.technique_kinetic_chain_analysis",
                "silver.technique_pose_timeline",
            ):
                conn.execute(sql_text(f"DELETE FROM {tbl} WHERE task_id = :t"), {"t": task_id})

        # ── Load bronze data ───────────────────────────────────
        meta_row = conn.execute(sql_text(
            "SELECT * FROM bronze.technique_analysis_metadata WHERE task_id = :t"
        ), {"t": task_id}).mappings().first()

        features = conn.execute(sql_text(
            "SELECT * FROM bronze.technique_features WHERE task_id = :t ORDER BY id"
        ), {"t": task_id}).mappings().all()

        categories = conn.execute(sql_text(
            "SELECT * FROM bronze.technique_feature_categories WHERE task_id = :t"
        ), {"t": task_id}).mappings().all()

        kinetic_rows = conn.execute(sql_text(
            "SELECT * FROM bronze.technique_kinetic_chain WHERE task_id = :t ORDER BY id"
        ), {"t": task_id}).mappings().all()

        # Get email + swing_type from submission_context
        sc_row = conn.execute(sql_text(
            "SELECT email, sport_type, raw_meta FROM bronze.submission_context WHERE task_id = :t"
        ), {"t": task_id}).mappings().first()

        email = (sc_row["email"] if sc_row else None) or ""
        raw_meta = {}
        if sc_row and sc_row.get("raw_meta"):
            try:
                raw_meta = json.loads(sc_row["raw_meta"]) if isinstance(sc_row["raw_meta"], str) else (sc_row["raw_meta"] or {})
            except (json.JSONDecodeError, TypeError):
                pass

        sport = raw_meta.get("sport") or (meta_row["sport"] if meta_row else None) or "tennis"
        swing_type = raw_meta.get("swing_type") or (meta_row["swing_type"] if meta_row else None) or ""
        dominant_hand = raw_meta.get("dominant_hand") or (meta_row["dominant_hand"] if meta_row else None) or ""
        player_height_mm = raw_meta.get("player_height_mm") or (meta_row["player_height_mm"] if meta_row else None)

        # Build category lookup
        cat_map = {}
        for c in categories:
            cat_map[c["category_name"]] = {
                "score": c["category_score"],
                "features": c.get("feature_names"),
            }

        # ── 1. technique_summary ───────────────────────────────
        scores = [f["score"] for f in features if f.get("score") is not None]
        overall_score = round(sum(scores) / len(scores), 2) if scores else None

        # Determine overall level from most common feature level
        levels = [f["level"] for f in features if f.get("level")]
        overall_level = max(set(levels), key=levels.count) if levels else None

        # Top strength: highest-scoring feature
        top_strength = None
        top_improvement = None
        if features:
            sorted_feats = sorted(
                [f for f in features if f.get("score") is not None],
                key=lambda x: x["score"],
            )
            if sorted_feats:
                top_strength = sorted_feats[-1]["feature_human_readable"] or sorted_feats[-1]["feature_name"]
                top_improvement = sorted_feats[0]["feature_human_readable"] or sorted_feats[0]["feature_name"]

        conn.execute(sql_text("""
            INSERT INTO silver.technique_summary
                (task_id, email, sport, swing_type, dominant_hand, player_height_mm,
                 analysis_status, feature_count, category_count, overall_score, level,
                 top_strength, top_improvement, warnings, errors, analysed_at)
            VALUES
                (:task_id, :email, :sport, :swing_type, :dominant_hand, :player_height_mm,
                 :analysis_status, :feature_count, :category_count, :overall_score, :level,
                 :top_strength, :top_improvement, :warnings, :errors, now())
            ON CONFLICT (task_id) DO UPDATE SET
                email = EXCLUDED.email,
                sport = EXCLUDED.sport,
                swing_type = EXCLUDED.swing_type,
                overall_score = EXCLUDED.overall_score,
                level = EXCLUDED.level,
                top_strength = EXCLUDED.top_strength,
                top_improvement = EXCLUDED.top_improvement,
                analysed_at = EXCLUDED.analysed_at
        """), {
            "task_id": task_id,
            "email": email,
            "sport": sport,
            "swing_type": swing_type,
            "dominant_hand": dominant_hand,
            "player_height_mm": player_height_mm,
            "analysis_status": meta_row["status"] if meta_row else "unknown",
            "feature_count": len(features),
            "category_count": len(categories),
            "overall_score": overall_score,
            "level": overall_level,
            "top_strength": top_strength,
            "top_improvement": top_improvement,
            "warnings": json.dumps(meta_row["warnings"]) if meta_row and meta_row.get("warnings") else None,
            "errors": json.dumps(meta_row["errors"]) if meta_row and meta_row.get("errors") else None,
        })
        counts["technique_summary"] = 1

        # ── 2. technique_features_enriched ─────────────────────
        feat_count = 0
        for f in features:
            # Find which category this feature belongs to
            feat_cats = f.get("feature_categories")
            if isinstance(feat_cats, str):
                try:
                    feat_cats = json.loads(feat_cats)
                except (json.JSONDecodeError, TypeError):
                    feat_cats = []
            elif not isinstance(feat_cats, list):
                feat_cats = []

            cat_name = feat_cats[0] if feat_cats else None
            cat_score = cat_map.get(cat_name, {}).get("score") if cat_name else None

            score_vs_cat = None
            if f.get("score") is not None and cat_score is not None:
                score_vs_cat = round(f["score"] - cat_score, 2)

            conn.execute(sql_text("""
                INSERT INTO silver.technique_features_enriched
                    (task_id, feature_name, feature_human_readable, level, score,
                     value, observation, suggestion, category_name, category_score,
                     score_vs_category, event_name, event_timestamp, event_frame_nr,
                     score_ranges, value_ranges, highlight_joints, highlight_limbs)
                VALUES
                    (:task_id, :feature_name, :feature_human_readable, :level, :score,
                     :value, :observation, :suggestion, :category_name, :category_score,
                     :score_vs_category, :event_name, :event_timestamp, :event_frame_nr,
                     :score_ranges, :value_ranges, :highlight_joints, :highlight_limbs)
            """), {
                "task_id": task_id,
                "feature_name": f["feature_name"],
                "feature_human_readable": f["feature_human_readable"],
                "level": f["level"],
                "score": f["score"],
                "value": f["value"],
                "observation": f["observation"],
                "suggestion": f["suggestion"],
                "category_name": cat_name,
                "category_score": cat_score,
                "score_vs_category": score_vs_cat,
                "event_name": f["event_name"],
                "event_timestamp": f["event_timestamp"],
                "event_frame_nr": f["event_frame_nr"],
                "score_ranges": json.dumps(f["score_ranges"]) if f.get("score_ranges") else None,
                "value_ranges": json.dumps(f["value_ranges"]) if f.get("value_ranges") else None,
                "highlight_joints": json.dumps(f["highlight_joints"]) if f.get("highlight_joints") else None,
                "highlight_limbs": json.dumps(f["highlight_limbs"]) if f.get("highlight_limbs") else None,
            })
            feat_count += 1
        counts["technique_features_enriched"] = feat_count

        # ── 3. technique_kinetic_chain_analysis ────────────────
        # Sort segments by peak_timestamp to determine sequencing
        sorted_kc = sorted(
            [r for r in kinetic_rows if r.get("peak_timestamp") is not None],
            key=lambda x: x["peak_timestamp"],
        )

        kc_count = 0
        prev_speed = None
        prev_ts = None
        for order, seg in enumerate(sorted_kc, 1):
            speed_delta = round(seg["peak_speed"] - prev_speed, 2) if prev_speed is not None and seg.get("peak_speed") is not None else None
            time_delta = round(seg["peak_timestamp"] - prev_ts, 4) if prev_ts is not None else None
            is_seq = time_delta is not None and time_delta > 0

            conn.execute(sql_text("""
                INSERT INTO silver.technique_kinetic_chain_analysis
                    (task_id, segment_name, peak_speed, peak_timestamp, peak_order,
                     speed_delta_prev, time_delta_prev, is_sequential, plot_values)
                VALUES
                    (:task_id, :segment_name, :peak_speed, :peak_timestamp, :peak_order,
                     :speed_delta_prev, :time_delta_prev, :is_sequential, :plot_values)
            """), {
                "task_id": task_id,
                "segment_name": seg["segment_name"],
                "peak_speed": seg["peak_speed"],
                "peak_timestamp": seg["peak_timestamp"],
                "peak_order": order,
                "speed_delta_prev": speed_delta,
                "time_delta_prev": time_delta,
                "is_sequential": is_seq,
                "plot_values": json.dumps(seg["plot_values"]) if seg.get("plot_values") else None,
            })
            prev_speed = seg.get("peak_speed")
            prev_ts = seg.get("peak_timestamp")
            kc_count += 1

        # Add segments without timestamps at the end
        for seg in kinetic_rows:
            if seg.get("peak_timestamp") is not None:
                continue
            kc_count += 1
            conn.execute(sql_text("""
                INSERT INTO silver.technique_kinetic_chain_analysis
                    (task_id, segment_name, peak_speed, peak_timestamp, peak_order,
                     speed_delta_prev, time_delta_prev, is_sequential, plot_values)
                VALUES (:task_id, :seg, :ps, NULL, :order, NULL, NULL, NULL, :pv)
            """), {
                "task_id": task_id,
                "seg": seg["segment_name"],
                "ps": seg["peak_speed"],
                "order": kc_count,
                "pv": json.dumps(seg["plot_values"]) if seg.get("plot_values") else None,
            })
        counts["technique_kinetic_chain_analysis"] = kc_count

        # ── 4. technique_pose_timeline ─────────────────────────
        # Build per-frame consolidated timeline from 2D + 3D pose data
        pose_count = _build_pose_timeline(conn, task_id)
        counts["technique_pose_timeline"] = pose_count

        # ── 5. technique_trends (cross-session) ───────────────
        if email and swing_type:
            # Delete old trend rows for this specific task, then re-insert
            conn.execute(sql_text(
                "DELETE FROM silver.technique_trends WHERE task_id = :t"
            ), {"t": task_id})

            trend_count = 0
            for f in features:
                if f.get("score") is None:
                    continue
                conn.execute(sql_text("""
                    INSERT INTO silver.technique_trends
                        (email, swing_type, feature_name, task_id, score, value, level, analysed_at)
                    VALUES (:email, :swing_type, :feature_name, :task_id, :score, :value, :level, now())
                    ON CONFLICT (email, swing_type, feature_name, task_id) DO UPDATE SET
                        score = EXCLUDED.score,
                        value = EXCLUDED.value,
                        level = EXCLUDED.level,
                        analysed_at = EXCLUDED.analysed_at
                """), {
                    "email": email,
                    "swing_type": swing_type,
                    "feature_name": f["feature_name"],
                    "task_id": task_id,
                    "score": f["score"],
                    "value": f["value"],
                    "level": f["level"],
                })
                trend_count += 1
            counts["technique_trends"] = trend_count
        else:
            counts["technique_trends"] = 0

    log.info("TECHNIQUE SILVER BUILD task_id=%s counts=%s", task_id, counts)
    return {"task_id": task_id, "counts": counts}


def _build_pose_timeline(conn, task_id: str) -> int:
    """Build per-frame pose timeline from bronze 2D + 3D pose data."""
    pose_2d_row = conn.execute(sql_text(
        "SELECT raw_data FROM bronze.technique_pose_2d WHERE task_id = :t"
    ), {"t": task_id}).mappings().first()

    pose_3d_row = conn.execute(sql_text(
        "SELECT raw_data FROM bronze.technique_pose_3d WHERE task_id = :t"
    ), {"t": task_id}).mappings().first()

    if not pose_2d_row and not pose_3d_row:
        return 0

    # Parse pose data — could be dict keyed by frame number or list
    frames_2d = _parse_pose_frames(pose_2d_row["raw_data"] if pose_2d_row else None)
    frames_3d = _parse_pose_frames(pose_3d_row["raw_data"] if pose_3d_row else None)

    # Merge frame numbers from both sources
    all_frames = sorted(set(list(frames_2d.keys()) + list(frames_3d.keys())))

    count = 0
    for frame_nr in all_frames:
        f2d = frames_2d.get(frame_nr)
        f3d = frames_3d.get(frame_nr)

        conn.execute(sql_text("""
            INSERT INTO silver.technique_pose_timeline
                (task_id, frame_nr, has_2d, has_3d,
                 confidence_2d, confidence_3d,
                 bbox_2d, bbox_3d, keypoints_2d, keypoints_3d)
            VALUES
                (:task_id, :frame_nr, :has_2d, :has_3d,
                 :confidence_2d, :confidence_3d,
                 :bbox_2d, :bbox_3d, :keypoints_2d, :keypoints_3d)
        """), {
            "task_id": task_id,
            "frame_nr": frame_nr,
            "has_2d": f2d is not None,
            "has_3d": f3d is not None,
            "confidence_2d": _extract_confidence(f2d),
            "confidence_3d": _extract_confidence(f3d),
            "bbox_2d": json.dumps(f2d.get("bbox")) if f2d and f2d.get("bbox") else None,
            "bbox_3d": json.dumps(f3d.get("bbox")) if f3d and f3d.get("bbox") else None,
            "keypoints_2d": json.dumps(f2d.get("keypoints")) if f2d and f2d.get("keypoints") else None,
            "keypoints_3d": json.dumps(f3d.get("keypoints")) if f3d and f3d.get("keypoints") else None,
        })
        count += 1

    return count


def _parse_pose_frames(raw_data) -> dict:
    """Parse pose data into {frame_nr: frame_data} dict."""
    if raw_data is None:
        return {}

    data = raw_data
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return {}

    if isinstance(data, dict):
        # Keyed by frame number (string keys)
        result = {}
        for k, v in data.items():
            try:
                frame_nr = int(k)
                result[frame_nr] = v if isinstance(v, dict) else {"keypoints": v}
            except (ValueError, TypeError):
                continue
        return result
    elif isinstance(data, list):
        # Indexed by frame position
        return {
            i: (item if isinstance(item, dict) else {"keypoints": item})
            for i, item in enumerate(data)
            if item is not None
        }
    return {}


def _extract_confidence(frame_data) -> float | None:
    """Extract average confidence from a pose frame."""
    if not frame_data or not isinstance(frame_data, dict):
        return None

    conf = frame_data.get("confidence")
    if isinstance(conf, (int, float)):
        return float(conf)

    # Try averaging keypoint confidences
    kps = frame_data.get("keypoints")
    if isinstance(kps, list):
        confs = []
        for kp in kps:
            if isinstance(kp, dict) and "confidence" in kp:
                try:
                    confs.append(float(kp["confidence"]))
                except (TypeError, ValueError):
                    pass
            elif isinstance(kp, (list, tuple)) and len(kp) >= 3:
                try:
                    confs.append(float(kp[-1]))  # last element often confidence
                except (TypeError, ValueError):
                    pass
        if confs:
            return round(sum(confs) / len(confs), 4)

    return None
