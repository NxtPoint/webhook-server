"""Hit-location assembly — the MODEL layer owns the complete hit fact.

A stroke IS a ball-hit: the bronze `stroke_events` row should carry not just the
timing but WHERE the ball was struck (`ball_hit_location_x/y`) and the hitter's
court SIDE (`hitter_side_near`). Silver then projects these VERBATIM (rule #1/#2)
instead of reconstructing them.

This is a faithful port of the assembly that previously lived inside
`build_silver_match_t5._t5_pass1_load_stroke_driven` + `_build_player_buckets`
(silver doing the model's job). Moved here 2026-06-15 so silver becomes a pure
projection — the audit's "hit-WHERE keystone". The logic mirrors the silver
helpers EXACTLY (ROI merge, pid_map canonicalisation, bounce-opposite side,
nearest-detection + mirror fallback) so coverage/values match and Pass-3 — which
anchors point/game structure on serve rows, not these rally rows — is preserved.

Render-side: runs inside `detect_strokes_for_task`. Reads `ml_analysis.ball_bounces`
(bounce-opposite side signal) + `player_detections` (+ `_roi` far merge), all
present after the Render re-ingest. Dev-ceiling: best-effort, NULL on the far-court
tail (~50% NULL court_y is calibration/train-last, not buildable here).
"""
from __future__ import annotations

import bisect
import logging
import os
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text as sql_text

from ml_pipeline.ball_merge import MAIN_ONLY_WHERE
from ml_pipeline.config import COURT_LENGTH_M

logger = logging.getLogger(__name__)

HALF_Y = COURT_LENGTH_M / 2.0  # 11.885m — net line; near = court_y > HALF_Y


def _bounce_from_model_enabled() -> bool:
    """Mirror build_silver_match_t5._bounce_from_model_enabled (T5_BOUNCE_FROM_MODEL)."""
    return os.getenv("T5_BOUNCE_FROM_MODEL", "1").strip().lower() in ("1", "true", "yes", "on")


def _load_bounce_index(conn, job_id: str):
    """[(frame_idx, court_x, court_y), …] ordered by frame. Source = the bounce
    MODEL (ml_analysis.ball_bounces) when enabled + non-empty, else legacy
    is_bounce — identical source/fallback to the silver builder."""
    if _bounce_from_model_enabled():
        rows = conn.execute(sql_text("""
            SELECT frame_idx, court_x, court_y
            FROM ml_analysis.ball_bounces
            WHERE job_id::text = :jid
              AND court_x IS NOT NULL AND court_y IS NOT NULL
            ORDER BY frame_idx
        """), {"jid": job_id}).fetchall()
        if rows:
            return rows
        logger.info("stroke_detector hit-loc: ball_bounces empty for job=%s — "
                    "falling back to legacy is_bounce", job_id)
    return conn.execute(sql_text(f"""
        SELECT frame_idx, court_x, court_y
        FROM ml_analysis.ball_detections
        WHERE job_id = :jid AND is_bounce = TRUE AND {MAIN_ONLY_WHERE}
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()


def _build_index(dets: List[dict]) -> Tuple[List[int], List[dict]]:
    dets.sort(key=lambda d: d["frame_idx"])
    return [d["frame_idx"] for d in dets], dets


def _find_nearest(frames: List[int], dets: List[dict], target: int,
                  max_distance_frames: Optional[int] = None) -> Optional[dict]:
    """Nearest detection in frame space (binary search). None if beyond
    max_distance_frames — prevents a single stale far detection being reused
    across many hits (the sparse-far-coverage bug)."""
    if not dets:
        return None
    idx = bisect.bisect_left(frames, target)
    best, best_dist = None, float("inf")
    for ci in (idx - 1, idx):
        if 0 <= ci < len(dets):
            d = abs(dets[ci]["frame_idx"] - target)
            if d < best_dist:
                best_dist, best = d, dets[ci]
    if max_distance_frames is not None and best_dist > max_distance_frames:
        return None
    return best


def _load_player_court(conn, job_id: str) -> dict:
    """near/far/any court-position indices + per-canonical-player tracks +
    pid_map. Faithfully mirrors build_silver_match_t5._build_player_buckets:
      (1) merge player_detections_roi (far pid=1 wins wholesale, add ROI-only),
      (2) pid_map: top-2 player_ids by count; everything != top[0] -> top[1],
      (3) dets_by_canonical keyed by the mapped canonical id.
    Court coords only (the detector needs position, not pose/stroke_class)."""
    main = conn.execute(sql_text("""
        SELECT frame_idx, player_id, court_x, court_y
        FROM ml_analysis.player_detections
        WHERE job_id::text = :jid
        ORDER BY frame_idx
    """), {"jid": job_id}).fetchall()
    rows = [(int(f), int(pid), cx, cy) for f, pid, cx, cy in main]

    # Merge far ViTPose ROI court positions (pid=1 wins wholesale; add ROI-only),
    # exactly like _build_player_buckets — the far player is denser/cleaner there.
    roi_present = conn.execute(sql_text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='ml_analysis' AND table_name='player_detections_roi' LIMIT 1
    """)).scalar()
    if roi_present:
        roi = conn.execute(sql_text("""
            SELECT frame_idx, player_id, court_x, court_y
            FROM ml_analysis.player_detections_roi
            WHERE job_id::text = :jid
            ORDER BY frame_idx
        """), {"jid": job_id}).fetchall()
        if roi:
            merged = {(pid, f): (f, pid, cx, cy) for f, pid, cx, cy in rows}
            for f, pid, cx, cy in roi:
                f, pid = int(f), int(pid)
                key = (pid, f)
                if pid == 1 or key not in merged:   # ROI wins wholesale for far
                    merged[key] = (f, pid, cx, cy)
            rows = sorted(merged.values(), key=lambda r: r[0])

    # pid_map: top-2 by detection count; every other pid folds into top[1].
    counts = Counter(r[1] for r in rows)
    top = [pid for pid, _ in counts.most_common(2)]
    if not top:
        top = [0, 1]
    elif len(top) == 1:
        top.append(top[0] + 1)
    pid_map = {pid: (str(top[0]) if pid == top[0] else str(top[1])) for pid in counts}

    near: List[dict] = []
    far: List[dict] = []
    any_c: List[dict] = []
    by_canon: Dict[str, List[dict]] = {}
    for f, pid, cx, cy in rows:
        if cx is None or cy is None:
            continue
        e = {"frame_idx": f, "court_x": cx, "court_y": cy}
        any_c.append(e)
        by_canon.setdefault(pid_map[pid], []).append(e)
        (near if cy > HALF_Y else far).append(e)
    return {
        "near": _build_index(near),
        "far": _build_index(far),
        "any": _build_index(any_c),
        "by_canon": {k: _build_index(v) for k, v in by_canon.items()},
        "pid_map": pid_map,
    }


def assemble_hit_locations(
    conn, job_id: str, fps: float, strokes: Sequence[Tuple[int, int]],
) -> List[dict]:
    """For each (predicted_hit_frame, attributed_player_id), resolve the hitter's
    SIDE and court position at the hit. Returns a list aligned 1:1 to `strokes`
    of {ball_hit_location_x, ball_hit_location_y, hitter_side_near}.

    SIDE: bounce-opposite when a bounce falls in (hf, hf+~1s] (reliable — ball
    struck on one half lands on the other); else the attributed player's own
    court position (canonicalised via pid_map, biased last resort). POSITION:
    nearest detection on the resolved side within the hit window; mirror fallback
    when that side has no detection. Faithful to the prior silver assembly.
    """
    HIT_WINDOW = max(1, int(round(fps * 0.20)))
    HIT_SOFT_WINDOW = max(1, int(round(fps * 1.20)))
    BOUNCE_AFTER = max(1, int(round(fps * 1.0)))

    bounce_rows = _load_bounce_index(conn, job_id)
    bounce_frames = [b[0] for b in bounce_rows]
    pc = _load_player_court(conn, job_id)
    near_f, near_d = pc["near"]
    far_f, far_d = pc["far"]
    any_f, any_d = pc["any"]
    by_canon = pc["by_canon"]
    pid_map = pc["pid_map"]

    n_bounce_side = n_attr_side = n_unresolved = n_mirror = 0
    out: List[dict] = []
    for hf, raw_pid in strokes:
        # ---- resolve SIDE (near = court_y > HALF_Y) ----
        bi = bisect.bisect_left(bounce_frames, hf)
        b_cy = None
        if bi < len(bounce_frames) and bounce_frames[bi] <= hf + BOUNCE_AFTER:
            b_cy = bounce_rows[bi][2]
        side_near: Optional[bool] = None
        if b_cy is not None:
            side_near = (b_cy < HALF_Y)  # ball bounced on far half -> hitter near
            n_bounce_side += 1
        else:
            mapped = pid_map.get(int(raw_pid), str(int(raw_pid)))
            idx = by_canon.get(mapped)
            if idx is not None:
                pf, pd = idx
                attr = (_find_nearest(pf, pd, hf, HIT_WINDOW)
                        or _find_nearest(pf, pd, hf, HIT_SOFT_WINDOW))
                if attr is not None and attr.get("court_y") is not None:
                    side_near = attr["court_y"] > HALF_Y
                    n_attr_side += 1
        if side_near is None:
            n_unresolved += 1
            out.append({"ball_hit_location_x": None, "ball_hit_location_y": None,
                        "hitter_side_near": None})
            continue

        # ---- hitter court position on the resolved side (+ mirror fallback) ----
        h_f, h_d = (near_f, near_d) if side_near else (far_f, far_d)
        hitter = (_find_nearest(h_f, h_d, hf, HIT_WINDOW)
                  or _find_nearest(h_f, h_d, hf, HIT_SOFT_WINDOW))
        if hitter is None and any_d:
            other = _find_nearest(any_f, any_d, hf, HIT_SOFT_WINDOW)
            if other is not None and other.get("court_y") is not None:
                other_near = other["court_y"] > HALF_Y
                my = ((COURT_LENGTH_M - other["court_y"])
                      if other_near != side_near else other["court_y"])
                my = max(0.0, min(COURT_LENGTH_M, my))
                hitter = {"court_x": other["court_x"], "court_y": my}
                n_mirror += 1
        if hitter is None:
            out.append({"ball_hit_location_x": None, "ball_hit_location_y": None,
                        "hitter_side_near": side_near})
            continue
        out.append({
            "ball_hit_location_x": hitter.get("court_x"),
            "ball_hit_location_y": hitter.get("court_y"),
            "hitter_side_near": side_near,
        })

    logger.info(
        "stroke_detector hit-loc: %d strokes — side(bounce=%d attr=%d unresolved=%d) "
        "mirror=%d, %d bounces / %d player-pos",
        len(strokes), n_bounce_side, n_attr_side, n_unresolved, n_mirror,
        len(bounce_rows), len(any_d),
    )
    return out
