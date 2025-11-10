# build_silver_all.py — single-file runner for Silver P1–P3 (and beyond)
# Uses the current entrypoint: build_silver(task_id, phase, replace)

from build_silver_point_detail import build_silver

if __name__ == "__main__":
    import argparse, json

    p = argparse.ArgumentParser(
        description="Run Silver builder (P1–P3 or chosen phase) for a task_id"
    )
    p.add_argument("--task-id", required=True, help="Task UUID")
    p.add_argument(
        "--phase",
        choices=["1", "2", "3", "4", "5", "all"],
        default="all",
        help="Which phase(s) to run (default: all)",
    )
    p.add_argument("--replace", action="store_true", help="Delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()

    print(f"🔹 Running Silver build — phase={args.phase}, replace={args.replace}")
    out = build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)
    print(json.dumps(out, indent=2, sort_keys=True))
    print("✅ Silver build complete.")
