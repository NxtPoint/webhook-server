# build_silver_all.py — single-file version - RESTING ONLY - DELETE LATER
from build_silver_point_detail import build_point_detail, build_point_detail_phase2

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Run Phase 1 + Phase 2 sequentially for a task_id")
    p.add_argument("--task-id", required=True)
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()

    print("🔹 Phase 1 — Bronze → Silver base fields")
    out1 = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out1)

    print("🔹 Phase 2 — Derived logic (serve, side, sequencing, play_d)")
    out2 = build_point_detail_phase2(task_id=args.task_id)
    print(out2)

    print("✅ Full Silver refresh complete.")
