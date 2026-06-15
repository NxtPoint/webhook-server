"""ADR-02 v2 swing-type classifier regression bench.

Replays the locked val split of the swing_type_v1 dataset against the
current `ml_pipeline/models/swing_classifier_v2.pt` weights. Reports
per-class precision/recall/F1 + macro-F1. Compares vs
`ml_pipeline/diag/bench_baseline_swing_type.json` and exits non-zero on
any negative delta of macro-F1.

STOPGAP semantics: if weights aren't present, the bench reports
`available=False` and exits 0. The baseline file's `macro_f1` field
should be left as null until weights ship; first successful run
captures the baseline (write via --bless flag).

Usage:
    .venv/Scripts/python -m ml_pipeline.diag.bench_swing_type
    .venv/Scripts/python -m ml_pipeline.diag.bench_swing_type --bless    # lock current as baseline

Not wired to CI (yet). Once weights ship + baseline locked, add to
.github/workflows/bench.yml alongside serve bench. Until then, local-only
gate per `CLAUDE.md` rule #1.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import torch
from torch.utils.data import DataLoader

from ml_pipeline.stroke_classifier.dataset import SwingTypeDataset
from ml_pipeline.stroke_classifier.model_v2 import (
    CLASSES, MODEL_WEIGHTS_V2, SwingTypeClassifierV2,
)

logger = logging.getLogger("bench_swing_type")

DEFAULT_DATASET_DIR = "ml_pipeline/training/datasets/swing_type_v3_4class"
BASELINE_PATH = Path(__file__).resolve().parent / "bench_baseline_swing_type.json"


def run_bench(dataset_dir: str = DEFAULT_DATASET_DIR,
              batch_size: int = 16) -> dict:
    clf = SwingTypeClassifierV2()
    if not clf.available:
        logger.info(
            "STOPGAP: weights not present at %s — skipping replay; classifier returns []",
            MODEL_WEIGHTS_V2,
        )
        return {"available": False, "macro_f1": None, "per_class": {},
                "n_val": 0, "weights_path": MODEL_WEIGHTS_V2}

    val_ds = SwingTypeDataset(dataset_dir, split="val", augment=False)
    loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=0)
    all_preds: list[int] = []
    all_labels: list[int] = []
    for batch in loader:
        preds = clf.predict_batch(batch["flow"], batch["handedness"])
        for (cls_name, _conf), lbl in zip(preds, batch["label_idx"].tolist()):
            all_preds.append(CLASSES.index(cls_name))
            all_labels.append(int(lbl))

    # Per-class precision/recall/F1
    per_class = {}
    for ci, c in enumerate(CLASSES):
        tp = sum(1 for p, l in zip(all_preds, all_labels) if p == ci and l == ci)
        fp = sum(1 for p, l in zip(all_preds, all_labels) if p == ci and l != ci)
        fn = sum(1 for p, l in zip(all_preds, all_labels) if p != ci and l == ci)
        prec = tp / max(1, tp + fp)
        rec = tp / max(1, tp + fn)
        f1 = 2 * prec * rec / max(1e-9, prec + rec)
        per_class[c] = {"precision": round(prec, 4),
                        "recall": round(rec, 4),
                        "f1": round(f1, 4)}
    macro_f1 = round(sum(v["f1"] for v in per_class.values()) / len(CLASSES), 4)

    return {"available": True, "macro_f1": macro_f1, "per_class": per_class,
            "n_val": len(all_labels), "weights_path": MODEL_WEIGHTS_V2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR)
    ap.add_argument("--bless", action="store_true",
                    help="Write current run as the new locked baseline.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    current = run_bench(dataset_dir=args.dataset_dir)

    print(json.dumps(current, indent=2))

    if not current["available"]:
        print("[OK] STOPGAP — no weights, no regression possible.")
        return 0

    if args.bless:
        BASELINE_PATH.write_text(json.dumps(current, indent=2))
        print(f"[BLESS] wrote new baseline to {BASELINE_PATH}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"[WARN] no baseline at {BASELINE_PATH}; run --bless to lock current.")
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    if not baseline.get("available"):
        print("[INFO] baseline is also STOPGAP — no comparison.")
        return 0

    delta = current["macro_f1"] - baseline["macro_f1"]
    if delta < 0:
        print(f"[FAIL] macro_f1 regression: {baseline['macro_f1']} -> {current['macro_f1']} (Δ={delta:+.4f})")
        return 1
    print(f"[OK] macro_f1 {current['macro_f1']} vs baseline {baseline['macro_f1']} (Δ={delta:+.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
