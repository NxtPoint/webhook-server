"""Train serve model v1.

Usage:
    python -m ml_pipeline.serve_model.train [--epochs 60] [--threshold 0.5]

Trains on 6 corpus matches, evaluates per-serve on the held-out reference
video (both its corpus tasks). The eval that matters is EVENT-level:
after NMS, how many labeled FAR serves get a kept anchor within ±1.25s
(recall) and how many kept anchors match a label (precision) — directly
comparable to the heuristic far baseline (4/12 on the reference video).

Saves ml_pipeline/models/serve_model_v1.pt when the held-out F1 beats the
previous checkpoint's (file is git-ignored, Batch-bundled via models/).
"""
from __future__ import annotations

import argparse
import logging
import os
from bisect import bisect_left

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WEIGHTS = os.path.join(os.path.dirname(__file__), "..", "models", "serve_model_v1.pt")


def event_eval(anchor_ts, scores, far_label_ts, threshold):
    from ml_pipeline.serve_model.model import nms
    from ml_pipeline.serve_model.dataset import POS_TOL_S
    kept = nms(anchor_ts, scores, threshold)
    kept_ts = [anchor_ts[i] for i in kept]
    matched_labels = set()
    matched_kept = 0
    for kt in kept_ts:
        j = bisect_left(far_label_ts, kt - POS_TOL_S)
        hit = None
        while j < len(far_label_ts) and far_label_ts[j] <= kt + POS_TOL_S:
            if j not in matched_labels:
                hit = j
                break
            j += 1
        if hit is not None:
            matched_labels.add(hit)
            matched_kept += 1
    n_lbl, n_kept = len(far_label_ts), len(kept_ts)
    recall = len(matched_labels) / n_lbl if n_lbl else 0.0
    precision = matched_kept / n_kept if n_kept else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return dict(recall=recall, precision=precision, f1=f1,
                matched=len(matched_labels), labels=n_lbl, emitted=n_kept)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    import torch
    import torch.nn as nn
    from db_init import engine
    from ml_pipeline.serve_model.dataset import build_dataset, split
    from ml_pipeline.serve_model.model import build_mlp, score, save

    ds = build_dataset(engine)
    X_train, y_train, heldout = split(ds)
    pos = int(y_train.sum())
    logger.info("train: %d anchors (%d pos / %d neg) | heldout tasks: %s",
                len(X_train), pos, len(X_train) - pos, list(heldout))

    torch.manual_seed(42)
    model = build_mlp()
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    # class imbalance: sqrt-damped positive weight (full neg/pos ratio ~27
    # makes the scorer over-predict positive and wrecks precision)
    pos_weight = torch.tensor([float(np.sqrt((len(y_train) - pos) / max(pos, 1)))])
    lossf = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    Xt = torch.from_numpy(X_train)
    yt = torch.from_numpy(y_train).unsqueeze(-1)
    n = len(Xt)

    # threshold is CHOSEN ON TRAIN tasks (event-level F1), never on heldout
    train_tasks = {s: d for s, d in ds.items() if not d["heldout"] and len(d["X"])}

    def sweep_threshold():
        best_t, best = args.threshold, -1.0
        for t in np.arange(0.3, 0.96, 0.05):
            f1s = []
            for d in train_tasks.values():
                s = score(model, d["X"])
                f1s.append(event_eval(d["anchor_ts"], s, d["far_label_ts"], t)["f1"])
            m = float(np.mean(f1s))
            if m > best:
                best, best_t = m, float(t)
        return best_t, best

    best_f1 = -1.0
    for epoch in range(args.epochs):
        model.train()
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            opt.zero_grad()
            loss = lossf(model(Xt[idx]), yt[idx])
            loss.backward()
            opt.step()
            tot += loss.item() * len(idx)

        if (epoch + 1) % 10 == 0 or epoch == args.epochs - 1:
            thr, train_f1 = sweep_threshold()
            evs = []
            for short, d in heldout.items():
                s = score(model, d["X"])
                evs.append(event_eval(d["anchor_ts"], s, d["far_label_ts"], thr))
            r = float(np.mean([e["recall"] for e in evs]))
            p = float(np.mean([e["precision"] for e in evs]))
            f1 = float(np.mean([e["f1"] for e in evs]))
            logger.info("epoch %3d  loss=%.4f  thr=%.2f(trainF1=%.2f)  heldout: R=%.2f P=%.2f F1=%.2f  %s",
                        epoch + 1, tot / n, thr, train_f1, r, p, f1,
                        [(s, e['matched'], e['labels'], e['emitted']) for s, e in zip(heldout, evs)])
            if f1 > best_f1:
                best_f1 = f1
                save(model, os.path.abspath(WEIGHTS), dict(
                    version="v1", epoch=epoch + 1, heldout_f1=round(f1, 4),
                    heldout_recall=round(r, 4), heldout_precision=round(p, 4),
                    threshold=round(thr, 2), n_train=len(X_train), n_pos=pos,
                ))

    logger.info("best heldout F1=%.3f  weights=%s", best_f1, os.path.abspath(WEIGHTS))


if __name__ == "__main__":
    main()
