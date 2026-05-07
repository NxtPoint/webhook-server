"""Serve + overhead visualizer.

For a given task_id, extracts the raw video frame at each serve
(serve_d=TRUE) and overhead-not-serve (stroke_d='Overhead' AND
serve_d=FALSE) timestamp. Overlays the ball bounce pixel position
(red dot) and hitter pixel position (green cross) from the ml_analysis
tables, plus a label showing the reconcile fields. Saves one PNG per
shot plus a combined contact-sheet PNG per category so the 11+9 shots
can be reviewed side-by-side.

Designed to diagnose the "why did this overhead fail the serve gate"
question — you can see visually whether the hitter was genuinely at
the baseline (calibration shifted him inside court) or mid-court
(correctly rejected by the gate).

Usage (from repo root, with DATABASE_URL env set):

    python -m ml_pipeline.diag.serve_viewer <task_id> \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --output ./diag_out

Reads from:
  silver.point_detail            (task_id, model='t5')
  ml_analysis.ball_detections    (pixel coords of bounces)
  ml_analysis.player_detections  (pixel coords of hitters)
  ml_analysis.video_analysis_jobs (fps, total_frames)
"""
from __future__ import annotations

import argparse
import bisect
import os
import sys
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from sqlalchemy import create_engine, text as sql_text


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def _get_fps(conn, task_id: str) -> float:
    row = conn.execute(sql_text("""
        SELECT COALESCE(video_fps,
                        total_frames::float / NULLIF(video_duration_sec, 0),
                        25.0) AS fps
        FROM ml_analysis.video_analysis_jobs WHERE job_id = :tid
    """), {"tid": task_id}).scalar()
    return float(row) if row else 25.0


def _fetch_shots(conn, task_id: str):
    """Return rows split into (serves, overheads_non_serve)."""
    rows = conn.execute(sql_text("""
        SELECT id, ball_hit_s, swing_type, stroke_d, serve, serve_d,
               ball_hit_location_x AS hx, ball_hit_location_y AS hy,
               court_x AS bx, court_y AS by_
        FROM silver.point_detail
        WHERE task_id = :tid AND model = 't5'
          AND (serve_d = TRUE
               OR (stroke_d = 'Overhead' AND serve_d = FALSE))
        ORDER BY ball_hit_s
    """), {"tid": task_id}).mappings().all()
    serves = [dict(r) for r in rows if r["serve_d"]]
    overheads = [dict(r) for r in rows if not r["serve_d"]]
    return serves, overheads


def _fetch_ball_pixel(conn, task_id: str, target_frame: int, window: int = 3):
    """Find the nearest ball_detection pixel within ±window frames."""
    rows = conn.execute(sql_text("""
        SELECT frame_idx, x, y, is_bounce
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid
          AND ABS(frame_idx - :tf) <= :w
        ORDER BY ABS(frame_idx - :tf)
        LIMIT 1
    """), {"tid": task_id, "tf": target_frame, "w": window}).mappings().first()
    return dict(rows) if rows else None


def _fetch_player_pixels(conn, task_id: str, target_frame: int, window: int = 30):
    """Return both near + far player pixels nearest the target frame.

    Window defaults to 30 frames (~1.2s) to match the soft-fallback
    window used by build_silver_match_t5.py when resolving hitters.
    The silver's hy value often comes from a detection up to 30 frames
    away from the bounce, so a tight window would silently miss it.
    """
    rows = conn.execute(sql_text("""
        SELECT frame_idx, player_id, center_x, center_y, court_x, court_y
        FROM ml_analysis.player_detections
        WHERE job_id = :tid AND ABS(frame_idx - :tf) <= :w
        ORDER BY ABS(frame_idx - :tf)
    """), {"tid": task_id, "tf": target_frame, "w": window}).mappings().all()
    # Pick first of each pid (nearest-in-time)
    seen = {}
    for r in rows:
        pid = r["player_id"]
        if pid not in seen:
            seen[pid] = dict(r)
    return seen


def _pick_hitter_pixel(players: dict, shot: dict,
                       court_y_tolerance_m: float = 3.0):
    """Find the detected player whose court_y best matches the silver
    row's hitter_y. Returns (pixel_xy, source_frame, delta_m) or None.

    Tolerance of 3m is wide enough to handle calibration extrapolation
    noise while rejecting the "only the near player is available so label
    him as hitter at y=24 even though hy=0.57" failure mode.
    """
    best = None
    best_delta = 1e9
    for pid, p in players.items():
        if p.get("court_y") is None:
            continue
        if shot.get("hy") is None:
            continue
        delta = abs(p["court_y"] - shot["hy"])
        if delta < best_delta:
            best_delta = delta
            best = p
    if best is None or best_delta > court_y_tolerance_m:
        return None
    return ((best["center_x"], best["center_y"]), best["frame_idx"], best_delta)


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def _extract_frame(cap: cv2.VideoCapture, frame_idx: int) -> Optional[np.ndarray]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    return frame if ok else None


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

def _draw_cross(img: np.ndarray, pt: tuple, colour: tuple, size: int = 16,
                thickness: int = 3) -> None:
    x, y = int(pt[0]), int(pt[1])
    cv2.line(img, (x - size, y), (x + size, y), colour, thickness)
    cv2.line(img, (x, y - size), (x, y + size), colour, thickness)


def _draw_dot(img: np.ndarray, pt: tuple, colour: tuple,
              radius: int = 12, thickness: int = 3) -> None:
    x, y = int(pt[0]), int(pt[1])
    cv2.circle(img, (x, y), radius, colour, thickness)
    cv2.circle(img, (x, y), 2, colour, -1)


def _draw_label_panel(img: np.ndarray, shot: dict, kind: str) -> None:
    """Top-left text block with the shot's metadata."""
    lines = [
        f"id={shot['id']}  ts={shot['ball_hit_s']:.2f}s  ({kind})",
        f"swing_type={shot['swing_type']}  stroke_d={shot['stroke_d']}",
        f"serve={shot['serve']}  serve_d={shot['serve_d']}",
        f"hitter_y={shot['hy']:.2f}m (gate: <1.5 or >22.27 to pass)",
        f"bounce court=({shot['bx']:.1f},{shot['by_']:.1f})",
    ]
    x0, y0 = 20, 30
    pad = 10
    line_h = 28
    w = max(cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)[0][0]
            for line in lines) + 2 * pad
    h = line_h * len(lines) + 2 * pad
    overlay = img.copy()
    cv2.rectangle(overlay, (x0, y0), (x0 + w, y0 + h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.65, img, 0.35, 0, img)
    for i, line in enumerate(lines):
        cv2.putText(img, line, (x0 + pad, y0 + pad + (i + 1) * line_h - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                    cv2.LINE_AA)


def _annotate(frame: np.ndarray, shot: dict, ball: Optional[dict],
              players: dict, kind: str) -> np.ndarray:
    img = frame.copy()

    # Bounce (red dot) from ball_detections (pixel space)
    if ball is not None:
        _draw_dot(img, (ball["x"], ball["y"]), (0, 0, 255))  # red BGR
        cv2.putText(img, "bounce", (int(ball["x"]) + 16, int(ball["y"]) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)

    # Hitter (green cross) — ONLY label a detection whose court_y matches
    # the silver row's hy within tolerance. If no tracked player matches,
    # the hitter is effectively missing from this frame window and we
    # flag it instead of falsely labeling the nearest person.
    hitter_match = _pick_hitter_pixel(players, shot)
    hitter_pixel = None
    if hitter_match is not None:
        hitter_pixel, src_frame, delta_m = hitter_match
        _draw_cross(img, hitter_pixel, (0, 255, 0))
        target_frame = int(round(shot["ball_hit_s"] * 25))
        frame_offset = src_frame - target_frame
        cv2.putText(
            img,
            f"hitter (hy={shot['hy']:.2f}m, match dy={delta_m:.2f}m, src frame {frame_offset:+d})",
            (int(hitter_pixel[0]) + 20, int(hitter_pixel[1]) + 6),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2, cv2.LINE_AA,
        )
    else:
        cv2.putText(
            img,
            f"!! no tracked player matches hy={shot['hy']:.2f}m in +/-30 frames",
            (20, frame.shape[0] - 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 3, cv2.LINE_AA,
        )

    # Draw the other detected players in grey so we can see what else the
    # tracker had available that frame — useful to confirm "the REAL
    # hitter wasn't tracked, only these other people were".
    for pid, p in players.items():
        pt = (p["center_x"], p["center_y"])
        if hitter_pixel and abs(pt[0] - hitter_pixel[0]) < 1 \
                and abs(pt[1] - hitter_pixel[1]) < 1:
            continue
        _draw_cross(img, pt, (180, 180, 180), size=10, thickness=2)
        label_y_m = p.get("court_y")
        label = f"pid {p['player_id']}" + (f" (y={label_y_m:.1f}m)" if label_y_m is not None else "")
        cv2.putText(img, label, (int(pt[0]) + 12, int(pt[1]) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1,
                    cv2.LINE_AA)

    _draw_label_panel(img, shot, kind)
    return img


def _make_contact_sheet(images: list, title: str, cols: int = 1) -> Optional[np.ndarray]:
    """Contact sheet: one image per row by default because the 3-frame
    strips are already 3x wide. cols>1 only makes sense for single-frame
    images (non-strip mode).
    """
    if not images:
        return None
    h, w = images[0].shape[:2]
    # Target row height ~360px so all shots fit on a reasonable page.
    target_row_h = 360
    scale = target_row_h / h
    thumb_w = int(w * scale)
    thumb_h = target_row_h
    rows = (len(images) + cols - 1) // cols
    sheet = np.full((rows * thumb_h + 60, cols * thumb_w, 3), 30, dtype=np.uint8)
    cv2.putText(sheet, title, (20, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                (255, 255, 255), 2, cv2.LINE_AA)
    for i, img in enumerate(images):
        r, c = divmod(i, cols)
        y0 = 60 + r * thumb_h
        x0 = c * thumb_w
        thumb = cv2.resize(img, (thumb_w, thumb_h))
        sheet[y0:y0 + thumb_h, x0:x0 + thumb_w] = thumb
    return sheet


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(task_id: str, video_path: str, output_dir: str) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL env var required", file=sys.stderr)
        return 2
    engine = create_engine(db_url)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    serves_dir = out / "serves"
    overs_dir = out / "overheads_not_serves"
    serves_dir.mkdir(exist_ok=True)
    overs_dir.mkdir(exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Could not open video: {video_path}", file=sys.stderr)
        return 2

    with engine.connect() as conn:
        fps = _get_fps(conn, task_id)
        # ball_hit_s stored in silver is the BOUNCE timestamp, not the
        # racket-contact timestamp. For a serve the ball travels ~0.3s
        # from contact to bounce, so extracting at ball_hit_s shows the
        # ball already in the opposite court. Back-track by ~0.32s
        # (same constant A1 uses to estimate hit frame from bounce) so
        # the viewer lands near the actual contact moment. Also emit
        # adjacent frames so the whole serve motion can be seen.
        hit_offset_frames = max(1, int(round(fps * 0.32)))
        sequence_offsets = [-hit_offset_frames - 5, -hit_offset_frames, 0]
        serves, overheads = _fetch_shots(conn, task_id)
        print(f"task_id={task_id}  fps={fps:.1f}  hit-back-track={hit_offset_frames} frames")
        print(f"  serves (serve_d=TRUE): {len(serves)}")
        print(f"  overheads (not serve): {len(overheads)}")

        serve_imgs, over_imgs = [], []

        for kind, shots, out_dir, sink in [
            ("SERVE", serves, serves_dir, serve_imgs),
            ("OVERHEAD-NOT-SERVE", overheads, overs_dir, over_imgs),
        ]:
            for shot in shots:
                bounce_frame = int(round(shot["ball_hit_s"] * fps))
                # Build a 3-frame strip: toss / contact / bounce so the
                # serve motion is visible, not just the aftermath.
                strip_frames = []
                for offset in sequence_offsets:
                    fi = max(0, bounce_frame + offset)
                    frame = _extract_frame(cap, fi)
                    if frame is None:
                        continue
                    label = {
                        sequence_offsets[0]: "toss (~0.5s before hit)",
                        sequence_offsets[1]: "CONTACT (estimated)",
                        sequence_offsets[2]: "bounce (ball_hit_s in silver)",
                    }.get(offset, f"+{offset} frames")
                    # Only fetch/annotate tracker detections for the
                    # contact frame (that's where the hitter logic ran)
                    if offset == sequence_offsets[1]:
                        ball = _fetch_ball_pixel(conn, task_id, fi)
                        players = _fetch_player_pixels(conn, task_id, fi)
                        frame = _annotate(frame, shot, ball, players, kind)
                    cv2.putText(frame, label, (20, frame.shape[0] - 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255),
                                3, cv2.LINE_AA)
                    strip_frames.append(frame)
                if not strip_frames:
                    print(f"  [skip id={shot['id']}] could not read any frame")
                    continue
                h = strip_frames[0].shape[0]
                w = strip_frames[0].shape[1]
                resized = [cv2.resize(f, (w // 2, h // 2)) for f in strip_frames]
                strip = np.hstack(resized)
                fname = f"{kind.lower()}_id{shot['id']:03d}_ts{shot['ball_hit_s']:06.1f}.png"
                path = out_dir / fname
                cv2.imwrite(str(path), strip)
                sink.append(strip)
                print(f"  [{kind}] id={shot['id']} ts={shot['ball_hit_s']:.2f} -> {path.name}")

        # Contact sheets
        sheet = _make_contact_sheet(serve_imgs, f"SERVES (n={len(serve_imgs)}) — task {task_id[:8]}")
        if sheet is not None:
            cv2.imwrite(str(out / "contact_serves.png"), sheet)
            print(f"  -> contact_serves.png")
        sheet = _make_contact_sheet(over_imgs, f"OVERHEADS NOT SERVES (n={len(over_imgs)}) — task {task_id[:8]}")
        if sheet is not None:
            cv2.imwrite(str(out / "contact_overheads.png"), sheet)
            print(f"  -> contact_overheads.png")

    cap.release()
    print(f"\nOutput: {out.resolve()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task_id")
    ap.add_argument("--video", required=True, help="Path to the source mp4")
    ap.add_argument("--output", default="./diag_out", help="Output directory")
    args = ap.parse_args(argv)
    return run(args.task_id, args.video, args.output)


if __name__ == "__main__":
    sys.exit(main())
