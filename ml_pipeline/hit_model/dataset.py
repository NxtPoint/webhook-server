"""Corpus -> (X, y) dataset for the hit model.

Labels come STRAIGHT from bronze: SA player_swing.ball_hit_s for the
corpus pair's sa_task_id (2,592 labels / 8 pairs — no S3 label JSONs).
A candidate is positive when it sits within POS_TOL_S of any SA hit.

Split discipline: BY VIDEO. The reference video appears as THREE tasks:
a35b37f6 + 17e2da3a (warp-era corpus rows) and the CLEAN rev-77 probe
rerun 86ade942 (CLEAN_EVAL below) — all held out. The gate number is the
clean one.

One FRESH connection per task (NAT idle-drop,
feedback_nat_idle_drop_long_db_connections).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import numpy as np
from sqlalchemy import text

from ml_pipeline.ball_merge import merged_ball_subquery
from ml_pipeline.hit_model.candidates import hit_candidates, attribute_player
from ml_pipeline.hit_model.features import featurize, N_FEATURES

logger = logging.getLogger(__name__)

POS_TOL_S = 0.5

# Reference-video tasks (held out, all three).
HELDOUT_TASKS = {"a35b37f6", "17e2da3a", "86ade942"}

# Clean rev-77 rerun of the reference video -> its SA companion. Not a
# corpus row (probe job), evaluated against bronze labels directly.
CLEAN_EVAL = {
    "86ade942-7472-4c55-b17c-f18cb0a18a74":
        "ba4812be-75af-4f8b-a15b-63941849f882",
}

SA_NEAR_PID, SA_FAR_PID = 22, 122


def _task_fps(conn, tid: str) -> float:
    r = conn.execute(text(
        "SELECT total_frames, video_duration_sec FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id::text = :t"), {"t": tid}).fetchone()
    return (r[0] / r[1]) if r and r[0] and r[1] else 25.0


def load_task_arrays(conn, tid: str) -> dict:
    fps = _task_fps(conn, tid)
    # Source-preference deduped (roi_far_ball > roi_prod > main > NULL) so the
    # sharp far-ROI ball wins per far frame and the overlapping main row doesn't
    # double the trajectory. No-op until roi_* rows exist. See ml_pipeline.ball_merge.
    ball_rows = [dict(r) for r in conn.execute(text(merged_ball_subquery(
        "frame_idx, x, y, is_bounce, court_x, court_y",
        job_pred="job_id::text = :t")), {"t": tid}).mappings().all()]
    cnn_ts = sorted(float(r[0]) for r in conn.execute(text(
        "SELECT ts FROM ml_analysis.ball_bounces WHERE job_id::text = :t"),
        {"t": tid}).fetchall())
    legacy_ts = sorted(float(r["frame_idx"]) / fps for r in ball_rows
                       if r.get("is_bounce"))
    ball_ts = sorted(float(r["frame_idx"]) / fps for r in ball_rows
                     if r.get("x") is not None)

    # player image centres at 0.2s resolution, per half
    lookups = {0: {}, 1: {}}
    for fi_, pid, cx_, cy_ in conn.execute(text(
            "SELECT frame_idx, player_id, center_x, center_y "
            "FROM ml_analysis.player_detections "
            "WHERE job_id::text = :t AND center_x IS NOT NULL"), {"t": tid}).fetchall():
        if pid in (0, 1):
            lookups[pid][round((fi_ / fps) * 5) / 5] = (float(cx_), float(cy_))

    return dict(fps=fps, ball_rows=ball_rows, cnn_ts=cnn_ts,
                legacy_ts=legacy_ts, ball_ts=ball_ts,
                near_lookup=lookups[0], far_lookup=lookups[1])


def _sa_labels(conn, sa_tid: str) -> List[tuple]:
    """[(ball_hit_s, side 0=near/1=far)] for the two MAIN players.

    ⚠️ SIDE IS POSITIONAL, PER SWING — never a person mapping. SA
    player_id is a PERSON; players change ENDS at changeovers, so any
    person->side mapping inverts mid-match (the first builds scored
    40-57% WHO agreement on long matches because of exactly this; the
    short reference clip masked it at 70-79%). T5 pids are positional
    (0=near, 1=far), so the label side comes from each swing's OWN
    ball_hit_location_y (> HALF_Y 11.885 = near) — the same per-event
    role rule the serve labels used. Main players = top-2 by swing count
    (excludes ball kids / stray ids)."""
    pids = [r[0] for r in conn.execute(text(
        "SELECT player_id FROM bronze.player_swing "
        "WHERE task_id::text = :s AND ball_hit_s IS NOT NULL "
        "GROUP BY player_id ORDER BY COUNT(*) DESC LIMIT 2"),
        {"s": sa_tid}).fetchall()]
    if len(pids) < 2:
        return []
    return [(float(r[0]), 0 if float(r[1]) > 11.885 else 1)
            for r in conn.execute(text(
                "SELECT ball_hit_s, ball_hit_location_y FROM bronze.player_swing "
                "WHERE task_id::text = :s AND player_id IN (:a, :b) "
                "AND ball_hit_s IS NOT NULL AND ball_hit_location_y IS NOT NULL "
                "ORDER BY 1"),
                {"s": sa_tid, "a": int(pids[0]), "b": int(pids[1])}).fetchall()]


def build_dataset(engine) -> Dict[str, dict]:
    with engine.connect() as conn:
        corpus = conn.execute(text(
            "SELECT t5_task_id::text, sa_task_id::text FROM ml_analysis.training_corpus "
            "WHERE label_kind = 'serve' ORDER BY created_at")).fetchall()

    out: Dict[str, dict] = {}
    for tid, sa_tid in (list(corpus) + list(CLEAN_EVAL.items())):
        short = tid[:8]
        with engine.connect() as conn:
            arrays = load_task_arrays(conn, tid)
            labels = _sa_labels(conn, sa_tid)

        cands = hit_candidates(arrays["ball_rows"], arrays["fps"])
        cand_ts = [c.ts for c in cands]
        X = np.stack([
            featurize(c, cand_ts, arrays["ball_ts"], arrays["cnn_ts"],
                      arrays["legacy_ts"], arrays["near_lookup"],
                      arrays["far_lookup"])
            for c in cands
        ]) if cands else np.zeros((0, N_FEATURES), dtype=np.float32)

        # Labeling: each SA hit makes POSITIVE only its NEAREST candidate
        # within POS_TOL_S; OTHER candidates inside the tolerance are
        # IGNORED (weight 0) — they're usually the bounce-discontinuity of
        # the same shot, and calling them positive taught the model
        # "hit-adjacent" instead of "hit" (first build: 5,305 'positives'
        # from ~1,600 labels, WHO-rule agreement at coin-flip because half
        # the 'positives' were bounces). Everything else is negative.
        y = np.zeros(len(cands), dtype=np.float32)
        w = np.ones(len(cands), dtype=np.float32)
        cand_arr = np.array(cand_ts)
        for ts_l, _pid in labels:
            in_tol = np.flatnonzero(np.abs(cand_arr - ts_l) <= POS_TOL_S)
            if len(in_tol) == 0:
                continue
            w[in_tol] = 0.0           # ambiguous zone: out of the loss
            nearest = in_tol[np.argmin(np.abs(cand_arr[in_tol] - ts_l))]
            y[nearest] = 1.0
            w[nearest] = 1.0

        # WHO-rule A/B: among positive candidates, agreement of (A) the
        # incoming-direction rule and (B) the nearest-player rule with the
        # SA pid of the matched label. The winner becomes the attribution
        # rule the wire-in uses.
        agree_dir = agree_prox = tot_pos = 0
        for i, c in enumerate(cands):
            if y[i] != 1.0:
                continue
            best = min(labels, key=lambda lp: abs(lp[0] - c.ts))
            if abs(best[0] - c.ts) > POS_TOL_S:
                continue
            tot_pos += 1
            agree_dir += int(attribute_player(c) == best[1])
            # rule B: nearer image-space player half at the candidate
            def _gap(lookup):
                g = 1e9
                for dt in (-0.2, 0.0, 0.2):
                    xy = lookup.get(round((c.ts + dt) * 5) / 5)
                    if xy is not None:
                        g = min(g, ((xy[0] - c.x) ** 2 + (xy[1] - c.y) ** 2) ** 0.5)
                return g
            prox_pid = 0 if _gap(arrays["near_lookup"]) <= _gap(arrays["far_lookup"]) else 1
            agree_prox += int(prox_pid == best[1])

        heldout = short in HELDOUT_TASKS
        out[short] = dict(task_id=tid, sa_task_id=sa_tid, X=X, y=y, w=w,
                          cands=cands, anchor_ts=cand_ts, labels=labels,
                          heldout=heldout,
                          clean=tid in CLEAN_EVAL,
                          near_lookup=arrays["near_lookup"],
                          far_lookup=arrays["far_lookup"])
        logger.info(
            "hit dataset %s: cands=%d pos=%d labels=%d who: dir %d/%d (%.0f%%) "
            "prox %d/%d (%.0f%%) heldout=%s%s",
            short, len(cands), int(y.sum()), len(labels),
            agree_dir, tot_pos, (100.0 * agree_dir / tot_pos) if tot_pos else 0.0,
            agree_prox, tot_pos, (100.0 * agree_prox / tot_pos) if tot_pos else 0.0,
            heldout, " CLEAN" if tid in CLEAN_EVAL else "")
    return out


def split(dataset: Dict[str, dict]):
    """(X_train, y_train, w_train, heldout_tasks)."""
    Xs, ys, ws, heldout = [], [], [], {}
    for short, d in dataset.items():
        if d["heldout"]:
            heldout[short] = d
        elif len(d["X"]):
            Xs.append(d["X"]); ys.append(d["y"]); ws.append(d["w"])
    return np.concatenate(Xs), np.concatenate(ys), np.concatenate(ws), heldout
