"""Silver reconciliation bench — score silver.point_detail vs hand-verified video GT.

Companion to `bench_silver` (which locks aggregate silver stats). This bench scores
POINT-LEVEL correctness — point boundaries and point winners — against ground truth
that Tomo hand-mapped on video, for BOTH reference matches:

  c8b77210 (Tomo v Jimbo)   — the 18/18 clean anchor (anti-overfit; must never move)
  df594aea (Erin v Yolanda) — a full, badly-tracked match (~100 points)

GT lives in `ml_pipeline/ground_truth/recon_<task8>.json` (see that dir). Two shapes:
  - boundary_mode="shot_map": {silver.point_detail.id -> true_point} + {true_point -> winner_id}
    (df594aea — the Excel export carries a true-point per shot, so we can score
     point BOUNDARIES: exact-1:1 vs merges vs splits.)
  - boundary_mode="identity": {point_number -> winner_id}  (c8b77210 — silver point == true
     point by construction (18/18), so only WINNERS are scored.)

Scoring is ID-based (silver.point_winner_player_id vs GT winner id) — no name mapping needed.

DB source is swappable so the SAME scorer measures the shipped baseline AND a candidate change:
  --db prod    (default) tf_readonly on Render prod  — read the CURRENTLY SHIPPED silver
  --db devenv           localhost:55433/tf_dev       — after a local build_silver_v2 rebuild
  --db-url <url>        explicit override

Iron rule (docs/_investigation/silver_recon_bench_plan.md): a silver change must keep
c8b77210 at 18/18 AND not regress df594aea (both-match anti-overfit). This bench is that gate.

Usage:
    python -m ml_pipeline.diag.recon_bench                      # score both vs prod
    python -m ml_pipeline.diag.recon_bench --diff               # + per-point winner diff
    python -m ml_pipeline.diag.recon_bench --db devenv          # score a local rebuild
    python -m ml_pipeline.diag.recon_bench --update-baseline    # lock current as baseline
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

GT_DIR = Path("ml_pipeline/ground_truth")
BASELINE_PATH = Path("ml_pipeline/diag/recon_baseline.json")
DEVENV_URL = "postgresql+psycopg://tf:tf@localhost:55433/tf_dev"


# ---------------------------------------------------------------------------
# DB source resolution
# ---------------------------------------------------------------------------
def _prod_url() -> str:
    """Read the tf_readonly URL from devenv/.env.local (read-only prod role)."""
    env = Path("devenv/.env.local")
    if not env.exists():
        raise SystemExit("devenv/.env.local not found — need the tf_readonly URL for --db prod")
    for line in env.read_text().splitlines():
        m = re.search(r"(postgresql://tf_readonly:[^\s]+)", line)
        if m:
            url = m.group(1)
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
    raise SystemExit("no tf_readonly URL in devenv/.env.local")


def _resolve_db(db: str, db_url: str | None) -> str:
    if db_url:
        return db_url
    if db == "prod":
        return _prod_url()
    if db == "devenv":
        return os.environ.get("DEVENV_URL", DEVENV_URL)
    raise SystemExit(f"unknown --db {db!r} (use prod|devenv or --db-url)")


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------
def _load_gts() -> list[dict]:
    gts = []
    for path in sorted(glob.glob(str(GT_DIR / "recon_*.json"))):
        gt = json.loads(Path(path).read_text())
        gt["_path"] = path
        gts.append(gt)
    return gts


def _fetch_all(conn, task_id: str, model: str) -> list[dict]:
    """Every silver shot (spine AND excluded), keyed later by ball_hit_s.

    We need the excluded rows too: a GT in-play shot that silver marked
    exclude_d=TRUE is a silver *drop* (e.g. df594aea true point 9 — all its
    shots excluded), which a spine-only fetch would silently hide.
    """
    from sqlalchemy import text
    return [dict(r) for r in conn.execute(text("""
        SELECT id, point_number, point_winner_player_id, exclude_d,
               double_fault_d, ace_d, shot_outcome_d,
               round(ball_hit_s::numeric, 3) AS hs
        FROM silver.point_detail
        WHERE task_id = :t AND model = :m
    """), {"t": task_id, "m": model}).mappings().all()]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_match(conn, gt: dict) -> dict:
    rows = _fetch_all(conn, gt["task_id"], gt["model"])
    spine = [r for r in rows if not r["exclude_d"]]
    # winner per silver point (constant within a point; first non-null on the spine)
    sp_winner: dict = {}
    for r in spine:
        sp, w = r["point_number"], r["point_winner_player_id"]
        if sp is not None and w is not None and sp not in sp_winner:
            sp_winner[sp] = str(w)
    silver_points = sorted({r["point_number"] for r in spine if r["point_number"] is not None})

    out: dict = {"task_id": gt["task_id"], "match": gt.get("match"),
                 "silver_points": len(silver_points)}

    if gt["boundary_mode"] == "shot_map":
        # GT keyed on ball_hit_s (stable across rebuilds). Join to silver by ts.
        shot_true = {round(float(k), 3): int(v) for k, v in gt["shot_true_point"].items()}
        spine_ts = {float(r["hs"]): r for r in spine if r["hs"] is not None}
        excl_ts = {float(r["hs"]): r for r in rows if r["exclude_d"] and r["hs"] is not None}

        sp_true: dict = defaultdict(set)   # silver point_number -> {true points} (spine only)
        true_sp: dict = defaultdict(set)   # true point -> {silver point_numbers} (spine only)
        excluded_shots = 0                 # GT in-play shots silver marked exclude_d
        missing_shots = 0                  # GT shots with no silver row at that ts
        for ts, tp in shot_true.items():
            r = spine_ts.get(ts)
            if r is not None and r["point_number"] is not None:
                sp_true[r["point_number"]].add(tp)
                true_sp[tp].add(r["point_number"])
            elif ts in excl_ts:
                excluded_shots += 1
            else:
                missing_shots += 1

        true_points = sorted(set(shot_true.values()))
        covered = [tp for tp in true_points if true_sp.get(tp)]
        dropped = [tp for tp in true_points if not true_sp.get(tp)]  # all shots excluded/missing
        exact = [tp for tp in covered
                 if len(true_sp[tp]) == 1 and len(sp_true[next(iter(true_sp[tp]))]) == 1]
        merges = {sp: sorted(t) for sp, t in sp_true.items() if len(t) > 1}
        splits = {tp: sorted(s) for tp, s in true_sp.items() if len(s) > 1}

        gtwin = gt["point_winner"]  # true_point(str) -> winner id
        agree, total, mism = 0, 0, []
        for tp in exact:
            g = gtwin.get(str(tp))
            if g is None:
                continue
            sv = sp_winner.get(next(iter(true_sp[tp])))
            total += 1
            if g == sv:
                agree += 1
            else:
                mism.append((tp, next(iter(true_sp[tp])), g, sv))
        out.update({
            "true_points": len(true_points),
            "exact_1to1": len(exact),
            "merges": len(merges), "splits": len(splits),
            "dropped_points": len(dropped),
            "excluded_shots": excluded_shots, "missing_shots": missing_shots,
            "winner_agree": agree, "winner_total": total,
            "winner_pct": round(100 * agree / total, 1) if total else None,
            "_merges": {str(k): v for k, v in sorted(merges.items())},
            "_splits": {str(k): v for k, v in sorted(splits.items())},
            "_dropped": dropped,
            "_winner_mismatch": mism,
        })
    else:  # identity — score winners by point_number
        gtwin = gt["point_winner"]  # point_number(str) -> winner id
        agree, total, mism = 0, 0, []
        for pn_str, g in gtwin.items():
            sv = sp_winner.get(int(pn_str))
            total += 1
            if g == sv:
                agree += 1
            else:
                mism.append((int(pn_str), int(pn_str), g, sv))
        out.update({
            "true_points": len(gtwin),
            "winner_agree": agree, "winner_total": total,
            "winner_pct": round(100 * agree / total, 1) if total else None,
            "_winner_mismatch": mism,
        })
    out.update(_score_outcomes(gt, rows, spine))
    return out


def _score_outcomes(gt: dict, rows: list, spine: list) -> dict:
    """Score DF / ace / net-error derivation vs the outcome GT (if present).

    Recall is scored against the owner's explicit annotations. False positives
    (silver flags an event GT doesn't) are counted and baselined — they must not
    INCREASE, but on a partial-GT match they are not treated as auto-fail.
    On a complete-GT match (c8b77210) the baseline FP is 0, so it gates precision.
    """
    og = gt.get("outcome_gt")
    if not og:
        return {}
    sv_df = {r["point_number"] for r in spine if r.get("double_fault_d")}
    sv_ace = {r["point_number"] for r in spine if r.get("ace_d")}
    out: dict = {}

    if gt["boundary_mode"] == "shot_map":
        ts_row = {float(r["hs"]): r for r in rows if r["hs"] is not None}

        def _pt(ts):
            r = ts_row.get(round(float(ts), 3))
            return r["point_number"] if r and r["point_number"] is not None else None

        df_shots = og.get("double_fault_shots", {})
        ace_shots = og.get("ace_shots", {})
        df_gt_pts = {p for p in (_pt(k) for k in df_shots) if p is not None}
        ace_gt_pts = {p for p in (_pt(k) for k in ace_shots) if p is not None}
        df_caught = sum(1 for k in df_shots if _pt(k) in sv_df)
        ace_caught = sum(1 for k in ace_shots if _pt(k) in sv_ace)

        ne = og.get("net_error_shots", {})
        ne_caught = 0
        ne_miss = []
        for k in ne:
            r = ts_row.get(round(float(k), 3))
            if r and str(r["shot_outcome_d"]) == "Error":
                ne_caught += 1
            else:
                ne_miss.append((k, r["shot_outcome_d"] if r else None))

        out.update({
            "df_caught": df_caught, "df_total": len(df_shots), "df_fp": len(sv_df - df_gt_pts),
            "ace_caught": ace_caught, "ace_total": len(ace_shots), "ace_fp": len(sv_ace - ace_gt_pts),
            "neterr_caught": ne_caught, "neterr_total": len(ne),
            "_df_fp_points": sorted(sv_df - df_gt_pts),
            "_ace_fp_points": sorted(sv_ace - ace_gt_pts),
            "_neterr_miss": ne_miss,
        })
    else:  # identity — GT keyed by point_number
        df_gt = set(og.get("double_fault_points", []))
        ace_gt = set(og.get("ace_points", []))
        out.update({
            "df_caught": len(df_gt & sv_df), "df_total": len(df_gt), "df_fp": len(sv_df - df_gt),
            "ace_caught": len(ace_gt & sv_ace), "ace_total": len(ace_gt), "ace_fp": len(sv_ace - ace_gt),
            "_df_fp_points": sorted(sv_df - df_gt),
            "_ace_fp_points": sorted(sv_ace - ace_gt),
        })
    return out


# ---------------------------------------------------------------------------
# Baseline compare — the both-match anti-overfit gate
# ---------------------------------------------------------------------------
# For each of these keys, a candidate that moves the metric in the "bad"
# direction beyond tolerance is a regression. Boundaries: fewer exact / more
# merges|splits = worse. Winners: fewer agree = worse.
_LOWER_IS_WORSE = {"exact_1to1", "winner_agree", "winner_pct",
                   "df_caught", "ace_caught", "neterr_caught"}
_HIGHER_IS_WORSE = {"merges", "splits", "dropped_points", "df_fp", "ace_fp"}


def _regressions(cur: dict, base: dict) -> list[str]:
    regs = []
    for k in _LOWER_IS_WORSE | _HIGHER_IS_WORSE:
        if k not in base or base[k] is None:
            continue
        c, b = cur.get(k), base.get(k)
        if c is None:
            regs.append(f"{k}: baseline {b}, got None")
            continue
        if k in _LOWER_IS_WORSE and c < b:
            regs.append(f"{k}: {c} < baseline {b} (dropped)")
        if k in _HIGHER_IS_WORSE and c > b:
            regs.append(f"{k}: {c} > baseline {b} (grew)")
    return regs


def _print_card(s: dict, show_diff: bool) -> None:
    t = s["task_id"][:8]
    print(f"\n=== {t}  {s.get('match','')} ===")
    if "exact_1to1" in s:
        print(f"  boundaries : silver {s['silver_points']} pts vs GT {s['true_points']} true pts  |  "
              f"exact 1:1 {s['exact_1to1']}  merges {s['merges']}  splits {s['splits']}  "
              f"dropped {s['dropped_points']}")
        print(f"  shot fate  : silver-excluded {s['excluded_shots']}  missing {s['missing_shots']} "
              f"(GT in-play shots silver didn't keep on the spine)")
    else:
        print(f"  points     : silver {s['silver_points']}  vs GT {s['true_points']}")
    wt, wa = s["winner_total"], s["winner_agree"]
    print(f"  winners    : {wa}/{wt} = {s['winner_pct']}%  ({wt - wa} wrong)")
    if "df_total" in s:
        nt = s.get("neterr_total")
        ne = f"  net-err recall {s['neterr_caught']}/{nt}" if nt else ""
        print(f"  outcomes   : DF recall {s['df_caught']}/{s['df_total']} (+{s['df_fp']} FP)  "
              f"ace recall {s['ace_caught']}/{s['ace_total']} (+{s['ace_fp']} FP){ne}")
    if show_diff:
        if s.get("_df_fp_points"):
            print(f"       DF false-positive silver points : {s['_df_fp_points']}")
        if s.get("_ace_fp_points"):
            print(f"       ace false-positive silver points: {s['_ace_fp_points']}")
        if s.get("_neterr_miss"):
            print(f"       net-error misses (ts, silver outcome): {s['_neterr_miss']}")
        if s.get("_merges"):
            print("  -- merges (silver pt -> true points) --")
            for sp, tps in s["_merges"].items():
                print(f"       silver {sp} = true {tps}")
        if s.get("_splits"):
            print("  -- splits (true pt -> silver pts) --")
            for tp, sps in s["_splits"].items():
                print(f"       true {tp} = silver {sps}")
        if s.get("_dropped"):
            print(f"  -- dropped true points (all shots excluded/missing) -- {s['_dropped']}")
        if s.get("_winner_mismatch"):
            print("  -- winner disagreements (true_pt, silver_pt, GT_id, silver_id) --")
            for m in s["_winner_mismatch"]:
                print(f"       {m}")


def _strip_private(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


def run(db: str, db_url: str | None, show_diff: bool, update_baseline: bool) -> int:
    from sqlalchemy import create_engine
    url = _resolve_db(db, db_url)
    eng = create_engine(url, future=True)

    gts = _load_gts()
    if not gts:
        print(f"no GT files in {GT_DIR} (recon_*.json)")
        return 0

    results = {}
    with eng.connect() as conn:
        who = conn.execute(__import__("sqlalchemy").text("select current_user")).scalar()
        print(f"recon_bench  db={db}  as={who}")
        for gt in gts:
            s = score_match(conn, gt)
            results[gt["task_id"][:8]] = s
            _print_card(s, show_diff)

    if update_baseline:
        baseline = {k: _strip_private(v) for k, v in results.items()}
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2))
        print(f"\n[updated baseline] {BASELINE_PATH}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"\nno baseline at {BASELINE_PATH} — run --update-baseline to lock the current numbers")
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    total_regs = 0
    print("\n=== gate (vs locked baseline) ===")
    for short, cur in results.items():
        base = baseline.get(short)
        if not base:
            print(f"  {short}: no baseline entry (new match — lock with --update-baseline)")
            continue
        regs = _regressions(cur, base)
        if regs:
            total_regs += len(regs)
            for r in regs:
                print(f"  [!] {short}: {r}")
        else:
            print(f"  [OK] {short}: no regression")
    return 1 if total_regs else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Silver point-level reconciliation bench (winners + boundaries vs video GT).")
    ap.add_argument("--db", default="prod", choices=["prod", "devenv"],
                    help="DB source (default prod = tf_readonly)")
    ap.add_argument("--db-url", default=None, help="explicit DB URL override")
    ap.add_argument("--diff", action="store_true", help="print per-point merges/splits/winner diffs")
    ap.add_argument("--update-baseline", action="store_true", help="lock current numbers as the baseline")
    args = ap.parse_args(argv)
    return run(args.db, args.db_url, args.diff, args.update_baseline)


if __name__ == "__main__":
    sys.exit(main())
