"""Bootstrap a ball-tracker fixture manifest for a T5 task.

Run on the Render shell (or anywhere with DATABASE_URL). Picks a handful of
representative frame windows from a task's SportAI silver truth and writes a
JSON manifest that `replay_ball` / `bench_ball` consume offline.

The manifest is the smallest thing we can commit to git — frames live in the
source video, which is already in `ml_pipeline/test_videos/<task>.mp4` or
fetched from S3 by the user.

Default window selection:
  - warmup       : first 300 frames (12s at 25fps) — high signal for phantom
                   bounce / detector-fires-on-nothing regressions
  - rally_p<N>   : ±100 frames around the first ``--rally-windows`` SA serves
                   (default 3). Picks a spread by stepping through the match.
  - known_miss_* : task-specific windows from ``KNOWN_MISS_WINDOWS`` (e.g.
                   a798eff0's 458/463/584 FAR misses) so the bench surfaces
                   the regimes we know the tracker struggles in.

Add task-specific entries to ``KNOWN_MISS_WINDOWS`` as new fixtures are added.

The user can hand-edit the resulting JSON to add / remove windows after the
snapshot runs — the tool is a bootstrap, not a source of truth.

Usage (Render shell):
    python -m ml_pipeline.diag.snapshot_task_ball \
        --task a798eff0-551f-4b5a-838f-7933866a727c \
        --sportai 2c1ad953-b65b-41b4-9999-975964ff92e1 \
        --video-local-path ml_pipeline/test_videos/a798eff0_sa_video.mp4
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text


logger = logging.getLogger(__name__)


DEFAULT_SA = "2c1ad953-b65b-41b4-9999-975964ff92e1"
WARMUP_FRAMES = 300
WINDOW_HALF_FRAMES = 100   # ±100 frames around an SA event = 8s at 25fps


# Frame-index windows for known-miss regimes, by T5 task short prefix.
# Add entries here as bench coverage grows.
KNOWN_MISS_WINDOWS: dict[str, list[dict]] = {
    "a798eff0": [
        # FAR misses 458.08, 463.52, 584.92 from project_t5_may07_phantom_bounces.
        # Frame = ts * fps (25). Window ±100 frames.
        {"name": "far_miss_458", "centre_ts": 458.08},
        {"name": "far_miss_463", "centre_ts": 463.52},
        {"name": "far_miss_584", "centre_ts": 584.92},
    ],
}


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _load_sa_serves(conn, sa_task_id: str) -> list[dict]:
    """Pull SA-confirmed serves (one row per match-start serve)."""
    rows = conn.execute(sql_text("""
        SELECT ball_hit_s AS ts, serve_side_d AS side, point_number AS pn
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
        ORDER BY ball_hit_s
    """), {"tid": sa_task_id}).mappings().all()
    return [dict(r) for r in rows]


def _load_sa_shots(conn, sa_task_id: str) -> list[dict]:
    """Pull every SA shot (serves + rally hits) — used to populate
    `sa_bounce_frames` for the recall metric in any window we keep."""
    rows = conn.execute(sql_text("""
        SELECT ball_hit_s AS ts
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND ball_hit_s IS NOT NULL
        ORDER BY ball_hit_s
    """), {"tid": sa_task_id}).mappings().all()
    return [dict(r) for r in rows]


def _job_fps(conn, t5_task_id: str) -> float:
    fps = conn.execute(sql_text(
        "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id = :tid OR task_id = :tid LIMIT 1",
    ), {"tid": t5_task_id}).scalar()
    return float(fps or 25.0)


def _pick_rally_windows(serves: list[dict], fps: float, n: int) -> list[dict]:
    """Pick `n` serves spread across the match — first, last, and (n-2) equally
    spaced in between. Returns window dicts in fixture-manifest shape."""
    if not serves or n <= 0:
        return []
    if n >= len(serves):
        picks = list(range(len(serves)))
    else:
        # First, last, and evenly distributed middle indices.
        step = (len(serves) - 1) / (n - 1) if n > 1 else 0
        picks = sorted({int(round(i * step)) for i in range(n)})

    windows = []
    for k, idx in enumerate(picks):
        ts = float(serves[idx]["ts"])
        centre = int(round(ts * fps))
        start = max(0, centre - WINDOW_HALF_FRAMES)
        windows.append({
            "name": f"rally_p{k+1}",
            "start_frame": start,
            "n_frames": WINDOW_HALF_FRAMES * 2,
            "_centre_ts": round(ts, 2),
        })
    return windows


def _pick_known_miss_windows(task_id: str, fps: float) -> list[dict]:
    prefix = task_id[:8]
    entries = KNOWN_MISS_WINDOWS.get(prefix, [])
    out = []
    for e in entries:
        centre = int(round(e["centre_ts"] * fps))
        out.append({
            "name": e["name"],
            "start_frame": max(0, centre - WINDOW_HALF_FRAMES),
            "n_frames": WINDOW_HALF_FRAMES * 2,
            "_centre_ts": e["centre_ts"],
        })
    return out


def _sa_frames_in_windows(
    shots: list[dict], windows: list[dict], fps: float,
) -> list[int]:
    """Project SA shot timestamps onto frame indices, keep only those that
    fall inside any configured window."""
    span_pairs = [(w["start_frame"], w["start_frame"] + w["n_frames"]) for w in windows]
    out: list[int] = []
    for s in shots:
        if s.get("ts") is None:
            continue
        f = int(round(float(s["ts"]) * fps))
        if any(a <= f < b for a, b in span_pairs):
            out.append(f)
    # Deduplicate preserving order — same SA event can appear multiple times in
    # silver (one row per stroke; serves often have a hit row + bounce row).
    seen: set[int] = set()
    uniq: list[int] = []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def build_manifest(
    *, t5_task_id: str, sa_task_id: str, video_local_path: str,
    fps: float, serves: list[dict], shots: list[dict],
    rally_windows: int,
) -> dict:
    windows = [
        {"name": "warmup", "start_frame": 0, "n_frames": WARMUP_FRAMES},
    ]
    windows.extend(_pick_rally_windows(serves, fps, rally_windows))
    windows.extend(_pick_known_miss_windows(t5_task_id, fps))

    sa_bounce_frames = _sa_frames_in_windows(shots, windows, fps)

    return {
        "task_id": t5_task_id,
        "sa_task_id": sa_task_id,
        "video_local_path": video_local_path,
        "fps": fps,
        "windows": windows,
        "sa_bounce_frames": sa_bounce_frames,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="T5 task_id")
    ap.add_argument("--sportai", default=DEFAULT_SA, help="SportAI reference task_id")
    ap.add_argument(
        "--video-local-path", required=True,
        help="Path to the local video file (relative to repo root, e.g. "
             "ml_pipeline/test_videos/a798eff0_sa_video.mp4). Used by replay; "
             "doesn't need to exist on the machine running the snapshot.",
    )
    ap.add_argument("--out", default=None,
                    help="Output manifest path (default ml_pipeline/fixtures_ball/<task_short>.json)")
    ap.add_argument("--rally-windows", type=int, default=3,
                    help="How many SA-serve-centred windows to include (default 3)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    out_path = args.out
    if out_path is None:
        out_dir = Path("ml_pipeline/fixtures_ball")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"{args.task[:8]}.json")

    engine = create_engine(_normalize_db_url(db_url))
    with engine.connect() as conn:
        fps = _job_fps(conn, args.task)
        serves = _load_sa_serves(conn, args.sportai)
        shots = _load_sa_shots(conn, args.sportai)

    print(f"=== snapshot_task_ball task={args.task[:8]} sa={args.sportai[:8]} ===")
    print(f"  fps={fps:.2f}  sa_serves={len(serves)}  sa_shots={len(shots)}")

    manifest = build_manifest(
        t5_task_id=args.task, sa_task_id=args.sportai,
        video_local_path=args.video_local_path,
        fps=fps, serves=serves, shots=shots,
        rally_windows=args.rally_windows,
    )

    print(f"  windows={len(manifest['windows'])}  "
          f"sa_bounce_frames_in_windows={len(manifest['sa_bounce_frames'])}")
    for w in manifest["windows"]:
        end = w["start_frame"] + w["n_frames"]
        centre_s = w.get("_centre_ts", "-")
        print(f"    {w['name']:<18} frames {w['start_frame']:>6}–{end:>6}  centre_ts={centre_s}")

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  -> wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
