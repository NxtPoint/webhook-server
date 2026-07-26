# silver_analytics — dashboard data layer (non-point-detail grains)

Turns the ~90% of bronze that `build_silver_v2` never reads (`player`,
`player_position`, `session_confidences`, `team_session`) into three new
`silver.*` grains that feed the *insight* dashboards. **Does not touch
`silver.point_detail`** (the 18/18-validated shot layer) — new grains get new
tables.

## Tables (built by `build_all(engine, task_id)`)

| table | grain | source | powers |
|---|---|---|---|
| `silver.match_player_summary` | player × match | `bronze.player` (+ confidences / team_session) | fitness (distance / top sprint / activity), shot-mix, near/far |
| `silver.player_movement_grid` | player × 1 m court cell | `bronze.player_position` (de-ghosted, GROUP BY cell) | movement heatmap, court coverage — **pre-aggregated (~150 cells/player, not ~3000 raw rows)** so charts stay fast |
| `silver.match_quality` | match | `bronze.session_confidences` | ball/pose/swing/final confidence → reliability `quality_tier` |

## Design rules (load-bearing)

- **Aggregate in SQL, not Python** (rule #2): every builder is `INSERT … SELECT …
  GROUP BY`; Python just gets a rowcount. Negligible worker memory.
- **Identity parity**: reuse `build_silver_v2._resolve_two_players` so the same two
  real players map here as everywhere else (drops the 3–4 phantom ids).
- **Idempotent**: DELETE-then-INSERT keyed on `(task_id, model)`; each builder runs
  in its own `engine.begin()` so one failing can't poison the others.
- **Best-effort in ingest**: wired at `ingest_worker_app._do_ingest` STEP 3b (after
  bronze + silver). A failure logs `analytics/skipped` and never fails the ingest.
- Constants at top: `GRID_M=1.0`, `HALF_Y=11.885`.

## Footgun

SQLAlchemy bind-param parsing chokes on `:t::uuid` — use `CAST(:t AS uuid)`.

## Open item

**`quality_tier` thresholds need calibration.** Measured 2026-07-26: both the good
match (`c8b77210`, ball 0.30) and the badly-tracked one (`df594aea`, ball 0.29)
read `medium` — the tiers don't yet discriminate. The bad match should read `low`.
See `docs/_investigation/silver_recon_bench_plan.md`.

## Validated

First real prod run on `df594aea` (2026-07-26): 2 player summaries, 762 grid cells,
1 quality row. Roadmap for the dashboards on top: `.claude/plans/twinkly-seeking-bentley.md`.
