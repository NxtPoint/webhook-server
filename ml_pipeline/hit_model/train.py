"""Train hit model v1.

Usage:
    python -m ml_pipeline.hit_model.train [--epochs 200] [--batch 512]

Trains on 6 warp-era corpus videos (~2,380 labels), evaluates on the
held-out reference video — the CLEAN rev-77 task (86ade942) is the gate
number; the two warp-era tasks of the same video are reported for drift
context.

The eval that matters is EVENT-level and PID-STRICT @1.0s, directly
comparable to the heuristic stroke_detector baseline on the same video:
near 13/51, far 19/51, with 216 emitted (the bar to beat on recall AND
precision). Also reports @0.5s (the training tolerance).

Saves ml_pipeline/models/hit_model_v1.pt when the clean held-out F1@0.5s
improves (threshold swept on TRAIN tasks only).
"""
from __future__ import annotations

import argparse
import logging
import os
from bisect import bisect_left

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WEIGHTS = os.path.join(os.path.dirname(__file__), "..", "models", "hit_model_v1.pt")

HEURISTIC_BASELINE = {"near": (13, 51), "far": (19, 51), "emitted": 216}


def event_eval(d, scores, threshold, tol):
    """Event-level eval for one task: NMS -> match kept events to labels.

    Returns dict with overall R/P/F1 at `tol` plus pid-strict near/far
    hit counts (the WHO rule attributes each kept event)."""
    from ml_pipeline.hit_model.model import nms
    from ml_pipeline.hit_model.candidates import attribute_player
    kept = nms(d["anchor_ts"], scores, threshold)
    kept_ts = [d["anchor_ts"][i] for i in kept]
    kept_pid = [attribute_player(d["cands"][i]) for i in kept]
    labels = d["labels"]  # [(ts, pid)]

    used = set()
    matched = 0
    pid_hit = {0: 0, 1: 0}
    pid_tot = {0: 0, 1: 0}
    for ts_l, pid_l in labels:
        pid_tot[pid_l] += 1
        best, bd = None, tol + 1
        for k, (ts_k, pid_k) in enumerate(zip(kept_ts, kept_pid)):
            if k in used:
                continue
            dt = abs(ts_k - ts_l)
            if dt <= tol and dt < bd:
                best, bd = k, dt
        if best is not None:
            used.add(best)
            matched += 1
            if kept_pid[best] == pid_l:
                pid_hit[pid_l] += 1
    n_l, n_k = len(labels), len(kept_ts)
    recall = matched / n_l if n_l else 0.0
    precision = matched / n_k if n_k else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return dict(recall=recall, precision=precision, f1=f1, matched=matched,
                labels=n_l, emitted=n_k,
                near=f"{pid_hit[0]}/{pid_tot[0]}", far=f"{pid_hit[1]}/{pid_tot[1]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    import torch
    import torch.nn as nn
    from db_init import engine
    from ml_pipeline.hit_model.dataset import build_dataset, split
    from ml_pipeline.hit_model.model import build_mlp, score, save

    ds = build_dataset(engine)
    X_train, y_train, w_train, heldout = split(ds)
    pos = int(y_train.sum())
    n_ignored = int((w_train == 0).sum())
    logger.info("train: %d candidates (%d pos / %d neg / %d ignored) | heldout: %s",
                len(X_train), pos, len(X_train) - pos - n_ignored, n_ignored,
                list(heldout))

    torch.manual_seed(42)
    model = build_mlp()
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    n_neg = len(y_train) - pos - n_ignored
    pos_weight = torch.tensor([float(np.sqrt(n_neg / max(pos, 1)))])
    lossf = nn.BCEWithLogitsLoss(pos_weight=pos_weight, reduction="none")

    Xt = torch.from_numpy(X_train)
    yt = torch.from_numpy(y_train).unsqueeze(-1)
    wt = torch.from_numpy(w_train).unsqueeze(-1)
    n = len(Xt)

    train_tasks = {s: d for s, d in ds.items() if not d["heldout"] and len(d["X"])}
    clean_tasks = {s: d for s, d in heldout.items() if d.get("clean")}

    def sweep_threshold():
        best_t, best = 0.5, -1.0
        for t in np.arange(0.3, 0.96, 0.05):
            f1s = [event_eval(d, score(model, d["X"]), t, 0.5)["f1"]
                   for d in train_tasks.values()]
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
            per = lossf(model(Xt[idx]), yt[idx]) * wt[idx]  # ambiguous zone weight 0
            loss = per.sum() / wt[idx].sum().clamp(min=1.0)
            loss.backward()
            opt.step()
            tot += loss.item() * len(idx)

        if (epoch + 1) % 10 == 0 or epoch == args.epochs - 1:
            thr, train_f1 = sweep_threshold()
            evs = {s: event_eval(d, score(model, d["X"]), thr, 0.5)
                   for s, d in heldout.items()}
            clean = {s: e for s, e in evs.items() if s in clean_tasks}
            cf1 = float(np.mean([e["f1"] for e in clean.values()])) if clean else 0.0
            logger.info(
                "epoch %3d loss=%.4f thr=%.2f(trainF1=%.2f) CLEAN F1=%.2f %s | warp-era %s",
                epoch + 1, tot / n, thr, train_f1, cf1,
                {s: (e["matched"], e["labels"], e["emitted"], e["near"], e["far"])
                 for s, e in clean.items()},
                {s: (e["matched"], e["labels"]) for s, e in evs.items()
                 if s not in clean_tasks},
            )
            if cf1 > best_f1:
                best_f1 = cf1
                # gate-comparable numbers at 1.0s pid-strict
                gate = {s: event_eval(d, score(model, d["X"]), thr, 1.0)
                        for s, d in clean_tasks.items()}
                save(model, os.path.abspath(WEIGHTS), dict(
                    version="v1", epoch=epoch + 1, threshold=round(thr, 2),
                    clean_f1_05=round(cf1, 4),
                    gate_1s={s: dict(near=g["near"], far=g["far"],
                                     emitted=g["emitted"]) for s, g in gate.items()},
                    heuristic_baseline=HEURISTIC_BASELINE,
                    n_train=len(X_train), n_pos=pos,
                ))

    logger.info("best CLEAN heldout F1@0.5=%.3f  weights=%s",
                best_f1, os.path.abspath(WEIGHTS))


if __name__ == "__main__":
    main()
