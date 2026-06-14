"""
ml_pipeline/__main__.py — Entry point for the tennis ML analysis pipeline.

Usage:
    # Local mode
    python -m ml_pipeline <video_path>

    # AWS Batch mode (S3 input → DB output)
    python -m ml_pipeline --job-id <job_id> --s3-key <s3_key>
"""

import sys
import os
import argparse
import logging
import tempfile
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def analyse_video(video_path: str, device: str = None, practice: bool = False):
    """Public API: analyse a tennis video and return structured results."""
    from ml_pipeline.pipeline import TennisAnalysisPipeline
    pipeline = TennisAnalysisPipeline(device=device, practice=practice)
    return pipeline.process(video_path)


def _run_local(video_path: str, practice: bool = False):
    """Local dev mode: analyse a file and print stats."""
    result = analyse_video(video_path, practice=practice)
    print(f"\n{'='*60}")
    print(f"Frames processed:   {result.total_frames_processed}")
    print(f"Ball detection %:   {result.ball_detection_rate*100:.1f}%")
    print(f"Court detected:     {result.court_detected} (conf={result.court_confidence:.2f})")
    print(f"Players found:      {result.player_count}")
    print(f"Bounces:            {result.bounce_count} (in={result.bounces_in}, out={result.bounces_out})")
    print(f"Rallies:            {result.rally_count}")
    print(f"Avg rally length:   {result.avg_rally_length:.1f} bounces")
    print(f"Serves:             {result.serve_count}")
    print(f"First serve %:      {result.first_serve_pct:.1f}%")
    print(f"Max speed:          {result.max_speed_kmh:.1f} km/h")
    print(f"Avg speed:          {result.avg_speed_kmh:.1f} km/h")
    print(f"Processing time:    {result.processing_time_sec:.1f}s")
    print(f"ms/frame:           {result.ms_per_frame:.1f}")
    print(f"Frame errors:       {result.frame_errors}")
    print(f"{'='*60}")


def _probe_video_codec(source_path: str) -> str:
    """Return the video codec name (e.g. 'h264', 'hevc', 'prores')."""
    import subprocess
    ffprobe_bin = os.environ.get("FFPROBE_BIN", "ffprobe")
    try:
        result = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", source_path],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip().lower()
    except Exception:
        return "unknown"


def _transcode_to_mp4(source_path: str) -> str:
    """
    Compress video for browser streaming.
    Scales to 720p max height, CQ/CRF 28, fast preset.

    L5 (Lever #5 from docs/_investigation/batch_optimisation_plan.md):
    primary encoder is h264_nvenc — the T4 host has dedicated NVENC silicon,
    typically 5-10× faster than libx264 ultrafast at comparable quality
    (NVENC p4 preset with -cq 28 is the standard CRF-28 equivalent). On a
    44-min 1080p→720p source this saves ~5-10 min wall time per job.

    Auto-fallback to libx264 if NVENC fails (driver / capability issue /
    image lacks the nvenc encoder). Output is much smaller than source
    (~80-90% reduction for raw phone footage).
    """
    import subprocess
    out_fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(out_fd)

    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "ffmpeg")
    codec = _probe_video_codec(source_path)
    logger.info(f"Source codec: {codec}")

    # Env-gated primary codec — defaults to nvenc, set TRANSCODE_CODEC=libx264
    # on the job-def to pin the CPU path if NVENC ever causes trouble.
    primary = os.environ.get("TRANSCODE_CODEC", "h264_nvenc").strip()

    def _cmd(codec_name: str):
        if codec_name == "h264_nvenc":
            return [
                ffmpeg_bin, "-y",
                "-i", source_path,
                "-c:v", "h264_nvenc",
                "-preset", "p4",          # NVENC speed/quality balance (p1=best p7=fastest)
                "-rc", "vbr",
                "-cq", "28",               # constant-quality target (CRF-28 equivalent)
                "-vf", "scale=-2:720",
                "-c:a", "aac",
                "-b:a", "96k",
                "-movflags", "+faststart",
                out_path,
            ]
        # libx264 fallback / pinned CPU path — same params as pre-L5.
        return [
            ffmpeg_bin, "-y",
            "-i", source_path,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-vf", "scale=-2:720",
            "-c:a", "aac",
            "-b:a", "96k",
            "-movflags", "+faststart",
            out_path,
        ]

    cmd = _cmd(primary)
    logger.info(f"Compressing for streaming ({primary}): {' '.join(cmd)}")
    t_enc = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    enc_sec = time.time() - t_enc

    # NVENC failure auto-fallback. Covers two real failure modes:
    #   (a) the image's ffmpeg is built without --enable-nvenc → ffmpeg
    #       reports "Unknown encoder 'h264_nvenc'" and exits non-zero;
    #   (b) NVENC capability missing at runtime (driver mismatch, max
    #       concurrent encoders exceeded on shared GPU, etc.).
    # We only fall back when nvenc was the primary AND it failed —
    # an explicit TRANSCODE_CODEC=libx264 won't trigger a redundant
    # second attempt.
    if result.returncode != 0 and primary == "h264_nvenc":
        logger.warning(
            "NVENC transcode failed (rc=%d, %.1fs); falling back to libx264. "
            "stderr tail: %s",
            result.returncode, enc_sec, result.stderr[-500:],
        )
        cmd = _cmd("libx264")
        logger.info(f"Fallback transcode (libx264): {' '.join(cmd)}")
        t_enc = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        enc_sec = time.time() - t_enc

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed (rc={result.returncode}): {result.stderr[-500:]}")

    src_size = os.path.getsize(source_path)
    out_size = os.path.getsize(out_path)
    logger.info(
        "Complete: %d → %d bytes (%.0f%%) in %.1fs",
        src_size, out_size, out_size / src_size * 100, enc_sec,
    )
    return out_path


def _run_batch(job_id: str, s3_key: str, practice: bool = False):
    """
    AWS Batch mode: download video from S3, run pipeline, save results to DB,
    upload heatmaps to S3, transcode to MP4, clean up source.
    """
    import boto3
    from sqlalchemy import text as sql_text
    from ml_pipeline.db_schema import ml_analysis_init, _get_engine
    from ml_pipeline.db_writer import MLDBWriter
    from ml_pipeline.pipeline import TennisAnalysisPipeline
    from ml_pipeline.heatmaps import generate_all_heatmaps
    from ml_pipeline.bronze_export import export_bronze_to_s3

    s3_bucket = os.environ["S3_BUCKET"]
    aws_region = os.environ.get("AWS_REGION", "us-east-1")

    # S3 bucket region can differ from the compute region when a job runs
    # in a fallback region (e.g. us-east-1 Batch executing against a bucket
    # that lives in eu-north-1). Pinning the S3 client to the compute region
    # produces a spurious 404 on head_object for cross-region buckets
    # because HeadObject does not follow the 301 redirect transparently.
    # Resolve the bucket's home region once, fall back to AWS_REGION on
    # failure so single-region deployments still work.
    try:
        _loc_client = boto3.client("s3", region_name="us-east-1")
        _loc = _loc_client.get_bucket_location(Bucket=s3_bucket)
        s3_region = _loc.get("LocationConstraint") or "us-east-1"
        if s3_region != aws_region:
            logger.info(
                f"S3 bucket {s3_bucket} lives in {s3_region}; compute region is "
                f"{aws_region}. Pinning S3 client to {s3_region}."
            )
    except Exception as e:
        logger.warning(
            f"get_bucket_location failed for {s3_bucket}: {e}; "
            f"falling back to AWS_REGION={aws_region}"
        )
        s3_region = aws_region

    engine = _get_engine()
    ml_analysis_init(engine)
    db = MLDBWriter(engine)

    batch_start = time.time()
    batch_job_id = os.environ.get("AWS_BATCH_JOB_ID", "local")
    batch_job_arn = os.environ.get("AWS_BATCH_JOB_ARN")

    # Record batch start
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE ml_analysis.video_analysis_jobs
            SET batch_job_id = :batch_job_id,
                batch_job_arn = :batch_job_arn,
                batch_start_at = now(),
                status = 'processing',
                updated_at = now()
            WHERE job_id = :job_id
        """), {"job_id": job_id, "batch_job_id": batch_job_id, "batch_job_arn": batch_job_arn})

    # Progress callback writes to DB
    def on_progress(stage: str, pct: int):
        db.update_job_progress(job_id, stage, pct)

    tmp_path = None
    try:
        # 1. Download from S3
        on_progress("downloading", 5)
        s3 = boto3.client("s3", region_name=s3_region)
        ext = os.path.splitext(s3_key)[1] or ".mp4"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(tmp_fd)

        logger.info(f"Downloading s3://{s3_bucket}/{s3_key} → {tmp_path}")
        s3.download_file(s3_bucket, s3_key, tmp_path)
        logger.info(f"Download complete ({os.path.getsize(tmp_path)} bytes)")

        # 2. Run pipeline (with live debug frame S3 upload context)
        pipeline = TennisAnalysisPipeline(progress_callback=on_progress, practice=practice)
        # Enable LIVE debug frame upload — user can inspect frames mid-run
        # and cancel bad runs without waiting for full ML processing
        pipeline.player_tracker.set_debug_upload_context(s3, s3_bucket, job_id)
        result = pipeline.process(tmp_path)

        # D1 (GPU memory audit) — phase boundary cleanup. The main loop's detection
        # data is already copied into `result` (player_detections / ball_detections),
        # so the GPU still holds only the main-loop allocator cache (activations,
        # fragmented blocks) when the ROI sweep is about to load ViTPose + bounce
        # TrackNet. Return that cache to the driver before ROI allocates — directly
        # targets the rev-58 ROI OOM theme. empty_cache() only frees unreferenced
        # cached blocks (PyTorch re-allocates on demand), so it is correctness-neutral.
        # Heavier model-weight eviction (move player/ball nets to CPU; court_detector
        # is reused by ROI and must stay) is a follow-up that needs a real GPU box to
        # validate — see docs/_investigation/t5_runtime_backlog.md D1.
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("D1: freed main-loop GPU cache before ROI sweep "
                            f"(reserved={torch.cuda.memory_reserved()//(1024*1024)}MB "
                            f"allocated={torch.cuda.memory_allocated()//(1024*1024)}MB)")
        except Exception as e:
            logger.warning(f"D1 phase-boundary cache free failed (non-fatal): {e}")

        # 2b+2c. ROI extraction — single shared video decode (Lever #1,
        # docs/_investigation/t5_pipeline_speed.md). Both post-pipeline ROI
        # passes are fanned off ONE sequential decode instead of one decode
        # each (pose) + one seek-decode per window (bounces). The video is now
        # decoded twice per job total (main per-frame loop + this sweep) rather
        # than ~3×, which is what raced long matches into the 6h Batch timeout.
        # Same models, same frames, same rows — only the decode scheduling
        # changed. Both passes depend on the FINAL bounce list so they can't be
        # folded into the main loop (pose's rally gate + the bounce windows are
        # only known after _postprocess). Failure of either pass is non-fatal
        # and isolated — additive coverage must not block downstream.
        #
        # Pose: ViTPose-Base on YOLOv8m-det crops of the 30-50 px far player
        #   → ml_analysis.player_detections_roi (source='far_vitpose'),
        #   consumed by serve_detector's merge logic on Render.
        # Bounces: TrackNet on tight service-box crops to recover bounces the
        #   full-frame pass misses (ball 1-2 px at 640x360) → canonical
        #   ml_analysis.ball_detections (source='roi_prod'). Anchor strategy is
        #   bounce-only without zone filter — best of 4 strategies on 880dff02
        #   (see bounces._select_anchors docstring).
        # 2d. Bronze BOUNCE MODEL (match only) — gravity-residual candidates +
        # trained CNN v2 → ml_analysis.ball_bounces (the MODEL-layer bounce fact
        # that replaces the velocity-reversal is_bounce rule per the bronze-first
        # cleanup). torch + weights live in THIS Batch image (the Render main API
        # has neither). Features are assembled from the IN-MEMORY result because
        # ml_analysis isn't populated until the Render re-ingest; rally state is
        # approximated in_rally (validated 2026-06-05: the rally gate barely moves
        # precision, 37%→34%). ball_bounces SURVIVES the re-ingest (it deletes only
        # ball/player_detections + match_analytics). Additive + non-fatal.
        #
        # ORDER (2026-06-06): runs BEFORE the ROI sweep so the sweep's rally
        # gate can consume the CNN events instead of the raw 20%-precision
        # is_bounce flags — phantom flags held IN_RALLY for 63% of frames and
        # starved far-pose ROI coverage (391 usable poses vs ~7,800 healthy).
        _cnn_bounce_ts = None
        _cnn_bounce_events = None
        # Shared by the bounce stage AND the serve-model stage below — built
        # outside the bounce try block so a bounce-stage failure can't
        # NameError the serve stage.
        _balls = [
            {"frame_idx": int(d.frame_idx), "x": d.x, "y": d.y,
             "court_x": d.court_x, "court_y": d.court_y,
             "is_bounce": d.is_bounce, "speed_kmh": d.speed_kmh}
            for d in (result.ball_detections or [])
        ] if not practice else []
        if not practice:
            try:
                import os as _os
                from ml_pipeline.bounce_detector.detector import (
                    detect_bounces_offline, _persist_events as _persist_bounces,
                )
                from ml_pipeline.bounce_detector.db import (
                    init_bounce_schema, delete_bounces_for_task,
                )
                from ml_pipeline.config import FRAME_SAMPLE_FPS as _BFPS
                _bw = _os.path.join(_os.path.dirname(__file__), "models",
                                    "bounce_detector_v2_7match.pt")
                _wrists: dict = {}
                for _pd in (result.player_detections or []):
                    if _pd.court_x is not None and _pd.court_y is not None:
                        _wrists.setdefault(int(_pd.frame_idx), []).append(
                            (float(_pd.court_x), float(_pd.court_y)))
                _last = max((b["frame_idx"] for b in _balls), default=0)
                _rally = {fi: "in_rally" for fi in range(_last + 1)}
                # Bounce CNN cutoff. Tuned 2026-06-14 via the offline corpus
                # threshold sweep (.claude/tmp/bounce_precision_sweep.py over the
                # 5 labelled corpus tasks): 0.5 -> 0.70 lifts precision 11%->23%
                # (2.1x) and erases the 1.88x over-emission (over_x ->0.78) for
                # only -2.5pp recall — and recall is training-gated anyway
                # (sharp-far retrain pending, memory far_roi_payoff_is_scorer_
                # training_gated). Env-configurable so future tuning is a Render/
                # job-def env flip, no Batch rebuild (env_var_rollback_pattern).
                _bthr = float(_os.environ.get("BOUNCE_CNN_THRESHOLD", "0.70"))
                _bev = detect_bounces_offline(
                    task_id=job_id, fps=float(_BFPS), ball_rows=_balls,
                    wrists_by_frame=_wrists, rally_by_frame=_rally,
                    weights_path=_bw, candidate_mode="gravity_residual",
                    threshold_override=_bthr,
                )
                # D2 (2026-06-07): fill NULL court coords by projecting the
                # ball's IMAGE position at the bounce frame. Ball court
                # coords were only ever computed for legacy is_bounce
                # frames (ball_tracker.detect_bounces), so 72% of CNN
                # events carried NULL — but at the bounce moment the ball
                # IS on the ground plane, which is exactly when the
                # homography projection is geometrically valid. Strict
                # bounds (to_court_coords default) keep wild projections
                # NULL — honest. Runs BEFORE persist + BEFORE the ROI
                # rally gate consumes the events, so the validated-
                # projected gate gains evidence density automatically.
                _court_det_b = getattr(pipeline, "court_detector", None)
                if _court_det_b is not None:
                    from ml_pipeline.bounce_detector.detector import (
                        _classify_player_side as _b_side,
                    )
                    _ball_xy = {int(b["frame_idx"]): (b["x"], b["y"])
                                for b in _balls
                                if b.get("x") is not None and b.get("y") is not None}
                    _filled = 0
                    for _e in _bev:
                        if _e.court_x is not None and _e.court_y is not None:
                            continue
                        _xy = _ball_xy.get(int(_e.frame_idx))
                        if not _xy:
                            continue
                        try:
                            _coords = _court_det_b.to_court_coords(_xy[0], _xy[1])
                        except Exception:
                            _coords = None
                        if _coords is not None:
                            _e.court_x = float(_coords[0])
                            _e.court_y = float(_coords[1])
                            _e.player_side = _b_side(_e.court_y)
                            _filled += 1
                    logger.info(
                        "Bounce CNN D2: projected court coords for %d "
                        "NULL-coord events (of %d total)", _filled, len(_bev))
                with engine.begin() as _bc:
                    init_bounce_schema(_bc)
                    delete_bounces_for_task(_bc, job_id)
                    _persist_bounces(_bc, _bev)
                _cnn_bounce_ts = sorted(float(e.ts) for e in _bev)
                # frame_idx + court coords for the ROI rally gate: it keeps
                # only VALIDATED PROJECTED bounces (NULL-coord pre-serve
                # ball-bouncing was holding IN_RALLY through far serve
                # wind-ups — 11/12 blocked on ea1e500c).
                _cnn_bounce_events = [
                    {"frame_idx": int(e.frame_idx),
                     "court_x": e.court_x, "court_y": e.court_y}
                    for e in _bev
                ]
                logger.info("Bounce CNN v2: wrote %d ball_bounces "
                            "(gravity_residual, thr 0.5, weights_loaded=%s)",
                            len(_bev), _os.path.exists(_bw))
            except Exception as e:
                logger.warning(f"Bounce CNN stage failed (non-fatal): {e}")

        far_ball_export_rows: list = []
        if not practice:
            try:
                # 81: must sit between the main pipeline's final stage
                # (computing_analytics=80, pipeline.py) and saving_results=82
                # below — it was 78, which made the UI progress bar visibly
                # step BACKWARDS (80→78) at the ROI phase boundary every run.
                on_progress("roi_extract", 81)
                from ml_pipeline.roi_extractors import run_unified_roi
                from ml_pipeline.config import FRAME_SAMPLE_FPS
                court_det = getattr(pipeline, "court_detector", None)
                # Pass the BRONZE sample rate (FRAME_SAMPLE_FPS), NOT the source
                # video fps: the ROI passes must index frames in the same 25fps
                # space as bronze/the main pipeline. run_unified_roi reads the
                # true source fps off the video itself to compute the decimation
                # stride. (Match path only — this block is `if not practice`.)
                n_pose, n_bounces, n_far_ball = run_unified_roi(
                    video_path=tmp_path,
                    job_id=job_id,
                    engine=engine,
                    fps=float(FRAME_SAMPLE_FPS),
                    court_detector=court_det,
                    bounces=getattr(result, "ball_detections", None),
                    pose_sample_every=2,
                    bounce_window_s=2.5,
                    bounce_cluster_gap_s=0.5,
                    bounce_anchor_zone_filter=False,
                    bounce_anchor_bounce_only=True,
                    cnn_bounce_ts=_cnn_bounce_ts,
                    cnn_bounce_events=_cnn_bounce_events,
                )
                logger.info(f"ROI unified: pose wrote {n_pose} rows, "
                            f"bounces wrote {n_bounces} rows, "
                            f"far_ball wrote {n_far_ball} rows")
                # Carry the roi_far_ball rows into the bronze export so they
                # survive the Render re-ingest's blanket DELETE+COPY (the
                # export+reingest-carry rule). far_ball.py already persisted
                # them; read them back for the payload.
                if n_far_ball:
                    with engine.connect() as _c:
                        far_ball_export_rows = [dict(r) for r in _c.execute(sql_text(
                            "SELECT frame_idx, x, y, court_x, court_y, speed_kmh, "
                            "is_bounce, is_in, source FROM ml_analysis.ball_detections "
                            "WHERE job_id = :jid AND source = 'roi_far_ball' "
                            "ORDER BY frame_idx"), {"jid": job_id}).mappings()]
            except Exception as e:
                logger.warning(f"ROI extraction failed (non-fatal): {e}")

        # 2e. SERVE MODEL stage (match only) — score far-serve candidate
        # anchors with the trained MLP → ml_analysis.serve_candidates (the
        # MODEL-layer serve fact, same pattern as the bounce stage above).
        # Runs AFTER the ROI sweep because the pose anchors + arm-raise
        # features consume player_detections_roi. Consumed Render-side by
        # serve_detector behind SERVE_MODEL_ENABLED; this stage just lands
        # the scored fact. Rollback: SERVE_MODEL_STAGE=0 (no rebuild).
        if not practice:
            try:
                import os as _os
                if _os.environ.get("SERVE_MODEL_STAGE", "1") != "0":
                    from sqlalchemy import text as _sqltext
                    from ml_pipeline.serve_model.infer import (
                        detect_serve_candidates_offline,
                    )
                    from ml_pipeline.serve_model.db import (
                        init_serve_candidates_schema,
                        delete_candidates_for_task, persist_candidates,
                    )
                    from ml_pipeline.config import FRAME_SAMPLE_FPS as _SFPS
                    _smw = _os.path.join(_os.path.dirname(__file__), "models",
                                         "serve_model_v1.pt")
                    # ROI rows were just written by the sweep — read back in
                    # the exact shape dataset.load_task_arrays trained on.
                    with engine.connect() as _sc:
                        _roi_raw = _sc.execute(_sqltext(
                            "SELECT frame_idx, keypoints, bbox_y1, bbox_y2 "
                            "FROM ml_analysis.player_detections_roi "
                            "WHERE job_id::text = :t ORDER BY frame_idx"
                        ), {"t": job_id}).fetchall()
                    _far_f = [int(p.frame_idx) for p in (result.player_detections or [])
                              if p.player_id == 1]
                    _near_f = [int(p.frame_idx) for p in (result.player_detections or [])
                               if p.player_id == 0]
                    _cands = detect_serve_candidates_offline(
                        task_id=job_id, fps=float(_SFPS),
                        ball_rows=_balls,
                        roi_rows_raw=_roi_raw,
                        far_pose_frames=_far_f, near_pose_frames=_near_f,
                        weights_path=_smw,
                    )
                    with engine.begin() as _smc:
                        init_serve_candidates_schema(_smc)
                        delete_candidates_for_task(_smc, job_id)
                        _n = persist_candidates(_smc, job_id, _cands)
                    logger.info("Serve model stage: wrote %d serve_candidates", _n)
            except Exception as e:
                logger.warning(f"Serve model stage failed (non-fatal): {e}")

        # 3. Export results to S3 as gzipped JSON (fast — single PUT)
        # The Render-side ingest worker (ml_pipeline.bronze_ingest_t5) downloads
        # and bulk-inserts into ml_analysis.* in the same region as the DB.
        on_progress("saving_results", 82)
        db.save_job_metadata(job_id, result)

        # Extract task_id from job row if present
        with engine.begin() as conn:
            row = conn.execute(sql_text(
                "SELECT task_id FROM ml_analysis.video_analysis_jobs WHERE job_id = :jid"
            ), {"jid": job_id}).fetchone()
            task_id = row[0] if row else None

        bronze_s3_key = export_bronze_to_s3(
            job_id=job_id,
            task_id=task_id,
            result=result,
            s3_client=s3,
            s3_bucket=s3_bucket,
            practice=practice,
            extra_ball_rows=far_ball_export_rows,
        )
        # Record the S3 key on the job row so the ingest worker can find it
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET bronze_s3_key = :bkey, updated_at = now()
                WHERE job_id = :jid
            """), {"jid": job_id, "bkey": bronze_s3_key})

        # 4. Generate and upload heatmaps
        on_progress("generating_heatmaps", 88)
        heatmaps = generate_all_heatmaps(result)
        ball_heatmap_key = None
        player_heatmap_keys = {}

        for filename, png_bytes in heatmaps.items():
            s3_heatmap_key = f"analysis/{job_id}/{filename}"
            s3.put_object(
                Bucket=s3_bucket,
                Key=s3_heatmap_key,
                Body=png_bytes,
                ContentType="image/png",
            )
            logger.info(f"Uploaded heatmap: s3://{s3_bucket}/{s3_heatmap_key}")

            if filename == "ball_heatmap.png":
                ball_heatmap_key = s3_heatmap_key
            else:
                player_heatmap_keys[filename] = s3_heatmap_key

        db.save_heatmap_keys(job_id, ball_heatmap_key, player_heatmap_keys)

        # 4b. Upload debug frames (YOLO bbox overlays) for visual inspection
        debug_dir = "/tmp/debug_frames"
        if os.path.exists(debug_dir):
            try:
                debug_files = sorted(os.listdir(debug_dir))
                logger.info(f"Uploading {len(debug_files)} debug frames to S3")
                for fname in debug_files:
                    local_path = os.path.join(debug_dir, fname)
                    if not fname.endswith(".jpg"):
                        continue
                    s3_debug_key = f"debug/{job_id}/{fname}"
                    try:
                        s3.upload_file(
                            local_path, s3_bucket, s3_debug_key,
                            ExtraArgs={"ContentType": "image/jpeg"},
                        )
                    except Exception as e:
                        logger.warning(f"Debug frame upload failed {fname}: {e}")
                    # Clean up local file
                    try:
                        os.unlink(local_path)
                    except Exception:
                        pass
                logger.info(f"Debug frames uploaded to s3://{s3_bucket}/debug/{job_id}/")
            except Exception as e:
                logger.warning(f"Debug frame upload step failed (non-fatal): {e}")

        # 5. Transcode to MP4 + upload to trimmed/{job_id}/practice.mp4
        on_progress("transcoding", 92)
        mp4_path = None
        try:
            mp4_path = _transcode_to_mp4(tmp_path)
            trimmed_key = f"trimmed/{job_id}/practice.mp4"
            s3.upload_file(mp4_path, s3_bucket, trimmed_key,
                           ExtraArgs={"ContentType": "video/mp4"})
            logger.info(f"Uploaded trimmed: s3://{s3_bucket}/{trimmed_key}")

            # (trimmed key is recorded in submission_context below — no need to
            # duplicate it on video_analysis_jobs)

            # Also update submission_context so Locker Room can find the footage
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET trim_status = 'completed',
                        trim_output_s3_key = :tkey
                    WHERE task_id = :jid
                """), {"jid": job_id, "tkey": trimmed_key})

        except Exception as e:
            logger.warning(f"Transcode failed (non-fatal): {e}")
        finally:
            if mp4_path and os.path.exists(mp4_path):
                os.unlink(mp4_path)

        # 6. Delete raw source from S3 (MOV cleanup)
        try:
            s3.delete_object(Bucket=s3_bucket, Key=s3_key)
            logger.info(f"Deleted raw source: s3://{s3_bucket}/{s3_key}")
        except Exception as e:
            logger.warning(f"Source cleanup failed (non-fatal): {e}")

        # 7. Record cost and mark complete
        batch_duration = time.time() - batch_start
        # G4dn.xlarge spot ≈ $0.1578/hr
        estimated_cost = (batch_duration / 3600) * 0.1578
        db.save_batch_cost(
            job_id, batch_job_id, batch_duration, estimated_cost,
            batch_job_arn=batch_job_arn,
        )
        on_progress("complete", 100)
        logger.info(f"Job {job_id} complete in {batch_duration:.0f}s (est. ${estimated_cost:.4f})")

    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        db.mark_failed(job_id, str(e))
        sys.exit(1)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            logger.info(f"Cleaned up temp file: {tmp_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tennis ML Analysis Pipeline")
    parser.add_argument("video_path", nargs="?", help="Local video file path")
    parser.add_argument("--job-id", help="ML analysis job ID (AWS Batch mode)")
    parser.add_argument("--s3-key", help="S3 object key of the video (AWS Batch mode)")
    parser.add_argument("--practice", action="store_true",
                        help="Practice mode: lower FPS + less frequent detection for faster processing")

    args = parser.parse_args()

    if args.job_id and args.s3_key:
        _run_batch(args.job_id, args.s3_key, practice=args.practice)
    elif args.video_path:
        _run_local(args.video_path, practice=args.practice)
    else:
        parser.print_help()
        sys.exit(1)
