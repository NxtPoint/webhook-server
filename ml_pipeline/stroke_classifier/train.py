"""
Train the optical flow stroke classifier.

Loads .npz training examples exported by export_training_data.py,
trains the StrokeFlowCNN model, and saves weights to models/stroke_classifier.pt.

Usage:
    python -m ml_pipeline.stroke_classifier.train \\
        --data <training_data_dir> \\
        [--epochs 50] [--batch-size 16] [--lr 1e-3]

Data augmentation:
  - Random temporal flip (reverse flow sequence)
  - Random horizontal flip (mirror + negate dx)
  - Random flow magnitude scaling (±20%)
  - Random Gaussian noise on flow

Requires at least 50 labeled examples across 3+ classes for meaningful training.
Recommended: 200+ examples from 3+ dual-submit pairs.
"""

import os
import json
import logging
import argparse
import numpy as np
from pathlib import Path
from typing import List, Tuple

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

from ml_pipeline.stroke_classifier.model import (
    StrokeFlowCNN, STROKE_CLASSES, NUM_CLASSES, STROKE_MODEL_WEIGHTS,
)
from ml_pipeline.stroke_classifier.flow_extractor import FLOW_WINDOW, CROP_H, CROP_W

logger = logging.getLogger(__name__)


class StrokeFlowDataset(Dataset):
    """PyTorch Dataset for optical flow stroke classification."""

    def __init__(self, data_dir: str, augment: bool = True):
        self.data_dir = data_dir
        self.augment = augment
        self.label_to_idx = {name: i for i, name in enumerate(STROKE_CLASSES)}

        # Load manifest
        manifest_path = os.path.join(data_dir, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
            self.examples = manifest["examples"]
        else:
            # Glob for .npz files
            self.examples = []
            for f in sorted(Path(data_dir).glob("*.npz")):
                label = f.stem.split("_")[-1]
                self.examples.append({"file": f.name, "label": label})

        # Filter to valid classes
        self.examples = [
            ex for ex in self.examples
            if ex["label"] in self.label_to_idx
        ]

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx) -> Tuple[torch.Tensor, int]:
        ex = self.examples[idx]
        data = np.load(os.path.join(self.data_dir, ex["file"]))
        flow = data["flow"].astype(np.float32)  # (T, H, W, 2)
        label = self.label_to_idx[ex["label"]]

        if self.augment:
            flow = self._augment(flow)

        # Reshape: (T, H, W, 2) → (2, T, H, W) for Conv3d
        tensor = torch.from_numpy(flow).permute(3, 0, 1, 2)  # (2, T, H, W)
        return tensor, label

    def _augment(self, flow: np.ndarray) -> np.ndarray:
        """Apply random augmentations to flow tensor."""
        # Random temporal flip (reverse swing direction)
        if np.random.random() < 0.5:
            flow = flow[::-1].copy()
            flow[..., :] = -flow[..., :]  # negate flow when reversing time

        # Random horizontal flip (mirror court perspective)
        if np.random.random() < 0.5:
            flow = flow[:, :, ::-1].copy()
            flow[..., 0] = -flow[..., 0]  # negate dx

        # Random magnitude scaling (±20%)
        scale = 0.8 + np.random.random() * 0.4
        flow = flow * scale

        # Random Gaussian noise
        if np.random.random() < 0.3:
            noise = np.random.normal(0, 0.5, flow.shape).astype(np.float32)
            flow = flow + noise

        return flow


def train(
    data_dir: str,
    epochs: int = 50,
    batch_size: int = 16,
    lr: float = 1e-3,
    val_split: float = 0.2,
    device: str = None,
    output_path: str = None,
) -> dict:
    """Train the stroke classifier.

    Returns dict with training metrics.
    """
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    output_path = output_path or STROKE_MODEL_WEIGHTS

    dataset = StrokeFlowDataset(data_dir, augment=True)
    n_total = len(dataset)

    if n_total < 20:
        logger.error(f"Only {n_total} examples — need at least 20 for training")
        return {"error": f"Insufficient data: {n_total} examples"}

    # Verify class distribution
    labels = [dataset.label_to_idx[ex["label"]] for ex in dataset.examples]
    unique_classes = set(labels)
    logger.info(f"Total examples: {n_total}, classes: {len(unique_classes)}")
    for cls_name, cls_idx in dataset.label_to_idx.items():
        count = labels.count(cls_idx)
        if count > 0:
            logger.info(f"  {cls_name}: {count}")

    # Train/val split
    n_val = max(1, int(n_total * val_split))
    n_train = n_total - n_val
    indices = np.random.permutation(n_total)
    train_indices = indices[:n_train].tolist()
    val_indices = indices[n_train:].tolist()

    train_subset = torch.utils.data.Subset(dataset, train_indices)
    val_dataset = StrokeFlowDataset(data_dir, augment=False)
    val_subset = torch.utils.data.Subset(val_dataset, val_indices)

    train_loader = DataLoader(train_subset, batch_size=batch_size, shuffle=True, drop_last=False)
    val_loader = DataLoader(val_subset, batch_size=batch_size, shuffle=False)

    # Class weights for imbalanced data
    class_counts = np.bincount(labels, minlength=NUM_CLASSES).astype(np.float32)
    class_counts = np.maximum(class_counts, 1)  # avoid div by zero
    class_weights = 1.0 / class_counts
    class_weights /= class_weights.sum()
    class_weights = torch.from_numpy(class_weights).to(device)

    model = StrokeFlowCNN().to(device)
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)

    best_val_acc = 0.0
    best_state = None
    history = {"train_loss": [], "val_loss": [], "val_acc": []}

    for epoch in range(epochs):
        # Training
        model.train()
        train_loss = 0.0
        train_correct = 0
        train_total = 0

        for flows, labels_batch in train_loader:
            flows = flows.to(device)
            labels_batch = labels_batch.to(device)

            optimizer.zero_grad()
            logits = model(flows)
            loss = criterion(logits, labels_batch)
            loss.backward()
            optimizer.step()

            train_loss += loss.item() * flows.size(0)
            train_correct += (logits.argmax(1) == labels_batch).sum().item()
            train_total += flows.size(0)

        train_loss /= max(train_total, 1)
        train_acc = train_correct / max(train_total, 1)

        # Validation
        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_total = 0

        with torch.no_grad():
            for flows, labels_batch in val_loader:
                flows = flows.to(device)
                labels_batch = labels_batch.to(device)
                logits = model(flows)
                loss = criterion(logits, labels_batch)
                val_loss += loss.item() * flows.size(0)
                val_correct += (logits.argmax(1) == labels_batch).sum().item()
                val_total += flows.size(0)

        val_loss /= max(val_total, 1)
        val_acc = val_correct / max(val_total, 1)
        scheduler.step(val_loss)

        history["train_loss"].append(train_loss)
        history["val_loss"].append(val_loss)
        history["val_acc"].append(val_acc)

        if (epoch + 1) % 5 == 0 or epoch == 0:
            logger.info(
                f"Epoch {epoch+1}/{epochs} — "
                f"train_loss={train_loss:.4f} train_acc={train_acc:.1%} "
                f"val_loss={val_loss:.4f} val_acc={val_acc:.1%}"
            )

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    # Save best model
    if best_state is not None:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        torch.save(best_state, output_path)
        logger.info(f"Saved best model (val_acc={best_val_acc:.1%}) to {output_path}")

    return {
        "best_val_acc": best_val_acc,
        "epochs": epochs,
        "n_train": n_train,
        "n_val": n_val,
        "history": history,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Train stroke classifier")
    parser.add_argument("--data", required=True, help="Training data directory")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--output", default=None, help="Output weights path")
    parser.add_argument("--device", default=None, help="cpu or cuda")
    args = parser.parse_args()

    result = train(
        data_dir=args.data,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        device=args.device,
        output_path=args.output,
    )
    print(f"\nTraining complete: {json.dumps(result, indent=2, default=str)}")
