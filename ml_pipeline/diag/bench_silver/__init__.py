"""Silver-builder bench harness — Docker-Postgres-backed regression check
for `build_silver_v2.py` and friends.

Design spec: `.claude/strategy/silver_bench_design_2026-05-21.md`.

Components:

  db_helper.py — spin up + tear down an ephemeral Docker Postgres container
                 for bench runs (CRITICAL PATH — built first).
  snapshot.py  — capture a task's bronze + ml_analysis state to a portable
                 `.sql.gz` fixture + silver baseline JSON. STUB / next session.
  bench.py     — orchestrator: load fixtures, run silver builder, compare
                 to baseline, emit verdict. STUB / next session.

CLI (mirrors `ml_pipeline.diag.bench` for the serve detector):

  python -m ml_pipeline.diag.bench_silver --setup           # start local PG
  python -m ml_pipeline.diag.bench_silver                   # run + compare
  python -m ml_pipeline.diag.bench_silver --update-baseline # lock baseline
  python -m ml_pipeline.diag.bench_silver --teardown        # stop PG

Status: scaffolding only as of 2026-05-21. See pickup doc for what's left.
"""
