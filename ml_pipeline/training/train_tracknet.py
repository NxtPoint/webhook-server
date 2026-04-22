"""
ml_pipeline/training/train_tracknet.py — Fine-tune TrackNet V2 on our footage.

Strategy:
  - Load pretrained BallTrackerNet from ml_pipeline/ball_tracker.py
  - Freeze encoder layers (conv1–conv10 + pooling) — preserve low-level features
  - Train decoder only (conv11–conv18) — adapt to our specific camera angle / ball size
  - Loss: weighted BCELoss — ball pixels are ~1 in 10,000, so positive class weight
    is set to ~100× to compensate
  - Optimizer: Adam with lr=1e-4, weight_decay=1e-5
  - Validation split: 80/20 (random, seeded for reproducibility)
  - Best model saved by validation loss to ml_pipeline/models/tracknet_v2_finetuned.pt

Usage:
    python -m ml_pipeline.training.train_tracknet \\
        --frames-dir ./frames \\
        --labels ./labels.json \\
        --epochs 20 \\
        --batch-size 4

Output:
    - ml_pipeline/models/tracknet_v2_finetuned.pt  (best checkpoint)
    - Epoch-by-epoch log: epoch, train_loss, val_loss, val_precision, val_recall
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, Tuple

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, random_split

logger = logging.getLogger(__name__)

# ── Paths (relative to this file's package root) ────────────────────────────

_ML_PIPELINE_DIR = Path(__file__).parent.parent
_MODELS_DIR = _ML_PIPELINE_DIR / "models"
_DEFAULT_WEIGHTS = _MODELS_DIR / "tracknet_v2.pt"
_FINETUNED_WEIGHTS = _MODELS_DIR / "tracknet_v2_finetuned.pt"

# ── Training hyperparameters ─────────────────────────────────────────────────

_DEFAULT_EPOCHS = 20
_DEFAULT_BATCH_SIZE = 4
_DEFAULT_LR = 1e-4
_DEFAULT_WEIGHT_DECAY = 1e-5
_POSITIVE_CLASS_WEIGHT = 100.0   # Ball pixels are ~1/10000 — weight heavily
_PRECISION_RECALL_THRESHOLD = 0.5  # Binarise heatmap predictions at this value
_VAL_SPLIT = 0.2                 # 20% held out for validation
_RANDOM_SEED = 42


# ============================================================
# Encoder layer names (freeze these)
# ============================================================

_ENCODER_LAYERS = [
    "conv1", "conv2", "pool1",
    "conv3", "conv4", "pool2",
    "conv5", "conv6", "conv7", "pool3",
    "conv8", "conv9", "conv10",
]


def _freeze_encoder(model: nn.Module) -> int:
    """Freeze all encoder parameters. Returns count of frozen params."""
    frozen = 0
    for name, param in model.named_parameters():
        # name format: "conv1.block.0.weight" — match on first segment
        layer = name.split(".")[0]
        if layer in _ENCODER_LAYERS:
            param.requires_grad = False
            frozen += 1
    return frozen


def _count_trainable(model: nn.Module) -> Tuple[int, int]:
    """Return (trainable_params, total_params)."""
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return trainable, total


# ============================================================
# Metrics
# ============================================================

def _precision_recall(
    preds: torch.Tensor,
    targets: torch.Tensor,
    threshold: float = _PRECISION_RECALL_THRESHOLD,
) -> Tuple[float, float]:
    """Compute binary precision and recall from raw heatmap logits.

    Args:
        preds:   (N, H, W) sigmoid-activated predictions in [0, 1]
        targets: (N, H, W) ground truth heatmaps in [0, 1]
        threshold: binarisation threshold

    Returns:
        (precision, recall) — both 0.0 if no positive predictions/targets
    """
    pred_bin = (preds >= threshold).float()
    tgt_bin = (targets >= threshold).float()

    tp = (pred_bin * tgt_bin).sum().item()
    fp = (pred_bin * (1 - tgt_bin)).sum().item()
    fn = ((1 - pred_bin) * tgt_bin).sum().item()

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall


# ============================================================
# Training loop
# ============================================================

def train(
    frames_dir: str,
    labels_json: str,
    epochs: int = _DEFAULT_EPOCHS,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    lr: float = _DEFAULT_LR,
    weight_decay: float = _DEFAULT_WEIGHT_DECAY,
    weights_path: str = None,
    output_path: str = None,
    device: str = None,
) -> Dict[str, float]:
    """
    Fine-tune BallTrackerNet (V2) on the provided frames and labels.

    Args:
        frames_dir:   Directory of frame_*.jpg files.
        labels_json:  JSON file with ball positions (see TrackNetDataset docs).
        epochs:       Number of training epochs.
        batch_size:   Batch size for DataLoader.
        lr:           Adam learning rate.
        weight_decay: Adam weight decay.
        weights_path: Path to pretrained weights. Defaults to tracknet_v2.pt.
        output_path:  Save path for best model. Defaults to tracknet_v2_finetuned.pt.
        device:       'cuda', 'cpu', or None for auto-detect.

    Returns:
        Dict with final metrics: train_loss, val_loss, val_precision, val_recall.
    """
    from ml_pipeline.ball_tracker import BallTrackerNet
    from ml_pipeline.training.tracknet_dataset import TrackNetDataset

    weights_path = weights_path or str(_DEFAULT_WEIGHTS)
    output_path = output_path or str(_FINETUNED_WEIGHTS)

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Training device: %s", device)

    # ── Dataset + splits ────────────────────────────────────────────────────
    logger.info("Loading dataset: frames_dir=%s  labels=%s", frames_dir, labels_json)
    # skip_no_label_middle=False: build_serve_bounce_dataset extracts frames
    # as [bounce-(N-1), ..., bounce] per the TrackNet V2 convention (label
    # on LAST frame of window, model predicts ball at t given frames [t-2,
    # t-1, t]). The middle frame is never labeled — dropping samples
    # whose middle is unlabeled would discard every single training
    # sample. The last-frame-label contract still holds regardless.
    full_dataset = TrackNetDataset(frames_dir, labels_json,
                                    skip_no_label_middle=False)
    stats = full_dataset.label_stats()
    logger.info(
        "Dataset: total=%d  with_ball=%d  without_ball=%d",
        stats["total"], stats["with_ball"], stats["without_ball"],
    )

    n_total = len(full_dataset)
    if n_total == 0:
        raise ValueError("Dataset is empty — check frames_dir and labels_json")

    n_val = max(1, int(n_total * _VAL_SPLIT))
    n_train = n_total - n_val
    logger.info("Split: train=%d  val=%d", n_train, n_val)

    generator = torch.Generator().manual_seed(_RANDOM_SEED)
    train_ds, val_ds = random_split(full_dataset, [n_train, n_val], generator=generator)

    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=True,
        num_workers=0,   # Safe default — avoids multiprocessing issues on Windows/Colab
        pin_memory=(device == "cuda"),
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=0,
        pin_memory=(device == "cuda"),
    )

    # ── Model ───────────────────────────────────────────────────────────────
    logger.info("Loading pretrained weights from %s", weights_path)
    if not Path(weights_path).exists():
        raise FileNotFoundError(f"Pretrained weights not found: {weights_path}")

    model = BallTrackerNet(in_channels=9)
    state = torch.load(weights_path, map_location=device, weights_only=True)
    model.load_state_dict(state)
    model.to(device)

    frozen = _freeze_encoder(model)
    trainable, total = _count_trainable(model)
    logger.info(
        "Frozen %d encoder param tensors. Trainable params: %d / %d (%.1f%%)",
        frozen, trainable, total, 100 * trainable / total if total else 0,
    )

    # ── Loss ────────────────────────────────────────────────────────────────
    # BCEWithLogitsLoss handles the sigmoid internally (numerically stable).
    # pos_weight amplifies the gradient from ball pixels (~1 in 10,000).
    pos_weight = torch.tensor([_POSITIVE_CLASS_WEIGHT], device=device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    # ── Optimiser ───────────────────────────────────────────────────────────
    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=lr,
        weight_decay=weight_decay,
    )

    # ── Training loop ────────────────────────────────────────────────────────
    best_val_loss = float("inf")
    best_epoch = 0
    final_metrics: Dict[str, float] = {}

    header = (
        f"{'epoch':>6} {'train_loss':>12} {'val_loss':>12} "
        f"{'val_prec':>10} {'val_rec':>10} {'time':>7}"
    )
    print()
    print(header)
    print("-" * len(header))

    for epoch in range(1, epochs + 1):
        t0 = time.time()

        # ── Train ──
        model.train()
        train_loss_sum = 0.0
        train_batches = 0

        for frames, heatmaps in train_loader:
            frames = frames.to(device)    # (B, 9, H, W)
            heatmaps = heatmaps.to(device)  # (B, H, W)

            optimizer.zero_grad()

            # Forward — use raw logits (no softmax/sigmoid) for BCEWithLogitsLoss
            logits = model(frames, testing=False)  # (B, 256, H*W) flattened

            # The model outputs (B, out_channels, H*W) — we only need channel 0
            # reshaped back to (B, H, W) to compare with our Gaussian heatmap.
            # TrackNet V2 uses a 256-way softmax over spatial positions; for
            # fine-tuning we treat the raw logit at position 0 (or aggregate)
            # as a single-channel heatmap prediction.
            #
            # Simpler approach: reshape logits to (B, 256, H, W), take the
            # max-responding channel's spatial map as our prediction.
            # We use channel 0 (background vs ball) as a binary prediction proxy.
            B, C, HW = logits.shape
            H = full_dataset.input_h if hasattr(full_dataset, "input_h") else 360
            W = full_dataset.input_w if hasattr(full_dataset, "input_w") else 640

            # Reshape to spatial: (B, C, H, W)
            logits_spatial = logits.view(B, C, H, W)

            # Use the max across channels as the ball presence logit at each pixel.
            # This is equivalent to asking "does any class predict ball here?"
            pred_logits, _ = logits_spatial.max(dim=1)   # (B, H, W)

            loss = criterion(pred_logits, heatmaps)
            loss.backward()
            optimizer.step()

            train_loss_sum += loss.item()
            train_batches += 1

        train_loss = train_loss_sum / train_batches if train_batches > 0 else 0.0

        # ── Validate ──
        model.eval()
        val_loss_sum = 0.0
        val_batches = 0
        all_preds = []
        all_targets = []

        with torch.no_grad():
            for frames, heatmaps in val_loader:
                frames = frames.to(device)
                heatmaps = heatmaps.to(device)

                logits = model(frames, testing=False)
                B, C, HW = logits.shape
                logits_spatial = logits.view(B, C, H, W)
                pred_logits, _ = logits_spatial.max(dim=1)

                loss = criterion(pred_logits, heatmaps)
                val_loss_sum += loss.item()
                val_batches += 1

                # Collect sigmoid-activated predictions for precision/recall
                preds_sigmoid = torch.sigmoid(pred_logits).cpu()
                all_preds.append(preds_sigmoid)
                all_targets.append(heatmaps.cpu())

        val_loss = val_loss_sum / val_batches if val_batches > 0 else 0.0
        all_preds_t = torch.cat(all_preds, dim=0)
        all_targets_t = torch.cat(all_targets, dim=0)
        val_precision, val_recall = _precision_recall(all_preds_t, all_targets_t)

        elapsed = time.time() - t0
        print(
            f"{epoch:>6} {train_loss:>12.6f} {val_loss:>12.6f} "
            f"{val_precision:>10.4f} {val_recall:>10.4f} {elapsed:>6.1f}s"
        )

        # Save best model
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_epoch = epoch
            out_path = Path(output_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(model.state_dict(), out_path)
            logger.info("Saved best model (epoch %d val_loss=%.6f) -> %s", epoch, val_loss, output_path)

        final_metrics = {
            "epoch": epoch,
            "train_loss": round(train_loss, 6),
            "val_loss": round(val_loss, 6),
            "val_precision": round(val_precision, 4),
            "val_recall": round(val_recall, 4),
        }

    print()
    print(f"Training complete. Best epoch: {best_epoch}  best_val_loss: {best_val_loss:.6f}")
    print(f"Fine-tuned model saved to: {output_path}")

    # ── Record in eval store ─────────────────────────────────────────────────
    try:
        from ml_pipeline.eval_store import record_component_eval
        record_component_eval(
            task_id="training",
            component="tracknet_finetune",
            passed=best_val_loss < 1.0,   # Heuristic: loss < 1.0 considered passing
            metrics={
                "best_epoch": best_epoch,
                "best_val_loss": round(best_val_loss, 6),
                "final_train_loss": final_metrics.get("train_loss"),
                "final_val_precision": final_metrics.get("val_precision"),
                "final_val_recall": final_metrics.get("val_recall"),
                "total_epochs": epochs,
                "n_train_samples": n_train,
                "n_val_samples": n_val,
                "output_path": output_path,
            },
        )
        logger.info("Eval store: recorded training metrics")
    except Exception as exc:
        logger.warning("Could not record to eval store: %s", exc)

    return final_metrics


# ============================================================
# CLI entry point
# ============================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(
        prog="ml_pipeline.training.train_tracknet",
        description="Fine-tune TrackNet V2 on our footage using SportAI ground truth labels",
    )
    p.add_argument("--frames-dir", required=True, help="Directory of frame_*.jpg files")
    p.add_argument("--labels", required=True, help="Path to labels JSON file")
    p.add_argument("--epochs", type=int, default=_DEFAULT_EPOCHS, help=f"Training epochs (default {_DEFAULT_EPOCHS})")
    p.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE, help=f"Batch size (default {_DEFAULT_BATCH_SIZE})")
    p.add_argument("--lr", type=float, default=_DEFAULT_LR, help=f"Learning rate (default {_DEFAULT_LR})")
    p.add_argument("--weight-decay", type=float, default=_DEFAULT_WEIGHT_DECAY, help=f"Weight decay (default {_DEFAULT_WEIGHT_DECAY})")
    p.add_argument("--weights", default=None, help="Pretrained weights path (default: tracknet_v2.pt)")
    p.add_argument("--output", default=None, help="Output path for fine-tuned model (default: tracknet_v2_finetuned.pt)")
    p.add_argument("--device", default=None, choices=["cuda", "cpu"], help="Device (default: auto)")

    args = p.parse_args()

    try:
        metrics = train(
            frames_dir=args.frames_dir,
            labels_json=args.labels,
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            weight_decay=args.weight_decay,
            weights_path=args.weights,
            output_path=args.output,
            device=args.device,
        )
        print(f"\nFinal metrics: {metrics}")
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
