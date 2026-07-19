# devenv — disposable local Postgres seeded with real bronze data

**Nothing here is deployed.** It exists so pipeline changes can be validated against real
matches without touching prod.

## Why

The 2026-07-19 audit (`docs/_investigation/pipeline_end_to_end_audit_2026-07-19.md`) queues up
changes to silver *derivation* — serve legality, court geometry, zone boundaries. Those rewrite
every derived number, so the only responsible way to ship them is: rebuild silver, diff every
derived field before/after on real matches, repeat. That loop cannot run against prod, and it
cannot run against synthetic data either (the bugs live in real coordinate edge cases).

## Safety model

- The **source** connection is only ever read from. Use a **read-only Postgres role**:

  ```sql
  CREATE ROLE tf_readonly LOGIN PASSWORD '<pw>';
  GRANT CONNECT ON DATABASE <db> TO tf_readonly;
  GRANT USAGE ON SCHEMA bronze TO tf_readonly;
  GRANT SELECT ON ALL TABLES IN SCHEMA bronze TO tf_readonly;
  ```

  Read-only is then enforced by Postgres, not by a script's good behaviour. Drop the role when
  the work is done. This is deliberately far narrower than `OPS_KEY`, which unlocks ~27 endpoints
  including deletion, GDPR erasure, `VACUUM FULL` and AWS Batch job submission.
- The **target** defaults to `localhost:55433` and `seed_local.py` refuses to write to anything
  hosted, anything non-local, or to `:55432` (the CourtFlow local DB this box already points at).
- The container is throwaway: `docker compose down -v` wipes it.

## Use

```bash
docker compose -f devenv/docker-compose.yml up -d

export RO_URL='postgresql://tf_readonly:<pw>@<host>/<db>'

python -m devenv.seed_local --source-url "$RO_URL" --list        # browse candidates
python -m devenv.seed_local --source-url "$RO_URL" --newest 3    # seed newest 3 matches
python -m devenv.seed_local --source-url "$RO_URL" --task <uuid> # or name them
```

Then work against the local DB:

```bash
export DATABASE_URL='postgresql+psycopg://tf:tf@localhost:55433/tf_dev'
python -c "import build_silver_v2 as b; print(b.build_silver_v2('<task>', replace=True))"
python -c "import gold_init; gold_init.gold_init_presentation()"
```

## What to seed

Pick matches that exercise the known failure modes, not just the newest three:

| want | why |
|---|---|
| a clean, well-detected match | baseline — changes here should be small and explainable |
| a match with several double faults | exercises the `'Double'` relabel and the first-serve-% denominator |
| a match with poor far-side detection | exercises NULL `court_x`/`ball_hit_location_y` paths (fabricated `body`, inverted `depth_d`) |
| a T5 task (`tennis_singles_t5`) | the coordinate frame differs from SportAI — needed to keep both honest |

## Notes

- `seed_local.py` builds the bronze schema by calling the repo's own `db_init.bronze_init()`, so
  the local shape cannot drift from production's.
- GENERATED columns (`ball_bounce.court_x/court_y/image_x/image_y`) are filtered out of the INSERT
  automatically — Postgres rejects any INSERT that supplies them.
- Seeding is idempotent per task: rows for a task are deleted before re-insert.
