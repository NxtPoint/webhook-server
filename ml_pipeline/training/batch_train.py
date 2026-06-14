"""Unified per-fact GPU training entrypoint for the T5 ML pipeline.

ONE command trains any of the trainable facts end-to-end on GPU:
build/refresh dataset (if needed) -> train -> save weights -> upload to S3.

This module is the ENTRYPOINT of the training Docker image
(`ml_pipeline/Dockerfile.train`) and is ALSO runnable locally / on the GPU
dev box for the exact same behaviour:

    # On a GPU box (or locally with a working CUDA torch):
    python -m ml_pipeline.training.batch_train --fact serve  --epochs 200
    python -m ml_pipeline.training.batch_train --fact hit    --epochs 200
    python -m ml_pipeline.training.batch_train --fact bounce --epochs 50
    python -m ml_pipeline.training.batch_train --fact swing  --epochs 50

    # On AWS Batch GPU (the seamless path) — submit via:
    python -m ml_pipeline.training.submit_train_job --fact swing

The five facts and their trainers (see .claude/training_environment.md):
  serve   coordinate MLP   ml_pipeline.serve_model.train             (reads prod DB)
  hit     coordinate MLP   ml_pipeline.hit_model.train               (reads prod DB)
  bounce  coordinate 1D-CNN ml_pipeline.training.train_bounce_detector (reads prod DB)
  swing   R(2+1)D-18 flow  ml_pipeline.training.train_swing_type     (GPU-hungry; needs
                                                                       a built dataset)
  identity  -- NO TRAINER (rule-based v1; OSNet v2 has no label_kind yet)

For `swing` the optical-flow dataset is built first (from corpus JSONs + the
720p trimmed videos in S3) unless --skip-dataset is passed and a dataset dir
already exists.

WEIGHTS SYNC: after a successful train, the produced weights file under
ml_pipeline/models/ is uploaded to
  s3://nextpoint-prod-uploads/training/weights/<fact>/<filename>
plus a `_latest` copy, and (for Batch) a metadata sidecar. The next session
pulls them down with `submit_train_job.py --download` (or a plain `aws s3 cp`)
into ml_pipeline/models/ (git-ignored) and the detector picks them up on next
Batch rebuild. See feedback_model_inference_belongs_in_batch_not_render.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("batch_train")

S3_BUCKET = os.environ.get("S3_BUCKET", "nextpoint-prod-uploads")
S3_WEIGHTS_PREFIX = "training/weights"

# Repo root models dir — every trainer writes here.
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# Per-fact output weight filenames (match each trainer's WEIGHTS constant).
WEIGHT_FILES = {
    "serve": "serve_model_v1.pt",
    "hit": "hit_model_v1.pt",
    "bounce": "bounce_detector_v2_7match.pt",
    "swing": "swing_classifier_v2.pt",
}


# ---------------------------------------------------------------------------
# GPU / env preflight
# ---------------------------------------------------------------------------

def _preflight(require_gpu: bool) -> str:
    import torch
    cuda = torch.cuda.is_available()
    dev = "cuda" if cuda else "cpu"
    name = torch.cuda.get_device_name(0) if cuda else "n/a"
    logger.info("torch=%s cuda_available=%s device=%s gpu=%s",
                torch.__version__, cuda, dev, name)
    if require_gpu and not cuda:
        raise SystemExit(
            "GPU required but torch.cuda.is_available() is False. "
            "Run this on the Batch training job-def or the GPU dev box, "
            "not the CPU dev box (the swing R(2+1)D model is GPU-bound)."
        )
    return dev


# ---------------------------------------------------------------------------
# Per-fact trainers
# ---------------------------------------------------------------------------

def _train_serve(args, device: str) -> str:
    from ml_pipeline.serve_model.train import main as serve_main
    argv_backup = sys.argv
    sys.argv = ["serve_train", "--epochs", str(args.epochs)]
    try:
        serve_main()
    finally:
        sys.argv = argv_backup
    return WEIGHT_FILES["serve"]


def _train_hit(args, device: str) -> str:
    from ml_pipeline.hit_model.train import main as hit_main
    argv_backup = sys.argv
    sys.argv = ["hit_train", "--epochs", str(args.epochs), "--batch", str(args.batch_size)]
    try:
        hit_main()
    finally:
        sys.argv = argv_backup
    return WEIGHT_FILES["hit"]


def _train_bounce(args, device: str) -> str:
    from ml_pipeline.training.train_bounce_detector import train as bounce_train
    out = str(MODELS_DIR / WEIGHT_FILES["bounce"])
    bounce_train(
        output_weights=out,
        task_filter=args.tasks,
        epochs=args.epochs,
        batch_size=args.batch_size,
        device=device,
        candidate_mode=args.candidate_mode,
        metric=args.metric,
    )
    return WEIGHT_FILES["bounce"]


def _train_swing(args, device: str) -> str:
    from ml_pipeline.training.build_swing_type_dataset import build_dataset
    from ml_pipeline.training.train_swing_type import train as swing_train

    dataset_dir = args.dataset_dir or str(
        Path(__file__).resolve().parent / "datasets" / "swing_type_v1"
    )
    cache_dir = args.cache_dir or str(
        Path(__file__).resolve().parent / "_dataset_cache"
    )
    manifest_path = Path(dataset_dir) / "manifest.json"

    if args.skip_dataset and manifest_path.exists():
        logger.info("swing: reusing existing dataset at %s (--skip-dataset)", dataset_dir)
    else:
        logger.info("swing: building optical-flow dataset -> %s (relabel=%s)",
                    dataset_dir, args.relabel)
        t0 = time.time()
        manifest = build_dataset(
            output_dir=dataset_dir,
            cache_dir=cache_dir,
            t5_filter=args.t5,
            relabel=args.relabel,
        )
        logger.info("swing: dataset built in %.0fs — %d matches / %d hits / by_class=%s",
                    time.time() - t0, manifest["n_matches"],
                    manifest["total_hits"], manifest["totals_by_class"])

    out = str(MODELS_DIR / WEIGHT_FILES["swing"])
    swing_train(
        dataset_dir=dataset_dir,
        output_weights=out,
        epochs=args.epochs,
        batch_size=args.batch_size,
        focal=args.focal,
        device=device,
    )
    return WEIGHT_FILES["swing"]


TRAINERS = {
    "serve": (_train_serve, False),   # (fn, require_gpu) — coord MLPs run fine on CPU too
    "hit": (_train_hit, False),
    "bounce": (_train_bounce, False),
    "swing": (_train_swing, True),    # R(2+1)D — GPU required for sane wall time
}


# ---------------------------------------------------------------------------
# Weights -> S3
# ---------------------------------------------------------------------------

def _upload_weights(fact: str, filename: str, run_id: str) -> None:
    local = MODELS_DIR / filename
    if not local.exists():
        logger.warning("weights file %s not produced — trainer may not have improved "
                       "on the previous checkpoint (nothing to upload).", local)
        return
    import boto3
    s3 = boto3.client("s3")
    sized = local.stat().st_size
    # Versioned copy (keeps history) + a _latest pointer for the puller.
    versioned = f"{S3_WEIGHTS_PREFIX}/{fact}/{run_id}/{filename}"
    latest = f"{S3_WEIGHTS_PREFIX}/{fact}/_latest/{filename}"
    for key in (versioned, latest):
        s3.upload_file(str(local), S3_BUCKET, key)
        logger.info("uploaded weights (%.1f MB) -> s3://%s/%s", sized / 1e6, S3_BUCKET, key)

    # Sidecar with the trainer's meta (if torch-saved as {'state_dict','meta'}).
    try:
        import torch
        blob = torch.load(str(local), map_location="cpu")
        meta = blob.get("meta") if isinstance(blob, dict) else None
        if meta is not None:
            sidecar = json.dumps({"fact": fact, "run_id": run_id, "meta": meta},
                                 indent=2, default=str)
            for key in (f"{S3_WEIGHTS_PREFIX}/{fact}/{run_id}/meta.json",
                        f"{S3_WEIGHTS_PREFIX}/{fact}/_latest/meta.json"):
                s3.put_object(Bucket=S3_BUCKET, Key=key, Body=sidecar.encode())
            logger.info("uploaded meta sidecar -> s3://%s/%s/%s/meta.json",
                        S3_BUCKET, S3_WEIGHTS_PREFIX, f"{fact}/{run_id}")
    except Exception as e:  # noqa: BLE001 — meta upload is best-effort
        logger.info("meta sidecar skipped (%s)", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fact", required=True, choices=sorted(TRAINERS),
                    help="Which fact to train.")
    ap.add_argument("--epochs", type=int, default=None,
                    help="Override epochs (defaults per fact).")
    ap.add_argument("--batch-size", type=int, default=None)
    ap.add_argument("--metric", choices=("f1", "pr_auc"), default="f1",
                    help="bounce: early-stop metric.")
    ap.add_argument("--candidate-mode", choices=("is_bounce", "gravity_residual"),
                    default="gravity_residual",
                    help="bounce: candidate pool — MUST match the deployed "
                         "BOUNCE_CANDIDATE_MODE for train/inference parity "
                         "(prod runtime = gravity_residual).")
    ap.add_argument("--task", action="append", default=None, dest="tasks",
                    help="bounce: restrict training to specific T5 task_id(s).")
    ap.add_argument("--dataset-dir", default=None,
                    help="swing: dataset output/read dir.")
    ap.add_argument("--cache-dir", default=None,
                    help="swing: local cache for downloaded labels/videos.")
    ap.add_argument("--t5", default=None,
                    help="swing: build dataset for only this one t5_task_id (smoke).")
    ap.add_argument("--relabel", action="store_true", default=True,
                    help="swing: relabel 4-class from bronze.player_swing "
                         "(default ON — the S3 label JSONs are stale 3-class).")
    ap.add_argument("--no-relabel", dest="relabel", action="store_false")
    ap.add_argument("--skip-dataset", action="store_true",
                    help="swing: reuse an existing dataset dir, skip the rebuild.")
    ap.add_argument("--focal", action="store_true",
                    help="swing: focal loss instead of CE+label-smoothing.")
    ap.add_argument("--no-upload", action="store_true",
                    help="Skip the S3 weights upload (local-only run).")
    ap.add_argument("--require-gpu", action="store_true",
                    help="Hard-fail if CUDA is unavailable (always true for swing).")
    args = ap.parse_args(argv)

    # Per-fact epoch defaults if not overridden.
    if args.epochs is None:
        args.epochs = {"serve": 200, "hit": 200, "bounce": 50, "swing": 50}[args.fact]
    if args.batch_size is None:
        args.batch_size = {"serve": 256, "hit": 512, "bounce": 16, "swing": 16}[args.fact]

    fn, require_gpu = TRAINERS[args.fact]
    device = _preflight(require_gpu or args.require_gpu)

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    logger.info("=== TRAIN fact=%s run_id=%s epochs=%d batch=%d device=%s ===",
                args.fact, run_id, args.epochs, args.batch_size, device)

    t0 = time.time()
    filename = fn(args, device)
    logger.info("=== TRAIN done fact=%s in %.0fs ===", args.fact, time.time() - t0)

    if args.no_upload:
        logger.info("weights upload skipped (--no-upload). Local: %s",
                    MODELS_DIR / filename)
    else:
        _upload_weights(args.fact, filename, run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
