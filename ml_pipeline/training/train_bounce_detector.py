"""ADR-01 v1 bounce_detector training loop.

Reads the manifest produced by `ml_pipeline.bounce_detector.dataset.build_manifest`
(positives + negatives mined from `ml_analysis.training_corpus` floor labels)
and trains the 1D temporal CNN from `ml_pipeline.bounce_detector.cnn.build_model`
per the recipe in ADR-01 §"Build spec v1":

  - AdamW, lr=1e-3, cosine decay, weight_decay=1e-4, 5-epoch warmup
  - BCE loss on sigmoid output (binary: bounce vs not-bounce)
  - WeightedRandomSampler — balances batches against the ~5:1 negative ratio
  - Per-epoch val: loss, accuracy, precision, recall, F1 @ threshold=0.5,
    PR-AUC (threshold-agnostic)
  - Early-stop on val F1 (or PR-AUC via --metric) — patience 10

Saves to --output as a torch state_dict + meta sidecar dict (epochs,
best metric, per-class counts, hyperparameters, manifest summary).

CLI:
  python -m ml_pipeline.training.train_bounce_detector \
      --output ml_pipeline/models/bounce_detector_v1.pt \
      --epochs 50

  # smoke-test on just Match 1 (67 floor labels — only Rivonia match in corpus):
  python -m ml_pipeline.training.train_bounce_detector \
      --task 78c32f53-5580-4a88-a4e7-7506e59b2b52 \
      --epochs 5 \
      --output ml_pipeline/models/bounce_detector_smoke.pt

Loaded by the production detector via cnn.BounceCNNWrapper.load_weights(); see
ml_pipeline/bounce_detector/detector.py — once weights exist the STOPGAP
threshold flips from 1.1 to 0.55 automatically.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, WeightedRandomSampler

from ml_pipeline.bounce_detector.cnn import build_model
from ml_pipeline.bounce_detector.dataset import (
    BounceDataset,
    build_manifest,
    class_counts,
    train_val_split,
)

logger = logging.getLogger("train_bounce_detector")


# ---------------------------------------------------------------------------
# Helpers (sampler / lr schedule / metrics)
# ---------------------------------------------------------------------------

def _build_sampler(manifest: list[dict]) -> WeightedRandomSampler:
    """Inverse-frequency weights → each batch is roughly class-balanced.

    For 67 positives + ~335 negatives → weight_pos ≈ 3, weight_neg ≈ 0.6
    so each sample gets drawn at ~50/50 expected ratio.
    """
    counts = class_counts(manifest)
    total = sum(counts.values())
    n_classes = sum(1 for c in counts.values() if c > 0) or 1
    per_class_w = {
        c: (total / (n_classes * max(1, n))) for c, n in counts.items()
    }
    weights = [per_class_w[int(s["label"])] for s in manifest]
    return WeightedRandomSampler(
        weights=weights, num_samples=len(weights), replacement=True,
    )


def _cosine_with_warmup(epoch: int, total: int, warmup: int) -> float:
    """LR multiplier — linear warmup then cosine to zero."""
    if epoch < warmup:
        return (epoch + 1) / max(1, warmup)
    progress = (epoch - warmup) / max(1, total - warmup)
    return 0.5 * (1.0 + math.cos(math.pi * progress))


def _pr_auc(scores: list[float], labels: list[int]) -> float:
    """Compute PR-AUC via trapezoidal rule over the precision-recall curve.

    Independent of sklearn — fewer deps + identical numbers for n < 1000.
    Threshold-agnostic; the metric we actually care about for an
    imbalanced binary classifier.
    """
    if not scores or sum(labels) == 0:
        return 0.0
    # Sort by descending score
    pairs = sorted(zip(scores, labels), key=lambda x: -x[0])
    tp = fp = 0
    total_pos = sum(labels)
    prev_recall = 0.0
    auc = 0.0
    prev_prec = 1.0
    for score, lbl in pairs:
        if lbl == 1:
            tp += 1
        else:
            fp += 1
        prec = tp / (tp + fp)
        rec = tp / total_pos
        # Trapezoidal step
        auc += (rec - prev_recall) * (prec + prev_prec) / 2.0
        prev_recall = rec
        prev_prec = prec
    return float(auc)


def _binary_metrics(scores: list[float], labels: list[int],
                    threshold: float = 0.5) -> dict:
    """Accuracy, precision, recall, F1 @ a fixed threshold, plus PR-AUC."""
    if not scores:
        return {
            "n": 0, "n_pos": 0, "n_neg": 0,
            "accuracy": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "pr_auc": 0.0, "threshold": threshold,
        }
    preds = [1 if s >= threshold else 0 for s in scores]
    tp = sum(1 for p, l in zip(preds, labels) if p == 1 and l == 1)
    fp = sum(1 for p, l in zip(preds, labels) if p == 1 and l == 0)
    fn = sum(1 for p, l in zip(preds, labels) if p == 0 and l == 1)
    tn = sum(1 for p, l in zip(preds, labels) if p == 0 and l == 0)
    n_pos = tp + fn
    n_neg = tn + fp
    acc = (tp + tn) / max(1, len(labels))
    prec = tp / max(1, tp + fp)
    rec = tp / max(1, tp + fn)
    f1 = 2 * prec * rec / max(1e-9, prec + rec)
    pr_auc = _pr_auc(scores, labels)
    return {
        "n": len(labels), "n_pos": n_pos, "n_neg": n_neg,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy": acc, "precision": prec, "recall": rec, "f1": f1,
        "pr_auc": pr_auc, "threshold": threshold,
    }


# ---------------------------------------------------------------------------
# Eval pass
# ---------------------------------------------------------------------------

def _validate(model, loader, device, threshold: float = 0.5) -> dict:
    model.eval()
    scores: list[float] = []
    labels: list[int] = []
    losses: list[float] = []
    loss_fn = nn.BCELoss(reduction="mean")
    with torch.no_grad():
        for batch in loader:
            x = batch["features"].to(device)
            y = batch["label"].to(device).view(-1, 1)
            p = model(x).view(-1, 1)
            losses.append(float(loss_fn(p, y).item()))
            scores.extend(p.view(-1).cpu().tolist())
            labels.extend([int(v) for v in batch["label"].view(-1).tolist()])
    metrics = _binary_metrics(scores, labels, threshold=threshold)
    metrics["loss"] = (sum(losses) / max(1, len(losses)))
    return metrics


# ---------------------------------------------------------------------------
# Train
# ---------------------------------------------------------------------------

def train(
    *,
    output_weights: str,
    task_filter: list[str] | None = None,
    neg_per_pos: int = 5,
    val_frac: float = 0.2,
    epochs: int = 50,
    batch_size: int = 16,
    lr: float = 1e-3,
    weight_decay: float = 1e-4,
    warmup_epochs: int = 5,
    patience: int = 10,
    threshold: float = 0.5,
    metric: str = "f1",                     # "f1" or "pr_auc"
    device: str | None = None,
    seed: int = 42,
    engine=None,
    candidate_mode: str = "is_bounce",
) -> dict:
    """End-to-end: build manifest → split → train → save best weights.

    Returns a metadata dict with the best epoch + metrics.
    """
    if metric not in ("f1", "pr_auc"):
        raise ValueError(f"metric must be f1 or pr_auc; got {metric!r}")

    torch.manual_seed(seed)
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")

    if engine is None:
        from db_init import engine as default_engine
        engine = default_engine

    logger.info("building manifest (candidate_mode=%s, neg_per_pos=%d, task_filter=%s)…",
                candidate_mode, neg_per_pos, task_filter)
    manifest = build_manifest(
        engine=engine, task_filter=task_filter,
        neg_per_pos=neg_per_pos, seed=seed,
        candidate_mode=candidate_mode,
    )
    if not manifest:
        raise RuntimeError(
            "manifest is empty — no positives mined from training_corpus. "
            "Run dual-submit to land more corpus rows, or check that the "
            "task has type='floor' SA labels."
        )
    counts = class_counts(manifest)
    if counts.get(1, 0) == 0:
        raise RuntimeError(
            "manifest has 0 positives — every floor label failed the "
            "strong-positive gate (±5 frames + ≤50 px). Check audit recipe."
        )
    if counts.get(0, 0) == 0:
        raise RuntimeError(
            "manifest has 0 negatives — increase neg_per_pos or check "
            "is_bounce candidate pool."
        )

    train_m, val_m = train_val_split(manifest, val_frac=val_frac, seed=seed)
    train_ds = BounceDataset(train_m, engine=engine)
    val_ds = BounceDataset(val_m, engine=engine)
    logger.info(
        "split: train=%d (pos=%d neg=%d)  val=%d (pos=%d neg=%d)  device=%s",
        len(train_m),
        sum(1 for s in train_m if s["label"] == 1),
        sum(1 for s in train_m if s["label"] == 0),
        len(val_m),
        sum(1 for s in val_m if s["label"] == 1),
        sum(1 for s in val_m if s["label"] == 0),
        device,
    )

    sampler = _build_sampler(train_m)
    train_loader = DataLoader(
        train_ds, batch_size=batch_size, sampler=sampler, num_workers=0,
    )
    val_loader = DataLoader(
        val_ds, batch_size=batch_size, shuffle=False, num_workers=0,
    )

    model = build_model().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    loss_fn = nn.BCELoss(reduction="mean")

    best_metric = -1.0
    best_epoch = -1
    best_val: dict = {}
    no_improve = 0

    for epoch in range(epochs):
        lr_mult = _cosine_with_warmup(epoch, epochs, warmup_epochs)
        for g in opt.param_groups:
            g["lr"] = lr * lr_mult

        model.train()
        epoch_loss = 0.0
        n_batches = 0
        for batch in train_loader:
            x = batch["features"].to(device)
            y = batch["label"].to(device).view(-1, 1)
            p = model(x).view(-1, 1)
            loss = loss_fn(p, y)

            opt.zero_grad()
            loss.backward()
            opt.step()
            epoch_loss += float(loss.item())
            n_batches += 1

        train_loss = epoch_loss / max(1, n_batches)
        val_metrics = _validate(model, val_loader, device, threshold=threshold)
        logger.info(
            "epoch %02d/%02d lr_mult=%.3f train_loss=%.4f "
            "val_loss=%.4f acc=%.3f prec=%.3f rec=%.3f f1=%.3f pr_auc=%.3f "
            "(tp=%d fp=%d fn=%d tn=%d)",
            epoch + 1, epochs, lr_mult, train_loss,
            val_metrics["loss"], val_metrics["accuracy"], val_metrics["precision"],
            val_metrics["recall"], val_metrics["f1"], val_metrics["pr_auc"],
            val_metrics.get("tp", 0), val_metrics.get("fp", 0),
            val_metrics.get("fn", 0), val_metrics.get("tn", 0),
        )

        cur_metric = val_metrics[metric]
        if cur_metric > best_metric:
            best_metric = cur_metric
            best_epoch = epoch
            best_val = val_metrics
            no_improve = 0
            output_path = Path(output_weights)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({
                "state_dict": model.state_dict(),
                "meta": {
                    "best_epoch": best_epoch + 1,
                    "best_metric_name": metric,
                    "best_metric_value": best_metric,
                    "val": val_metrics,
                    "n_train": len(train_m),
                    "n_val": len(val_m),
                    "manifest_counts": counts,
                    "hyper": {
                        "lr": lr, "weight_decay": weight_decay,
                        "warmup_epochs": warmup_epochs, "epochs": epochs,
                        "batch_size": batch_size, "neg_per_pos": neg_per_pos,
                        "val_frac": val_frac, "threshold": threshold,
                    },
                    "task_filter": task_filter,
                    "candidate_mode": candidate_mode,
                },
            }, output_path)
            logger.info("  ↑ new best %s=%.4f — saved to %s",
                        metric, best_metric, output_path)
        else:
            no_improve += 1
            if no_improve >= patience:
                logger.info("early stop at epoch %d (patience=%d)",
                            epoch + 1, patience)
                break

    logger.info("BEST: epoch=%d %s=%.4f", best_epoch + 1, metric, best_metric)
    return {
        "best_epoch": best_epoch + 1,
        "best_metric_name": metric,
        "best_metric_value": best_metric,
        "best_val": best_val,
        "n_train": len(train_m),
        "n_val": len(val_m),
        "manifest_counts": counts,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True,
                    help="Path to write weights .pt (e.g. "
                         "ml_pipeline/models/bounce_detector_v1.pt)")
    ap.add_argument("--task", action="append", default=None, dest="tasks",
                    help="Restrict training to a specific T5 task_id "
                         "(can be passed multiple times)")
    ap.add_argument("--neg-per-pos", type=int, default=5)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--warmup-epochs", type=int, default=5)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="Eval threshold for precision/recall/F1")
    ap.add_argument("--metric", choices=("f1", "pr_auc"), default="f1",
                    help="Early-stop metric")
    ap.add_argument("--device", default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--candidate-mode", choices=("is_bounce", "gravity_residual"),
                    default="is_bounce",
                    help="Pool of candidates positives match against AND negatives "
                         "are sampled from. Must match detector.py runtime "
                         "BOUNCE_CANDIDATE_MODE for train/inference parity.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    result = train(
        output_weights=args.output,
        task_filter=args.tasks,
        neg_per_pos=args.neg_per_pos,
        val_frac=args.val_frac,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        weight_decay=args.weight_decay,
        warmup_epochs=args.warmup_epochs,
        patience=args.patience,
        threshold=args.threshold,
        metric=args.metric,
        device=args.device,
        seed=args.seed,
        candidate_mode=args.candidate_mode,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
