"""PyTorch Dataset wrapper for the ADR-02 v2 swing-type corpus.

Reads the dataset built by `ml_pipeline.training.build_swing_type_dataset`:
  - manifest.json                       — match list + train/val split + totals
  - {t5_task_id}.pt                     — per-match (N, 16, 112, 112, 2) flows + metadata

Per-hit returns: (flow_tensor, label_idx, role_str, handedness_bit, meta).

Flow tensor shape transformation:
  on disk:  (16, 112, 112, 2)   — temporal, H, W, (dx, dy)
  to model: (2, 16, 112, 112)   — (dx,dy) channels, temporal, H, W
            ready for r2plus1d_18 input.

Augmentation (apply=True at train time, False at val time):
  - horizontal flip with dx-channel sign flip + handedness bit toggle
    (per ADR-02 §"Training recipe" / Hong et al. ICCV 2021)
  - temporal random crop ±2 frames (drop 2 from one end, pad with edge frames)

NOT done here (caller's responsibility):
  - mixup α=0.2 — done in train loop on the assembled batch
  - WeightedRandomSampler for backhand minority — passed as DataLoader sampler
  - focal loss — done in loss fn

Handedness:
  Per-match handedness should ideally be auto-inferred from the first ~10 hits
  (forehand-side preference). For STOPGAP v0 the default is right-handed (1.0)
  for every player. Override via the `handedness_overrides` constructor arg
  (dict: {(t5_task_id, player_id): "right"|"left"}). Training time can pass an
  inferred map; inference time (inference_v2.py) can override per-match too.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Optional

import torch
from torch.utils.data import Dataset

from ml_pipeline.stroke_classifier.model_v2 import CLASS_TO_IDX, NUM_CLASSES

logger = logging.getLogger(__name__)

DEFAULT_HANDEDNESS = "right"   # 1.0 in the bit; ADR-02 fallback for unknown player


def _handedness_to_bit(handedness: str) -> float:
    return 1.0 if handedness == "right" else 0.0


class SwingTypeDataset(Dataset):
    """One example per (match, hit). Loads all per-match flow tensors into
    RAM eagerly; the full dataset (368 hits x 16 x 112 x 112 x 2 x 4 bytes)
    is ~120 MB — fine for a workstation, will rebuild lazily per match if
    we hit memory pressure post-Corpus-4.
    """

    def __init__(
        self,
        dataset_dir: str | Path,
        split: str = "train",                    # 'train' | 'val' | 'all'
        augment: bool = False,
        handedness_overrides: Optional[dict] = None,
        temporal_crop_jitter: int = 2,
    ) -> None:
        super().__init__()
        self.dataset_dir = Path(dataset_dir)
        self.augment = augment
        self.temporal_crop_jitter = int(temporal_crop_jitter)
        self.handedness_overrides = handedness_overrides or {}

        manifest_path = self.dataset_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"missing manifest at {manifest_path}")
        manifest = json.loads(manifest_path.read_text())

        if split == "train":
            wanted = set(manifest["train_match_ids"])
        elif split == "val":
            wanted = set(manifest["val_match_ids"])
        elif split == "all":
            wanted = set(manifest["train_match_ids"] + manifest["val_match_ids"])
        else:
            raise ValueError(f"split must be 'train'|'val'|'all'; got {split!r}")

        # Flatten (match_idx, hit_idx) index. Keep per-match flows + labels
        # in self._matches; the index is (m_i, h_i) tuples.
        self._matches: list[dict] = []
        self._index: list[tuple[int, int]] = []
        for m in manifest["matches"]:
            if "error" in m:
                continue
            if m["t5_task_id"] not in wanted:
                continue
            pt_path = Path(m["pt_path"])
            if not pt_path.is_absolute():
                pt_path = self.dataset_dir / pt_path.name
            blob = torch.load(pt_path, weights_only=False)
            self._matches.append({
                "t5_task_id": m["t5_task_id"],
                "flows": blob["flows"],         # (N, 16, 112, 112, 2)
                "labels_dict": blob["labels"],  # dict of parallel lists, len N
                "meta": blob["meta"],
            })
            n_hits = blob["flows"].shape[0]
            mi = len(self._matches) - 1
            for hi in range(n_hits):
                self._index.append((mi, hi))

        if not self._index:
            raise RuntimeError(f"empty {split} split — no usable hits")

        self.split = split
        logger.info(
            "SwingTypeDataset[%s] loaded %d hits across %d matches (augment=%s)",
            split, len(self._index), len(self._matches), augment,
        )

    def __len__(self) -> int:
        return len(self._index)

    def _resolve_handedness(self, t5_task_id: str, player_id_sa: Optional[int]) -> str:
        key = (t5_task_id, player_id_sa)
        return self.handedness_overrides.get(key, DEFAULT_HANDEDNESS)

    def __getitem__(self, idx: int) -> dict:
        m_i, h_i = self._index[idx]
        m = self._matches[m_i]

        flow = m["flows"][h_i]  # (16, 112, 112, 2), float32 torch tensor
        labels = m["labels_dict"]

        swing_type = labels["swing_type"][h_i]
        label_idx = CLASS_TO_IDX[swing_type]
        role = labels["role"][h_i]
        player_id_sa = labels["player_id_sa"][h_i] if "player_id_sa" in labels else None
        handedness = self._resolve_handedness(m["t5_task_id"], player_id_sa)
        handedness_bit = _handedness_to_bit(handedness)

        # Augmentations (train only)
        if self.augment:
            flow, handedness_bit = self._maybe_hflip(flow, handedness_bit)
            flow = self._maybe_temporal_crop(flow)

        # On-disk shape (T, H, W, C) -> model shape (C, T, H, W)
        flow = flow.permute(3, 0, 1, 2).contiguous()   # (2, 16, 112, 112)

        return {
            "flow": flow,
            "label_idx": torch.tensor(label_idx, dtype=torch.long),
            "handedness": torch.tensor([handedness_bit], dtype=torch.float32),
            "role": role,
            "t5_task_id": m["t5_task_id"],
            "hit_frame": labels["hit_frame"][h_i],
        }

    def _maybe_hflip(self, flow: torch.Tensor, hand_bit: float) -> tuple:
        """Horizontal flip the spatial axis AND negate dx channel AND toggle handedness."""
        if random.random() < 0.5:
            # flow is (T, H, W, 2); flip W = axis 2; dx is channel 0
            flow = torch.flip(flow, dims=(2,))
            flow = flow.clone()
            flow[..., 0] = -flow[..., 0]
            hand_bit = 1.0 - hand_bit
        return flow, hand_bit

    def _maybe_temporal_crop(self, flow: torch.Tensor) -> torch.Tensor:
        """Drop up to `temporal_crop_jitter` frames from one end, pad with edge
        copies to keep total T constant. Mirrors common video-CNN augmentation."""
        if self.temporal_crop_jitter <= 0:
            return flow
        T = flow.shape[0]
        drop = random.randint(0, self.temporal_crop_jitter)
        if drop == 0:
            return flow
        if random.random() < 0.5:
            # drop from front, pad with first remaining frame
            kept = flow[drop:]
            pad = kept[:1].expand(drop, -1, -1, -1)
            return torch.cat([pad, kept], dim=0)
        else:
            kept = flow[: T - drop]
            pad = kept[-1:].expand(drop, -1, -1, -1)
            return torch.cat([kept, pad], dim=0)


def class_counts(dataset: SwingTypeDataset) -> dict[str, int]:
    """Helper: count per-class for class-weight / sampler setup."""
    counts = {c: 0 for c in CLASS_TO_IDX}
    for m in dataset._matches:
        for st in m["labels_dict"]["swing_type"]:
            counts[st] += 1
    return counts
