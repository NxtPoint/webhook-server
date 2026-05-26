"""Interactive hand-labeller for ground-truth ball bounces (Workstream A).

WHY THIS EXISTS
---------------
SportAI's ball-bounce data is unreliable (Tomo: SA is accurate on most
signals but weak on bounce), so it cannot be the yardstick for measuring
T5 bounce recall / precision / xy-accuracy. The other labellers in this
package (`label_ball_positions.py`, `label_serve_bounces.py`) are
SA-as-teacher exports — useful for TrackNet *position* training, NOT for
bounce-event ground truth. This tool produces an **SA-independent** truth
set: a human scrubs the video and marks every floor bounce.

See docs/_investigation/bounce_accuracy.md §8 (Workstream A).

OUTPUT (JSON, resumable — re-running reloads and lets you keep going):
{
  "video": "...", "video_fps": 25.0, "frame_width": 1920, "frame_height": 1080,
  "labels": [
    {"frame_idx": 1362, "pixel_x": 1043.0, "pixel_y": 612.0,
     "ts": 54.48, "type": "floor", "confidence": "high"},
    ...
  ]
}
Pixel coords are in ORIGINAL video space. Court (x,y) is NOT stored here —
projection to metres is a scoring-time concern (reuse the faithful
player-feet homography; see diag/bounce_xy_accuracy.py). Keeping the
labeller projection-free means it needs no calibration to run.

CONTROLS (GUI window)
  left-click            mark a bounce at the cursor (current type+confidence)
  right-click           delete the nearest label on the current frame
  a / d                 step -1 / +1 frame
  A / D (shift)         step -10 / +10 frames
  w / e                 step -100 / +100 frames
  p                     play/pause (forward); any key pauses
  f / s                 set pending TYPE = floor / swing
  h / l                 set pending CONFIDENCE = high / low
                        (tip: far-court / top-of-frame bounces -> low)
  u                     undo last-added label
  z                     save now
  q / ESC               save and quit

USAGE
  # smoke-test plumbing (no GUI): video opens, dims/fps, JSON roundtrip
  python -m ml_pipeline.training.label_bounces_manual --selfcheck

  # label the a798eff0 bench-reference match (video is local)
  python -m ml_pipeline.training.label_bounces_manual \
      --video ml_pipeline/test_videos/a798eff0_sa_video.mp4

  # custom output / start frame / display scale
  python -m ml_pipeline.training.label_bounces_manual \
      --video <path> --out ml_pipeline/ground_truth/a798eff0_bounces.json \
      --start-frame 1300 --scale 0.6

NOTE: needs a GUI-capable OpenCV (opencv-python, NOT opencv-python-headless).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import List, Optional

import cv2

DEFAULT_VIDEO = "ml_pipeline/test_videos/a798eff0_sa_video.mp4"
GROUND_TRUTH_DIR = "ml_pipeline/ground_truth"


def _default_out(video: str) -> str:
    stem = Path(video).stem.replace(".mp4", "")
    return os.path.join(GROUND_TRUTH_DIR, f"{stem}_bounces.json")


def _load(out_path: str) -> List[dict]:
    if os.path.exists(out_path):
        with open(out_path) as f:
            data = json.load(f)
        return data.get("labels", [])
    return []


def _save(out_path: str, video: str, fps: float, w: int, h: int, labels: List[dict]) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    labels = sorted(labels, key=lambda d: d["frame_idx"])
    with open(out_path, "w") as f:
        json.dump({
            "video": video, "video_fps": fps,
            "frame_width": w, "frame_height": h,
            "labels": labels,
        }, f, indent=2)
    print(f"[saved] {len(labels)} labels -> {out_path}")


def selfcheck(video: str, out_path: str) -> int:
    """Headless plumbing check — no GUI. Returns process exit code."""
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"[FAIL] cannot open video: {video}")
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[ok] video opened: {w}x{h}  fps={fps}  frames={n}  dur~{n/fps:.0f}s")
    ok0, _ = cap.read()
    cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
    okm, _ = cap.read()
    cap.release()
    print(f"[ok] frame reads: first={ok0} mid={okm}")
    # JSON roundtrip with a throwaway path
    tmp = out_path + ".selfcheck"
    _save(tmp, video, fps or 25.0, w, h,
          [{"frame_idx": 100, "pixel_x": 10.0, "pixel_y": 20.0,
            "ts": 4.0, "type": "floor", "confidence": "high"}])
    rt = _load(tmp)
    os.remove(tmp)
    ok_rt = len(rt) == 1 and rt[0]["frame_idx"] == 100
    print(f"[ok] JSON roundtrip: {ok_rt}")
    if not (ok0 and okm and ok_rt):
        print("[FAIL] plumbing check failed")
        return 1
    print("[PASS] selfcheck OK — tool is runnable (GUI needs a display + opencv-python).")
    return 0


class _State:
    def __init__(self, labels, frame, pending_type, pending_conf, scale):
        self.labels = labels
        self.frame = frame
        self.pending_type = pending_type
        self.pending_conf = pending_conf
        self.scale = scale
        self.dirty = False


def _on_mouse(event, x, y, flags, st: "_State"):
    if event == cv2.EVENT_LBUTTONDOWN:
        ox, oy = x / st.scale, y / st.scale
        st.labels.append({
            "frame_idx": int(st.frame),
            "pixel_x": round(ox, 1), "pixel_y": round(oy, 1),
            "type": st.pending_type, "confidence": st.pending_conf,
        })
        st.dirty = True
    elif event == cv2.EVENT_RBUTTONDOWN:
        here = [l for l in st.labels if l["frame_idx"] == int(st.frame)]
        if here:
            ox, oy = x / st.scale, y / st.scale
            nearest = min(here, key=lambda l: (l["pixel_x"] - ox) ** 2 + (l["pixel_y"] - oy) ** 2)
            st.labels.remove(nearest)
            st.dirty = True


def run_gui(video: str, out_path: str, start_frame: int, scale: float) -> int:
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"[FAIL] cannot open video: {video}")
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    st = _State(_load(out_path), max(0, min(start_frame, n - 1)), "floor", "high", scale)
    print(f"[load] {len(st.labels)} existing labels from {out_path}")
    win = "bounce-labeller  (q=save+quit, z=save, u=undo, click=mark)"
    try:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    except cv2.error as e:
        print(f"[FAIL] OpenCV GUI unavailable ({e}). Install opencv-python (not -headless).")
        cap.release()
        return 1
    cv2.setMouseCallback(win, _on_mouse, st)

    playing = False
    last_read = -1
    frame_img = None
    while True:
        if int(st.frame) != last_read:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(st.frame))
            ok, frame_img = cap.read()
            if not ok:
                st.frame = max(0, int(st.frame) - 1)
                playing = False
                continue
            last_read = int(st.frame)

        disp = cv2.resize(frame_img, None, fx=scale, fy=scale) if scale != 1.0 else frame_img.copy()
        # markers for labels on this frame
        for l in st.labels:
            if l["frame_idx"] == int(st.frame):
                px, py = int(l["pixel_x"] * scale), int(l["pixel_y"] * scale)
                col = (0, 255, 0) if l["confidence"] == "high" else (0, 200, 255)
                cv2.drawMarker(disp, (px, py), col, cv2.MARKER_CROSS, 18, 2)
                cv2.putText(disp, l["type"][0].upper(), (px + 8, py - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
        ts = int(st.frame) / fps
        hud = (f"f {int(st.frame)}/{n}  t {ts:6.2f}s  | labels {len(st.labels)} "
               f"| pending: {st.pending_type}/{st.pending_conf} "
               f"| {'PLAY' if playing else 'paused'}{'  *' if st.dirty else ''}")
        cv2.rectangle(disp, (0, 0), (disp.shape[1], 22), (0, 0, 0), -1)
        cv2.putText(disp, hud, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.imshow(win, disp)

        k = cv2.waitKey(int(1000 / fps) if playing else 0) & 0xFF
        if playing and k == 255:  # no key -> advance one frame
            if int(st.frame) < n - 1:
                st.frame += 1
            else:
                playing = False
            continue
        if k in (ord('q'), 27):
            break
        elif k == ord('p'):
            playing = not playing
        elif k == ord('a'):
            st.frame = max(0, int(st.frame) - 1)
        elif k == ord('d'):
            st.frame = min(n - 1, int(st.frame) + 1)
        elif k == ord('A'):
            st.frame = max(0, int(st.frame) - 10)
        elif k == ord('D'):
            st.frame = min(n - 1, int(st.frame) + 10)
        elif k == ord('w'):
            st.frame = max(0, int(st.frame) - 100)
        elif k == ord('e'):
            st.frame = min(n - 1, int(st.frame) + 100)
        elif k == ord('f'):
            st.pending_type = "floor"
        elif k == ord('s'):
            st.pending_type = "swing"
        elif k == ord('h'):
            st.pending_conf = "high"
        elif k == ord('l'):
            st.pending_conf = "low"
        elif k == ord('u') and st.labels:
            st.labels.pop()
            st.dirty = True
        elif k == ord('z'):
            # add ts on save
            for l in st.labels:
                l["ts"] = round(l["frame_idx"] / fps, 2)
            _save(out_path, video, fps, w, h, st.labels)
            st.dirty = False

    for l in st.labels:
        l["ts"] = round(l["frame_idx"] / fps, 2)
    _save(out_path, video, fps, w, h, st.labels)
    cap.release()
    cv2.destroyAllWindows()
    return 0


def main():
    ap = argparse.ArgumentParser(description="Interactive ground-truth bounce labeller.")
    ap.add_argument("--video", default=DEFAULT_VIDEO)
    ap.add_argument("--out", default=None, help="output JSON (default: ground_truth/<stem>_bounces.json)")
    ap.add_argument("--start-frame", type=int, default=0)
    ap.add_argument("--scale", type=float, default=0.6, help="display scale (clicks stored in original px)")
    ap.add_argument("--selfcheck", action="store_true", help="headless plumbing check, no GUI")
    args = ap.parse_args()

    out_path = args.out or _default_out(args.video)
    if args.selfcheck:
        raise SystemExit(selfcheck(args.video, out_path))
    raise SystemExit(run_gui(args.video, out_path, args.start_frame, args.scale))


if __name__ == "__main__":
    main()
