"""Production far-player pose extractor — runs in AWS Batch after the
main TennisAnalysisPipeline. Scans the whole video for far-baseline
person detections and runs ViTPose-Base to produce high-quality pose
keypoints. Writes to ml_analysis.player_detections_roi where the
serve_detector's merge logic picks them up.

Two ways to drive it:
  - extract_far_pose(...)   — standalone: owns its own VideoCapture decode.
    Used by the diag tooling (replay_roi_pose) and as a fallback.
  - FarPoseProcessor        — per-frame core (prepare / feed / finalize) so a
    single shared decode loop can fan one decoded frame out to both the pose
    and bounce ROI passes (roi_extractors/unified.py). The video is decoded
    ONCE for both ROI passes instead of once each. Same models, same frames,
    same rows — purely a decode-scheduling change.

Differs from ml_pipeline/diag/extract_vitpose_far.py:
  - No SA reference needed (scans entire video, not per-serve windows)
  - Not a CLI — called as a function from _run_batch
  - Writes directly to DB instead of deferring to a label JSON
  - Samples every Nth frame (default N=2) to keep total runtime bounded
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

import cv2
import numpy as np
from sqlalchemy import text as sql_text

logger = logging.getLogger("roi_pose")

COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97

# Far-baseline ROI in court metric space (where the far player stands).
# -8 m lets the ROI catch raised arms above head. +5 m covers the
# half-court side. See diag/extract_vitpose_far.py for the rationale.
FAR_ROI_Y_LO = -8.0
FAR_ROI_Y_HI = 5.0
FAR_ROI_X_PAD = 1.5

BBOX_EXPAND_W = 1.5
BBOX_EXPAND_H = 5.0

VITPOSE_REPO = "usyd-community/vitpose-plus-base"

DEFAULT_DET_CONF = 0.15
DEFAULT_WRIST_CONF = 0.3
DEFAULT_SHOULDER_CONF = 0.3


def _init_schema(conn):
    """Ensure ml_analysis.player_detections_roi exists (idempotent)."""
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.player_detections_roi (
            id          BIGSERIAL PRIMARY KEY,
            job_id      TEXT NOT NULL,
            frame_idx   INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            bbox_x1     DOUBLE PRECISION NOT NULL,
            bbox_y1     DOUBLE PRECISION NOT NULL,
            bbox_x2     DOUBLE PRECISION NOT NULL,
            bbox_y2     DOUBLE PRECISION NOT NULL,
            center_x    DOUBLE PRECISION NOT NULL,
            center_y    DOUBLE PRECISION NOT NULL,
            court_x     DOUBLE PRECISION,
            court_y     DOUBLE PRECISION,
            keypoints   JSONB,
            source      TEXT NOT NULL DEFAULT 'far_roi_pose',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS idx_player_detections_roi_job_player
            ON ml_analysis.player_detections_roi (job_id, player_id);
    """))


def _project(mx, my, detector):
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    calib = detector._calibration
    if calib is not None:
        p = proj(mx, my, calib)
        if p is not None:
            return p
    best = detector._locked_detection or detector._best_detection
    if best is not None and best.homography is not None:
        H_inv = np.linalg.inv(best.homography)
        pt = H_inv @ np.array([mx, my, 1.0])
        if pt[2] != 0:
            return float(pt[0] / pt[2]), float(pt[1] / pt[2])
    return None


def _compute_far_roi_pixel(detector, frame_shape, pad_px=20):
    corners_m = [
        (-FAR_ROI_X_PAD, FAR_ROI_Y_LO),
        (COURT_WIDTH_DOUBLES_M + FAR_ROI_X_PAD, FAR_ROI_Y_LO),
        (COURT_WIDTH_DOUBLES_M + FAR_ROI_X_PAD, FAR_ROI_Y_HI),
        (-FAR_ROI_X_PAD, FAR_ROI_Y_HI),
    ]
    pxs = []
    for mx, my in corners_m:
        p = _project(mx, my, detector)
        if p is None:
            return None
        pxs.append(p)
    xs = [p[0] for p in pxs]
    ys = [p[1] for p in pxs]
    h, w = frame_shape[:2]
    return (max(0, int(min(xs) - pad_px)),
            max(0, int(min(ys) - pad_px)),
            min(w, int(max(xs) + pad_px)),
            min(h, int(max(ys) + pad_px)))


def _expand_bbox(bbox, sw, sh, fw, fh, extend_down=4.0):
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2
    w = (x2 - x1) * sw
    h_orig = y2 - y1
    h_ext = h_orig * sh
    up_frac = 0.5 / (0.5 + extend_down)
    new_top = y1 - (h_ext - h_orig) * up_frac
    new_bot = y2 + (h_ext - h_orig) * (1.0 - up_frac)
    return (max(0, int(cx - w / 2)),
            max(0, int(new_top)),
            min(fw - 1, int(cx + w / 2)),
            min(fh - 1, int(new_bot)))


class FarPoseProcessor:
    """Per-frame far-baseline ViTPose core.

    Lifecycle:
        proc = FarPoseProcessor(job_id, engine, court_detector=..., bounces=...)
        if not proc.prepare(frame_shape):   # loads models, projects ROI, builds rally gate
            return 0
        for idx, frame in decode(video):    # caller owns the decode
            proc.feed(frame, idx)
        n = proc.finalize()                 # writes rows to DB, returns count

    `feed` applies the same sampling + rally gate the old monolithic loop did,
    so the rows written are identical regardless of who owns the decode. The
    split exists so unified.py can feed the SAME decoded frame to both this and
    the bounce processor (single decode for both ROI passes).
    """

    def __init__(
        self,
        job_id: str,
        engine,
        *,
        fps: float = 25.0,
        sample_every: int = 2,
        det_conf: float = DEFAULT_DET_CONF,
        source_tag: str = "far_vitpose",
        vitpose_repo: str = VITPOSE_REPO,
        court_detector=None,
        bounces: Optional[List] = None,
        frame_from: Optional[int] = None,
        frame_to: Optional[int] = None,
        replace: bool = True,
    ):
        self.job_id = job_id
        self.engine = engine
        self.fps = fps or 25.0
        self.sample_every = max(1, int(sample_every))
        self.det_conf = det_conf
        self.source_tag = source_tag
        self.vitpose_repo = vitpose_repo
        self.court_detector = court_detector
        self.bounces = bounces
        self.frame_from = frame_from
        self.frame_to = frame_to
        self.replace = replace

        self._ready = False
        self.rows_to_write: list = []
        # ROI pixel rect + frame dims (set in prepare)
        self.x0 = self.y0 = self.x1 = self.y1 = 0
        self.H_FRAME = self.W_FRAME = 0
        # rally gate
        self.rally = None
        self.rally_in_rally_state = None
        self.rally_gate_broken = False
        # counters (for the summary log)
        self.total_frames_probed = 0
        self.total_in_rally_skipped = 0
        self.total_dets = 0
        self.total_usable = 0

    # -- setup ---------------------------------------------------------------

    def prepare(self, frame_shape) -> bool:
        """Project the far ROI, load YOLO + ViTPose, build the rally gate.

        Returns False (and logs) if the ROI can't be projected — caller should
        then skip pose for this job. Mirrors the early-return guards in the old
        monolithic extract_far_pose."""
        if self.court_detector is None:
            logger.warning("roi_pose: no court_detector supplied; skipping")
            return False

        self.H_FRAME, self.W_FRAME = frame_shape[:2]
        roi = _compute_far_roi_pixel(self.court_detector, frame_shape)
        if roi is None:
            logger.warning("roi_pose: cannot project ROI corners; skipping")
            return False
        self.x0, self.y0, self.x1, self.y1 = roi
        logger.info(
            "roi_pose: far ROI pixel (%d,%d)-(%d,%d) size=%dx%d",
            self.x0, self.y0, self.x1, self.y1,
            self.x1 - self.x0, self.y1 - self.y0,
        )

        # Load detectors
        from ultralytics import YOLO
        from ml_pipeline.config import YOLO_WEIGHTS
        self.det_model = YOLO(YOLO_WEIGHTS)
        logger.info("roi_pose: YOLO loaded")

        import torch
        from transformers import VitPoseForPoseEstimation, VitPoseImageProcessor
        self._torch = torch
        self.vit_model = VitPoseForPoseEstimation.from_pretrained(self.vitpose_repo)
        self.vit_proc = VitPoseImageProcessor.from_pretrained(self.vitpose_repo)
        self.vit_model.eval()
        if torch.cuda.is_available():
            self.vit_model = self.vit_model.to("cuda")
            logger.info("roi_pose: ViTPose on cuda")
        else:
            logger.info("roi_pose: ViTPose on cpu")
        self.coco_idx = torch.tensor([0])

        self._build_rally_gate()
        self._ready = True
        return True

    def _build_rally_gate(self):
        """Build the IN_RALLY skip gate from the in-memory bounce list.

        Real serves only happen between rallies; mid-rally trophy poses
        (overheads, lobs, stretch volleys) are pose-locally indistinguishable
        from real serves at the baseline, so we skip them at the source. Bronze
        ml_analysis.ball_detections is empty at this stage (Render ingests
        bronze later from the JSON export), which is why the in-memory list is
        the right input — see handover_t5.md NEXT SESSION block."""
        if not self.bounces:
            logger.info(
                "roi_pose: no bounces supplied; processing all sampled frames "
                "(no rally gate)"
            )
            return
        try:
            from ml_pipeline.serve_detector.rally_state import (
                RallyStateMachine, RallyState,
            )
            from ml_pipeline.serve_detector.bounce_validity import validate_bounces
            raw_bounces = [
                {"frame_idx": d.frame_idx, "court_y": getattr(d, "court_y", None)}
                for d in self.bounces
                if getattr(d, "is_bounce", False)
            ]
            # Filter phantom near-baseline TrackNet clusters BEFORE feeding the
            # rally state machine (Tomo's bounce-validity rule, May 7). Without
            # this, racquet-bouncing pre-serve and TrackNet misclassifications
            # on near-court features hold the rally state IN_RALLY for 16-second
            # blocks, blocking ROI pose extraction during real serves
            # (a798eff0 misses 458/463/584).
            valid_bounces = validate_bounces(raw_bounces)
            bounce_ts = [b["frame_idx"] / self.fps for b in valid_bounces]
            self.rally = RallyStateMachine(bounce_ts=bounce_ts)
            self.rally_in_rally_state = RallyState.IN_RALLY
            logger.info(
                "roi_pose: rally gate active, %d valid bounces "
                "(filtered %d phantom of %d raw, from %d ball detections)",
                len(bounce_ts), len(raw_bounces) - len(valid_bounces),
                len(raw_bounces), len(self.bounces),
            )
        except Exception as e:
            # Caller asked for the gate (passed bounces) but we couldn't build
            # it. This is a packaging / dependency bug — fall through and
            # process every frame so the run still succeeds, but flag it loud
            # in logs so the next CloudWatch grep catches it instead of
            # silently regressing to baseline.
            logger.error(
                "roi_pose: BUG — rally gate requested but failed to build (%s). "
                "Falling back to UNGATED full-video scan. Fix me before relying "
                "on results.", e,
            )
            self.rally = None
            self.rally_in_rally_state = None
            self.rally_gate_broken = True

    # -- per-frame -----------------------------------------------------------

    def wants(self, idx: int) -> bool:
        """True if frame idx should be processed (range + sampling + rally gate).

        Side effect: increments the IN_RALLY-skip counter when the rally gate
        rejects a sampled frame, matching the old loop's accounting."""
        if self.frame_from is not None and idx < self.frame_from:
            return False
        if self.frame_to is not None and idx > self.frame_to:
            return False
        if idx % self.sample_every != 0:
            return False
        if self.rally is not None:
            if self.rally.state_at(idx / self.fps) == self.rally_in_rally_state:
                self.total_in_rally_skipped += 1
                return False
        return True

    def feed(self, frame, idx: int):
        """Run YOLO-det + ViTPose on one decoded frame if it passes the gate."""
        if not self._ready or not self.wants(idx):
            return
        self.total_frames_probed += 1

        roi_crop = frame[self.y0:self.y1, self.x0:self.x1]
        if roi_crop.size == 0:
            return

        det_res = self.det_model.predict(
            roi_crop, conf=self.det_conf, imgsz=1280, classes=[0], verbose=False,
        )
        if not det_res or det_res[0].boxes is None or len(det_res[0].boxes) == 0:
            return

        boxes = det_res[0].boxes.xyxy.cpu().numpy()
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        big = int(np.argmax(areas))
        bx1, by1, bx2, by2 = [float(v) for v in boxes[big]]
        fbx1 = bx1 + self.x0
        fby1 = by1 + self.y0
        fbx2 = bx2 + self.x0
        fby2 = by2 + self.y0
        self.total_dets += 1

        ebx1, eby1, ebx2, eby2 = _expand_bbox(
            (fbx1, fby1, fbx2, fby2),
            BBOX_EXPAND_W, BBOX_EXPAND_H, self.W_FRAME, self.H_FRAME,
        )
        bbox_w = ebx2 - ebx1
        bbox_h = eby2 - eby1
        if bbox_w <= 0 or bbox_h <= 0:
            return
        pose_input = frame[eby1:eby2, ebx1:ebx2]
        if pose_input.size == 0:
            return
        rgb = cv2.cvtColor(pose_input, cv2.COLOR_BGR2RGB)
        vit_inputs = self.vit_proc(
            images=[rgb],
            boxes=[[[0, 0, bbox_w, bbox_h]]],
            return_tensors="pt",
        )
        torch = self._torch
        if torch.cuda.is_available():
            vit_inputs = {k: v.to("cuda") for k, v in vit_inputs.items()}
        with torch.no_grad():
            vit_out = self.vit_model(
                pixel_values=vit_inputs["pixel_values"],
                dataset_index=self.coco_idx.to(self.vit_model.device),
            )
        results = self.vit_proc.post_process_pose_estimation(
            vit_out, boxes=[[[0, 0, bbox_w, bbox_h]]],
        )
        if not results or not results[0]:
            return
        pkp = results[0][0]["keypoints"].cpu().numpy()
        psc = results[0][0]["scores"].cpu().numpy()
        kp_full = np.column_stack([
            pkp[:, 0] + ebx1,
            pkp[:, 1] + eby1,
            psc,
        ])
        wrist_conf = float(max(kp_full[9, 2], kp_full[10, 2]))
        shoulder_conf = float(max(kp_full[5, 2], kp_full[6, 2]))
        has_usable = (
            (wrist_conf > DEFAULT_WRIST_CONF and shoulder_conf > DEFAULT_SHOULDER_CONF)
            or wrist_conf > 0.5
        )
        if not has_usable:
            return
        self.total_usable += 1

        feet_x = (fbx1 + fbx2) / 2
        feet_y = fby2
        # Project feet to real court coords. The diag-tool predecessor
        # (diag/extract_vitpose_far.py) hardcoded court_y=0.0 because it was
        # bounded to ±2.5s windows around SA-GT serves where the player WAS at
        # the baseline by definition. The production extractor scans the full
        # video, so we MUST keep the real projected court_y — without it,
        # downstream serve_detector can't tell a baseline trophy pose (real
        # serve setup) apart from a mid-court trophy pose (rally
        # overhead/forehand). Skip the row entirely when projection fails
        # (strict=False already gives ±5m slack for far-baseline calib noise).
        court = self.court_detector.to_court_coords(feet_x, feet_y, strict=False)
        if court is None:
            return
        cx, cy = float(court[0]), float(court[1])

        import json as _json
        kp_json = [
            [float(kp_full[j, 0]), float(kp_full[j, 1]), float(kp_full[j, 2])]
            for j in range(kp_full.shape[0])
        ]
        self.rows_to_write.append({
            "job_id": self.job_id, "frame_idx": idx, "player_id": 1,
            "bbox_x1": fbx1, "bbox_y1": fby1,
            "bbox_x2": fbx2, "bbox_y2": fby2,
            "center_x": feet_x, "center_y": feet_y,
            "court_x": cx, "court_y": cy,
            "keypoints": _json.dumps(kp_json),
            "source": self.source_tag,
        })

    # -- teardown ------------------------------------------------------------

    def finalize(self, scan_seconds: Optional[float] = None) -> int:
        """Write collected rows to DB (replace prior source rows). Returns count."""
        gate_tag = " [RALLY GATE BROKEN — UNGATED RESULTS]" if self.rally_gate_broken else ""
        dt = f" in {scan_seconds:.1f}s" if scan_seconds is not None else ""
        logger.info(
            "roi_pose: scanned %d sampled frames (every %d), skipped %d IN_RALLY "
            "frames, %d detections, %d usable poses%s%s",
            self.total_frames_probed, self.sample_every, self.total_in_rally_skipped,
            self.total_dets, self.total_usable, dt, gate_tag,
        )

        if not self.rows_to_write:
            logger.info("roi_pose: nothing to write")
            return 0

        with self.engine.begin() as conn:
            _init_schema(conn)
            if self.replace:
                n_del = conn.execute(sql_text("""
                    DELETE FROM ml_analysis.player_detections_roi
                    WHERE job_id = :tid AND source = :src
                """), {"tid": self.job_id, "src": self.source_tag}).rowcount
                if n_del:
                    logger.info("roi_pose: deleted %d prior rows (source=%s)",
                                n_del, self.source_tag)
            conn.execute(sql_text("""
                INSERT INTO ml_analysis.player_detections_roi
                  (job_id, frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                   center_x, center_y, court_x, court_y, keypoints, source)
                VALUES
                  (:job_id, :frame_idx, :player_id, :bbox_x1, :bbox_y1, :bbox_x2,
                   :bbox_y2, :center_x, :center_y, :court_x, :court_y,
                   CAST(:keypoints AS JSONB), :source)
            """), self.rows_to_write)
        logger.info("roi_pose: wrote %d rows (source=%s)",
                    len(self.rows_to_write), self.source_tag)
        return len(self.rows_to_write)


def extract_far_pose(
    video_path: str,
    job_id: str,
    engine,
    *,
    fps: float = 25.0,
    sample_every: int = 2,
    det_conf: float = DEFAULT_DET_CONF,
    source_tag: str = "far_vitpose",
    vitpose_repo: str = VITPOSE_REPO,
    calib_frames: int = 300,
    court_detector=None,
    bounces: Optional[List] = None,
    frame_from: Optional[int] = None,
    frame_to: Optional[int] = None,
    replace: bool = True,
) -> int:
    """Run far-baseline ViTPose extraction across the entire video (standalone).

    Owns its own VideoCapture decode. For the production path where the bounce
    pass also needs to decode the video, prefer roi_extractors.unified which
    decodes once and feeds both this and the bounce processor.

    Writes rows to ml_analysis.player_detections_roi for every sampled frame
    that YOLO detects a person in the far-baseline ROI and ViTPose returns
    usable keypoints.

    Args:
        video_path: local filesystem path to the video (Batch has it).
        job_id: the ml_analysis.video_analysis_jobs.job_id (used as FK).
        engine: SQLAlchemy engine (DATABASE_URL).
        fps: video fps (default 25, used for timestamp calc).
        sample_every: process 1 of every N frames. 2 = 12.5 fps effective.
        det_conf: YOLO person-detection confidence threshold.
        source_tag: ml_analysis.player_detections_roi.source value.
        vitpose_repo: HuggingFace repo for the ViTPose model weights.
        calib_frames: frames used by CourtDetector to lock the homography.
        court_detector: an already-calibrated CourtDetector instance. If None,
            a new one is built from the first calib_frames of the video. Pass
            the pipeline's court detector when available to save ~10-20 s of
            re-calibration.
        bounces: in-memory list of BallDetection-like objects with frame_idx +
            is_bounce attributes (typically result.ball_detections from the
            just-finished pipeline.process()). When supplied, frames whose ts
            falls inside an IN_RALLY window are skipped at the source. When
            None, every sampled frame is processed.
        frame_from / frame_to: optional inclusive frame range to limit the scan.
            Default None on both = whole video (production behaviour). Used by
            diag tooling (replay_roi_pose) to test specific windows.
        replace: when True (default), DELETEs prior rows for (job_id, source)
            before inserting — production idempotency. Set False for diag /
            additive scans where multiple frame-range runs share a source_tag.

    Returns:
        Number of rows written.
    """
    if not os.path.exists(video_path):
        logger.warning("roi_pose: video not found: %s; skipping", video_path)
        return 0

    t_start = time.time()

    # 1. Court calibration — reuse pipeline's detector if provided.
    if court_detector is None:
        from ml_pipeline.court_detector import CourtDetector
        court_detector = CourtDetector()
        cap = cv2.VideoCapture(video_path)
        try:
            for i in range(calib_frames + 1):
                ok, f = cap.read()
                if not ok:
                    break
                court_detector.detect(f, i)
        finally:
            cap.release()
        if (court_detector._locked_detection is None
                and court_detector._best_detection is None):
            logger.warning("roi_pose: court calibration failed; skipping")
            return 0

    # 2. Read the first frame for shape, build the processor.
    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    if not ok:
        cap.release()
        logger.warning("roi_pose: cannot read first frame; skipping")
        return 0

    proc = FarPoseProcessor(
        job_id, engine,
        fps=fps, sample_every=sample_every, det_conf=det_conf,
        source_tag=source_tag, vitpose_repo=vitpose_repo,
        court_detector=court_detector, bounces=bounces,
        frame_from=frame_from, frame_to=frame_to, replace=replace,
    )
    if not proc.prepare(first.shape):
        cap.release()
        return 0

    # 3. Decode + feed. Seek to frame_from if set — saves walking through tens
    #    of thousands of frames just to discard them.
    start_frame = frame_from if frame_from is not None else 0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    idx = start_frame
    while True:
        if frame_to is not None and idx > frame_to:
            break
        ok, frame = cap.read()
        if not ok:
            break
        proc.feed(frame, idx)
        idx += 1
    cap.release()

    return proc.finalize(scan_seconds=time.time() - t_start)
