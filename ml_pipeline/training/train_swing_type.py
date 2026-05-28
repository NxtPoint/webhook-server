"""ADR-02 v2 swing-type classifier training loop.

Reads the dataset built by `build_swing_type_dataset.py`, trains
`SwingTypeR2plus1D` per the recipe in ADR-02 §"Training recipe":
  - AdamW, lr=1e-4, cosine decay, weight_decay=1e-4, 5-epoch warmup
  - Cross-entropy + label-smoothing ε=0.1 (or focal-loss γ=2 if --focal)
  - Horizontal flip w/ handedness toggle (in Dataset)
  - Temporal crop ±2 (in Dataset)
  - Mixup α=0.2 (here, on the batch)
  - WeightedRandomSampler oversamples the minority class
  - Early-stop on val macro-F1 (patience 10)

Saves weights to --output as a state_dict plus a metadata sidecar dict
(epochs trained, best val macro-F1, per-class precision/recall on val,
manifest version, builder version, etc.).

CLI:
  python -m ml_pipeline.training.train_swing_type \
      --dataset-dir ml_pipeline/training/datasets/swing_type_v1 \
      --output ml_pipeline/models/swing_classifier_v2.pt \
      --epochs 50

NOT runnable today on the 368-hit v1 corpus (will overfit -- ADR-02 spec
asks for ~2-3k labels). Wait for ~5-10 more matches to accumulate. This
file lives so that when the data is ready, training is one command away.
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
import torch.nn.functional as F
from torch.utils.data import DataLoader, WeightedRandomSampler

from ml_pipeline.stroke_classifier.dataset import SwingTypeDataset, class_counts
from ml_pipeline.stroke_classifier.model_v2 import (
    CLASSES, NUM_CLASSES, SwingTypeR2plus1D,
)

logger = logging.getLogger("train_swing_type")


def _build_sampler(dataset: SwingTypeDataset) -> WeightedRandomSampler:
    """Inverse-frequency per-class weights → each batch is roughly class-balanced.
    For our 368-hit corpus: 139 forehand / 75 backhand / 154 overhead → weights
    biased ~2x for backhand."""
    counts = class_counts(dataset)
    total = sum(counts.values())
    class_w = {c: total / (NUM_CLASSES * max(1, n)) for c, n in counts.items()}
    weights = []
    for m in dataset._matches:
        for st in m["labels_dict"]["swing_type"]:
            weights.append(class_w[st])
    return WeightedRandomSampler(weights=weights, num_samples=len(weights), replacement=True)


def _focal_loss(logits: torch.Tensor, target: torch.Tensor, gamma: float = 2.0) -> torch.Tensor:
    """Standard multi-class focal loss. `target` is class indices (B,)."""
    log_probs = F.log_softmax(logits, dim=1)
    pt = log_probs.gather(1, target.unsqueeze(1)).squeeze(1).exp()
    loss = -((1 - pt) ** gamma) * log_probs.gather(1, target.unsqueeze(1)).squeeze(1)
    return loss.mean()


def _mixup(batch_flow: torch.Tensor, batch_hand: torch.Tensor,
           batch_label: torch.Tensor, alpha: float = 0.2):
    """Apply mixup on a batch. Returns (mixed_flow, mixed_hand, target_a, target_b, lam)."""
    if alpha <= 0:
        return batch_flow, batch_hand, batch_label, batch_label, 1.0
    lam = float(torch.distributions.Beta(alpha, alpha).sample().item())
    idx = torch.randperm(batch_flow.size(0), device=batch_flow.device)
    mixed_flow = lam * batch_flow + (1 - lam) * batch_flow[idx]
    mixed_hand = lam * batch_hand + (1 - lam) * batch_hand[idx]
    return mixed_flow, mixed_hand, batch_label, batch_label[idx], lam


def _cosine_with_warmup(epoch: int, total: int, warmup: int) -> float:
    """LR multiplier — linear warmup then cosine to zero."""
    if epoch < warmup:
        return (epoch + 1) / max(1, warmup)
    progress = (epoch - warmup) / max(1, total - warmup)
    return 0.5 * (1.0 + math.cos(math.pi * progress))


def _validate(model, loader, device) -> dict:
    model.eval()
    all_preds, all_labels = [], []
    with torch.no_grad():
        for batch in loader:
            flow = batch["flow"].to(device)
            hand = batch["handedness"].to(device)
            lbl = batch["label_idx"].to(device)
            logits = model(flow, hand)
            pred = logits.argmax(dim=1)
            all_preds.extend(pred.cpu().tolist())
            all_labels.extend(lbl.cpu().tolist())

    # Per-class precision/recall/F1 + macro-F1
    per_class = {}
    for ci, c in enumerate(CLASSES):
        tp = sum(1 for p, l in zip(all_preds, all_labels) if p == ci and l == ci)
        fp = sum(1 for p, l in zip(all_preds, all_labels) if p == ci and l != ci)
        fn = sum(1 for p, l in zip(all_preds, all_labels) if p != ci and l == ci)
        prec = tp / max(1, tp + fp)
        rec = tp / max(1, tp + fn)
        f1 = 2 * prec * rec / max(1e-9, prec + rec)
        per_class[c] = {"precision": prec, "recall": rec, "f1": f1,
                        "n": Counter(all_labels)[ci]}
    macro_f1 = sum(v["f1"] for v in per_class.values()) / NUM_CLASSES
    return {"macro_f1": macro_f1, "per_class": per_class,
            "n_val": len(all_labels)}


def train(
    dataset_dir: str,
    output_weights: str,
    epochs: int = 50,
    batch_size: int = 16,
    lr: float = 1e-4,
    weight_decay: float = 1e-4,
    warmup_epochs: int = 5,
    label_smoothing: float = 0.1,
    focal: bool = False,
    mixup_alpha: float = 0.2,
    patience: int = 10,
    device: str = "cuda" if torch.cuda.is_available() else "cpu",
    seed: int = 42,
) -> dict:
    torch.manual_seed(seed)
    train_ds = SwingTypeDataset(dataset_dir, split="train", augment=True)
    val_ds = SwingTypeDataset(dataset_dir, split="val", augment=False)
    logger.info("train=%d val=%d device=%s", len(train_ds), len(val_ds), device)

    sampler = _build_sampler(train_ds)
    train_loader = DataLoader(train_ds, batch_size=batch_size, sampler=sampler, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=0)

    model = SwingTypeR2plus1D().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    best_macro_f1 = -1.0
    best_epoch = -1
    no_improve = 0
    best_val_metrics: dict = {}

    for epoch in range(epochs):
        lr_mult = _cosine_with_warmup(epoch, epochs, warmup_epochs)
        for g in opt.param_groups:
            g["lr"] = lr * lr_mult

        model.train()
        epoch_loss = 0.0
        n_batches = 0
        for batch in train_loader:
            flow = batch["flow"].to(device)
            hand = batch["handedness"].to(device)
            lbl = batch["label_idx"].to(device)

            mixed_flow, mixed_hand, lbl_a, lbl_b, lam = _mixup(flow, hand, lbl, mixup_alpha)
            logits = model(mixed_flow, mixed_hand)
            if focal:
                loss = lam * _focal_loss(logits, lbl_a) + (1 - lam) * _focal_loss(logits, lbl_b)
            else:
                loss = (lam * F.cross_entropy(logits, lbl_a, label_smoothing=label_smoothing)
                        + (1 - lam) * F.cross_entropy(logits, lbl_b, label_smoothing=label_smoothing))

            opt.zero_grad()
            loss.backward()
            opt.step()
            epoch_loss += float(loss.item())
            n_batches += 1

        train_loss = epoch_loss / max(1, n_batches)
        val_metrics = _validate(model, val_loader, device)
        logger.info(
            "epoch %02d/%02d lr_mult=%.3f train_loss=%.4f val_macro_f1=%.4f  per_class=%s",
            epoch + 1, epochs, lr_mult, train_loss, val_metrics["macro_f1"],
            {c: f"f1={v['f1']:.2f} n={v['n']}" for c, v in val_metrics["per_class"].items()},
        )

        if val_metrics["macro_f1"] > best_macro_f1:
            best_macro_f1 = val_metrics["macro_f1"]
            best_epoch = epoch
            best_val_metrics = val_metrics
            no_improve = 0
            output_path = Path(output_weights)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({
                "state_dict": model.state_dict(),
                "meta": {
                    "best_epoch": best_epoch + 1,
                    "best_macro_f1": best_macro_f1,
                    "per_class": val_metrics["per_class"],
                    "n_train": len(train_ds),
                    "n_val": len(val_ds),
                    "classes": list(CLASSES),
                    "hyper": {"lr": lr, "weight_decay": weight_decay,
                              "warmup_epochs": warmup_epochs,
                              "label_smoothing": label_smoothing,
                              "focal": focal, "mixup_alpha": mixup_alpha,
                              "batch_size": batch_size, "epochs": epochs},
                    "dataset_dir": str(dataset_dir),
                },
            }, output_path)
            logger.info("  ↑ new best — saved to %s", output_path)
        else:
            no_improve += 1
            if no_improve >= patience:
                logger.info("early stop at epoch %d (patience=%d)", epoch + 1, patience)
                break

    logger.info("BEST: epoch=%d macro_f1=%.4f", best_epoch + 1, best_macro_f1)
    return {"best_epoch": best_epoch + 1, "best_macro_f1": best_macro_f1,
            "best_val_metrics": best_val_metrics}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", required=True,
                    help="Output dir from build_swing_type_dataset.py")
    ap.add_argument("--output", required=True, help="Path to write weights .pt")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--warmup-epochs", type=int, default=5)
    ap.add_argument("--label-smoothing", type=float, default=0.1)
    ap.add_argument("--focal", action="store_true",
                    help="Use focal loss γ=2 instead of cross-entropy + label smoothing")
    ap.add_argument("--mixup-alpha", type=float, default=0.2)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--device", default=None,
                    help="cuda / cpu (default: auto-detect)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    result = train(
        dataset_dir=args.dataset_dir, output_weights=args.output,
        epochs=args.epochs, batch_size=args.batch_size,
        lr=args.lr, weight_decay=args.weight_decay,
        warmup_epochs=args.warmup_epochs,
        label_smoothing=args.label_smoothing, focal=args.focal,
        mixup_alpha=args.mixup_alpha, patience=args.patience,
        device=device, seed=args.seed,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
