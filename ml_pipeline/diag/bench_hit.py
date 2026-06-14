"""Hit-model bench — the hit-layer equivalent of bench_bounce / bench_ball.

Runs the trained hit model over the dual-submit corpus and reports, per task
and in aggregate, the two far-side failure modes the hit model is gated on:

  EMISSION    — for each SA player_swing, did a kept (NMS) event fire <=1.0s?
  ATTRIBUTION — of those that fired, was the nearest correctly attributed
                to the right court side (near/far)?
  GATE        — emission AND attribution (the real per-label success).
  PRECISION   — kept events that matched some label / total kept.

Split near/far because the whole hit story is far-side: emission is balanced
(B1 anchor recall 94-96%) but far ATTRIBUTION is the blocker (far ~6/51).
This is the repeatable gate for the sharp-far retrain (DoD item #6).

LOCAL-ONLY — not CI-gated (rule #9; CI is the serve bench only). Reads the
live prod DB (dev box IP allowlisted) + the corpus pairing in
ml_analysis.training_corpus. Labels come STRAIGHT from bronze SA player_swing
(per-swing positional side via ball_hit_location_y > 11.885 = near), the same
source hit_model/dataset.py trains on.

Usage:
    python -m ml_pipeline.diag.bench_hit
    python -m ml_pipeline.diag.bench_hit --task <t5_uuid>
    python -m ml_pipeline.diag.bench_hit --threshold 0.6
    python -m ml_pipeline.diag.bench_hit --weights-path ml_pipeline/models/hit_model_v1.pt
    python -m ml_pipeline.diag.bench_hit --update-baseline
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy import text as sql_text


BASELINE_PATH = Path("ml_pipeline/diag/bench_baseline_hit.json")
DEFAULT_WEIGHTS = "ml_pipeline/models/hit_model_v1.pt"
EMIT_TOL_S = 1.0   # a label is "fired on" if a kept event is within this


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _load_corpus_pairs(conn) -> list[dict]:
    """(t5_task_id, sa_task_id) pairs from the corpus (ball_position kind —
    one row per paired task, same set bench_bounce uses)."""
    rows = conn.execute(sql_text("""
        SELECT t5_task_id, sa_task_id
        FROM ml_analysis.training_corpus
        WHERE label_kind = 'ball_position'
        ORDER BY created_at
    """)).mappings().all()
    return [dict(r) for r in rows]


def _eval_task(conn, t5_tid: str, sa_tid: str, model, thr: float) -> dict:
    """Run the hit model on one task; return near/far emission/attribution."""
    from ml_pipeline.hit_model.dataset import load_task_arrays, _sa_labels
    from ml_pipeline.hit_model.features import featurize
    from ml_pipeline.hit_model.candidates import hit_candidates, attribute_player
    from ml_pipeline.hit_model.model import score, nms

    arrays = load_task_arrays(conn, t5_tid)
    labels = _sa_labels(conn, sa_tid)
    cands = hit_candidates(arrays["ball_rows"], arrays["fps"])
    if not cands or not labels:
        return {"task": t5_tid[:8], "error": "no candidates" if not cands else "no labels"}

    cand_ts = [c.ts for c in cands]
    X = np.stack([featurize(c, cand_ts, arrays["ball_ts"], arrays["cnn_ts"],
                            arrays["legacy_ts"], arrays["near_lookup"],
                            arrays["far_lookup"]) for c in cands])
    scores = score(model, X)
    kept = nms(cand_ts, scores, thr)
    kept_ts = [cand_ts[i] for i in kept]
    kept_pid = [attribute_player(cands[i]) for i in kept]

    out = {"task": t5_tid[:8], "emitted": len(kept), "matched_any": 0}
    for side, name in [(0, "near"), (1, "far")]:
        labs = [ts for ts, p in labels if p == side]
        fired = correct = 0
        used: set[int] = set()
        for ts_l in labs:
            best, bd = None, EMIT_TOL_S + 1e-9
            for k, tk in enumerate(kept_ts):
                if k in used:
                    continue
                d = abs(tk - ts_l)
                if d <= EMIT_TOL_S and d < bd:
                    best, bd = k, d
            if best is not None:
                used.add(best)
                fired += 1
                correct += int(kept_pid[best] == side)
        out[f"{name}_labels"] = len(labs)
        out[f"{name}_emit"] = fired           # fired within tol
        out[f"{name}_gate"] = correct         # fired AND right side
        out["matched_any"] += fired
    return out


def _agg(results: list[dict]) -> dict:
    ok = [r for r in results if "error" not in r]
    a = {k: 0 for k in ("near_labels", "near_emit", "near_gate",
                        "far_labels", "far_emit", "far_gate",
                        "emitted", "matched_any")}
    for r in ok:
        for k in a:
            a[k] += r.get(k, 0)
    return a


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text())


# Enforcement slack: counts can wobble ±a few between runs (NMS ties /
# corpus row ordering). The gate is the two real accuracy axes: NEAR gate,
# FAR gate, and total matched_any (precision proxy).
HIT_GATE_SLACK = 2


def _check_regression(agg: dict, base_agg: dict | None) -> tuple[bool, list[str]]:
    """Compare the aggregate to the committed baseline — same contract as the
    serve bench.py: negative delta on a tracked axis => regression."""
    if not base_agg:
        return (False, ["(no committed baseline aggregate — nothing to compare)"])
    regressed = False
    lines: list[str] = []
    for axis in ("near_gate", "far_gate", "matched_any"):
        cur = agg.get(axis, 0)
        bas = base_agg.get(axis, 0)
        d = cur - bas
        tag = "" if d >= -HIT_GATE_SLACK else "  [!] REGRESSION"
        if tag:
            regressed = True
        lines.append(f"  {axis:<12} {cur:>5} vs {bas:<5} (delta {d:+d}){tag}")
    return (regressed, lines)


def _fmt_pct(n, d):
    return f"{(100.0*n/d):.0f}%" if d else "  -"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None, help="restrict to one t5_task_id")
    ap.add_argument("--weights-path", default=DEFAULT_WEIGHTS)
    ap.add_argument("--threshold", type=float, default=None,
                    help="override the model's stored operating threshold")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)

    from ml_pipeline.hit_model.model import load
    if not Path(args.weights_path).exists():
        print(f"[ABORT] weights not found: {args.weights_path} — train first "
              f"(python -m ml_pipeline.hit_model.train)", file=sys.stderr)
        return 1
    model, meta = load(args.weights_path)
    thr = args.threshold if args.threshold is not None else meta.get("threshold", 0.5)

    from db_init import engine
    with engine.connect() as conn:
        pairs = _load_corpus_pairs(conn)
    if args.task:
        pairs = [p for p in pairs if p["t5_task_id"] == args.task]
    if not pairs:
        print("No corpus pairs (ml_analysis.training_corpus, label_kind=ball_position).",
              file=sys.stderr)
        return 1

    print(f"=== bench_hit  {len(pairs)} corpus tasks  commit={_git_sha()} ===")
    print(f"    weights={args.weights_path}  threshold={thr}  emit_tol={EMIT_TOL_S}s")
    print()
    print(f"{'task':<10} {'emit':>6} {'N_lab':>6} {'N_fire':>7} {'N_gate':>7} "
          f"{'F_lab':>6} {'F_fire':>7} {'F_gate':>7}")
    print("-" * 70)

    results = []
    with engine.connect() as conn:
        for p in pairs:
            r = _eval_task(conn, p["t5_task_id"], p["sa_task_id"], model, thr)
            results.append(r)
            if "error" in r:
                print(f"{r['task']:<10} ERROR: {r['error']}")
                continue
            print(f"{r['task']:<10} {r['emitted']:>6} "
                  f"{r['near_labels']:>6} {r['near_emit']:>7} {r['near_gate']:>7} "
                  f"{r['far_labels']:>6} {r['far_emit']:>7} {r['far_gate']:>7}")

    a = _agg(results)
    print("-" * 70)
    print(f"{'TOTAL':<10} {a['emitted']:>6} "
          f"{a['near_labels']:>6} {a['near_emit']:>7} {a['near_gate']:>7} "
          f"{a['far_labels']:>6} {a['far_emit']:>7} {a['far_gate']:>7}")
    print()
    print(f"NEAR  emission {a['near_emit']}/{a['near_labels']} ({_fmt_pct(a['near_emit'],a['near_labels'])})"
          f"  gate {a['near_gate']}/{a['near_labels']} ({_fmt_pct(a['near_gate'],a['near_labels'])})")
    print(f"FAR   emission {a['far_emit']}/{a['far_labels']} ({_fmt_pct(a['far_emit'],a['far_labels'])})"
          f"  gate {a['far_gate']}/{a['far_labels']} ({_fmt_pct(a['far_gate'],a['far_labels'])})")
    tot_lab = a['near_labels'] + a['far_labels']
    tot_gate = a['near_gate'] + a['far_gate']
    print(f"TOTAL gate {tot_gate}/{tot_lab} ({_fmt_pct(tot_gate,tot_lab)})  "
          f"precision(matched/emit) {a['matched_any']}/{a['emitted']} ({_fmt_pct(a['matched_any'],a['emitted'])})")
    print()
    print("Read: FAR gate is THE hit blocker (far attribution). Emission is the "
          "easy half; attribution lifts with the sharp-far retrain (DoD #8).")

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps({
            "updated_at": date.today().isoformat(), "commit": _git_sha(),
            "weights": args.weights_path, "threshold": thr,
            "aggregate": a, "tasks": results,
        }, indent=2, default=str))
        print(f"\n-> wrote baseline {BASELINE_PATH}")
        return 0

    # Enforcement: compare to the committed baseline and exit non-zero on a
    # negative delta (mirrors the serve bench.py contract). --task narrows the
    # population, so skip the gate there.
    if args.task:
        print("\n[skip gate] --task narrows the population; run the full "
              "corpus to enforce against the baseline.")
        return 0
    base_agg = _load_baseline().get("aggregate")
    regressed, lines = _check_regression(a, base_agg)
    print("\n=== vs committed baseline ===")
    for ln in lines:
        print(ln)
    if regressed:
        print("\n[!] REGRESSION DETECTED vs bench_baseline_hit.json. "
              "Investigate before pushing.")
        return 1
    print("\n[OK] No regression vs committed hit baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
