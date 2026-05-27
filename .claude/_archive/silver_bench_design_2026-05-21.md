# Silver-Builder Bench Harness — Design Spec (2026-05-21)

**Audience:** the future session that builds this. **Status:** spec only, not implemented.
**Why this exists separately:** the infra audit (`infrastructure_audit_2026-05-20.md` §8 item #2) estimated "1 session of build" but that was optimistic. Silver builder is 5 passes of Postgres SQL — running it offline needs a local DB. This spec captures the architecture so the build itself is a focused 2-3 session task instead of design + build mashed together.
**Author:** Claude session 2026-05-21 PM. Written *after* the ball-tracker bench scaffolding shipped (see commits `0d9c9ee`, `4867ccc`, `0546278`); mirrors that bench's shape where applicable.

---

## TL;DR

| Question | Answer |
|---|---|
| What does this bench test? | That a `build_silver_v2.py` (or `build_silver_match_t5.py`) code change doesn't regress silver row counts, stroke distributions, exclusion counts, or serve detections vs locked baselines on a fixture set. |
| Where does the silver actually *run*? | Local Docker Postgres. Spun up per bench run, bronze fixture restored, silver builder runs in-process against it, results queried back out, container torn down. |
| Sub-second? | Container spin-up + restore is the slow part (~10-30s). Silver run itself is ~1-5s. Steady-state bench is ~30s per fixture. Worth it vs the 5-10 min Render round-trip. |
| Why not just compare prod silver to baseline? | Considered (§5 below). Rejected because it doesn't catch regressions *before* push — you'd still push broken code to prod, then bench tells you. Defeats the local-iteration purpose. |
| When can this be baselined? | NOW. Phase 5a Step F has landed (commit `7d8bfaa`); silver row count on `880dff02` is at its new post-5a value. The sequencing caveat in the infra audit §9 is satisfied. |
| Why not build it tonight? | Cognitive-load reasons (two parallel agents already in flight) — see `next_session_pickup.md`. Build in a dedicated future session. |

---

## 1. Problem statement

Today, silver-builder iteration is:

```
edit build_silver_v2.py
  → git push origin main
  → wait ~5 min for Render auto-deploy
  → SSH into Render shell
  → python -m ml_pipeline.harness rerun-silver <task_id>
  → query DB to see what changed
  → repeat
```

Each cycle is 5-10 min. **Phase 3 part 2 was reverted twice** (commits `00b8639` and `f0b104e` per north_star.md §"Phase 3"). Both reverts were caused by silver logic that looked right but emitted wrong row counts in production. A local bench that ran the silver builder against a cached fixture and compared to baseline expectations would have caught each in seconds, before push.

**Goal:** convert silver-builder iteration to:

```
edit build_silver_v2.py
  → python -m ml_pipeline.diag.bench_silver
  → see green or [!] REGRESSION in seconds
  → push only if green
```

Same shape as the serve-detector bench in `ml_pipeline/diag/bench.py`.

---

## 2. Architecture

### Two halves

1. **Snapshot tooling** — one-time per fixture, captures bronze + silver state of a known-good task to portable artefacts.
2. **Bench tooling** — per-edit, spins up a local DB, runs silver against the fixture, compares to baseline, exits 0 or 1.

### Storage layers

| Artefact | Location | What it is |
|---|---|---|
| Bronze fixture | `ml_pipeline/fixtures_silver/<task_short>_bronze.sql.gz` (gitignored) + S3 backup at `s3://nextpoint-prod-uploads/fixtures/silver/` | `pg_dump`-style restorable dump of `bronze.*` + `ml_analysis.*` tables for one task, scoped to that task's rows only |
| Silver baseline | `ml_pipeline/fixtures_silver/<task_short>_silver_baseline.json` (in-git) | The expected silver state after running silver builder against the bronze fixture. Schema in §3 |
| Aggregate baseline | `ml_pipeline/diag/bench_silver_baseline.json` (in-git, like `bench_baseline.json`) | Per-fixture summary numbers — what bench compares against |

### Why pg_dump (not pickle)?

`bench.py` (serve detector) uses pickle because the serve detector consumes Python objects (`BallDetection`, `PoseFrame`, etc) directly. Silver builder consumes SQL tables. The fixture has to be re-loadable into a Postgres instance.

`pg_dump --table=bronze.X --table=ml_analysis.X ... --data-only ... | gzip` produces a portable artefact that `pg_restore` (or `gunzip | psql`) loads into a clean local DB. ~1-5 MB per task (rows-only, no schema — schema lives in `db_init.py` and gets re-run on the test DB).

### Why Docker Postgres (not in-memory)?

`build_silver_v2.py` uses:
- Window functions (`row_number()`, `lag()`, `lead()`)
- CTEs with recursive references
- `JSONB` operators
- Custom aggregations
- Possibly array operations

None of these survive a translation to SQLite or DuckDB without rewriting the silver builder. The bench has to test the *real* SQL. Docker Postgres on the same major version as production is the only honest path.

---

## 3. Baseline JSON schema

```json
{
  "task_id": "880dff02-58bd-412c-9a29-5c5151004447",
  "captured_at": "2026-05-21T22:00:00Z",
  "captured_after_commit": "7d8bfaa",
  "model": "t5",
  "expected": {
    "row_count_total": 183,
    "row_count_active": 49,
    "row_count_excluded": 134,
    "serve_count_active": 19,
    "serve_count_total": 24,
    "stroke_distribution_active": {
      "Forehand": 21,
      "Backhand": 10,
      "Serve": 19,
      "Volley": 0,
      "Slice": 0,
      "Overhead": 0,
      "Other": 0
    },
    "outcome_distribution": {
      "Winner": 3,
      "Error": 5,
      "In": 41
    },
    "depth_distribution_active": {
      "Deep": 22,
      "Middle": 21,
      "Short": 6
    },
    "first_serve_ts_s": 4.96,
    "last_serve_ts_s": 612.5
  },
  "tolerance": {
    "row_count_total": {"abs": 0, "comment": "exact — silver builder is deterministic given fixed bronze"},
    "row_count_active": {"abs": 0},
    "stroke_distribution_active": {"per_class_pct": 5}
  }
}
```

**Tolerance defaults to exact-match** because silver is deterministic — same bronze in, same silver out. The `tolerance` field lets specific cases loosen this (e.g. floating-point coordinate comparisons).

The aggregate `bench_silver_baseline.json` would aggregate per-fixture verdicts the same way `bench_baseline.json` aggregates serve fixtures.

---

## 4. CLI surface

Mirrors the serve bench almost exactly.

### Once-per-task setup (capture a fixture)

Run **on Render shell** (needs DATABASE_URL):

```bash
python -m ml_pipeline.diag.snapshot_silver --task <T5_TID> --output ml_pipeline/fixtures_silver/<TID8>_bronze.sql.gz
```

Implementation: `pg_dump --data-only --table=bronze.submission_context --table=bronze.player_swing --table=bronze.rally --table=bronze.ball_bounce --table=bronze.ball_position --table=bronze.player_position --table=ml_analysis.video_analysis_jobs --table=ml_analysis.ball_detections --table=ml_analysis.player_detections --table=ml_analysis.court_detections --table=ml_analysis.serve_events --where "task_id='<task>'"` (or job_id equivalent). Filter to one task's rows. Gzip output.

Also captures the silver baseline by querying current `silver.point_detail WHERE task_id` and writing JSON.

```bash
python -m ml_pipeline.diag.snapshot_silver --task <T5_TID> --silver-baseline ml_pipeline/fixtures_silver/<TID8>_silver_baseline.json
```

Upload both to S3 for portability:

```bash
aws s3 cp ml_pipeline/fixtures_silver/<TID8>_bronze.sql.gz s3://nextpoint-prod-uploads/fixtures/silver/
aws s3 cp ml_pipeline/fixtures_silver/<TID8>_silver_baseline.json s3://nextpoint-prod-uploads/fixtures/silver/
```

### Per-edit cycle

On local (Linux/Mac/Windows-WSL/Git-Bash; Docker required):

```bash
# Spin up local Postgres + load all fixtures (idempotent; reuses container if alive)
python -m ml_pipeline.diag.bench_silver --setup

# Run bench across all fixtures
python -m ml_pipeline.diag.bench_silver

# If green: lock new baseline + commit
python -m ml_pipeline.diag.bench_silver --update-baseline
git add ml_pipeline/diag/bench_silver_baseline.json
git commit -m "bench_silver: lock new baseline at <commit_short>"

# Tear down
python -m ml_pipeline.diag.bench_silver --teardown
```

### Per-fixture deep dive

When the bench shows a regression and you need to see WHAT changed:

```bash
python -m ml_pipeline.diag.bench_silver --task <TID> --diff
```

Output: row-level diff of silver.point_detail current vs baseline. Like `git diff` but for silver rows. Shows added rows, removed rows, changed rows with per-column delta.

---

## 5. Alternative considered: "push-then-compare"

A simpler bench would skip Docker Postgres entirely:

1. After every push that touches `build_silver_v2.py`, manually `harness rerun-silver` on Render
2. Bench reads current prod silver via `/ops/diag/sql`
3. Compare to `bench_silver_baseline.json` expectations
4. Fail if drift detected

**Why rejected:**

- Doesn't catch regressions BEFORE push. Phase 3 part 2 reverts were both caught only after pushing broken code and seeing customer-facing data look wrong.
- Render rerun-silver is ~30s; not 5-10 min as I claimed before, but still slower than local Docker would be once warm.
- The CI workflow (`.github/workflows/bench.yml`) would need DATABASE_URL secret + ops-key — adds attack surface for a CI runner that historically has zero secrets.
- Doesn't allow parallel exploration ("what if I tried X, then Y, then Z" all locally without touching prod state).

**Keep as a fallback**: `bench_silver --remote` could read prod silver and compare to baseline, useful as an integration test after deploy. But that's downstream of the real bench.

---

## 6. CI integration

Extend `.github/workflows/bench.yml`:

```yaml
jobs:
  serve_bench:  # existing job
    ...
  silver_bench:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: bench
        ports:
          - 5432:5432
        options: --health-cmd pg_isready --health-interval 10s
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r ml_pipeline/diag/requirements-bench.txt
      - run: aws s3 cp s3://nextpoint-prod-uploads/fixtures/silver/ ./ml_pipeline/fixtures_silver/ --recursive
      - run: python -m ml_pipeline.diag.bench_silver
```

Trigger globs: `build_silver_v2.py`, `build_silver_match_t5.py`, `build_silver_practice.py`, `ml_pipeline/diag/bench_silver*`, `ml_pipeline/fixtures_silver/*.json`.

S3 read on the CI runner: GitHub Actions OIDC + a dedicated IAM role (read-only on `fixtures/silver/`). Setup: ~30 min, one-time.

---

## 7. Build sequence (the actual session plan)

| Step | Output | Estimated effort |
|---|---|---|
| 1 | Module skeleton: `ml_pipeline/diag/bench_silver/__init__.py` + `snapshot.py` + `bench.py` + `requirements-bench.txt` updated for `psycopg`/`docker-py` if needed | 30 min |
| 2 | `snapshot.py`: pg_dump-style export of bronze + ml_analysis rows for one task; writes `.sql.gz` + `_silver_baseline.json` | 1-2 hrs |
| 3 | Local Docker Postgres setup helper (spin up, load schema via `db_init.py`, restore fixture, return connection string) | 1-2 hrs |
| 4 | `bench.py`: orchestrator — load fixtures, run `build_silver_v2.run_passes()` against local DB, query results, compare to baseline JSON, emit verdict | 2-3 hrs |
| 5 | Capture first fixture for `880dff02` (post-5a state) → upload to S3 → commit baseline | 30 min |
| 6 | Capture second fixture for `a798eff0` → same | 30 min |
| 7 | Wire CI workflow + test PR | 1 hr |

**Total: ~7-10 hours of focused work.** Realistically: 2-3 working sessions.

**Critical path:** step 3 (Docker Postgres setup) is the trickiest. Spend the first session on it alone. The fixture format + bench orchestrator can mirror `snapshot_task.py` + `bench.py` patterns from the serve bench.

---

## 8. Open questions

1. **`build_silver_v2.run_passes()` doesn't currently exist** as a callable. Today it's a SQL file or script. The build session may need to refactor `build_silver_v2.py` to expose a programmatic entry point that takes a DB connection. Should be small (the script's body becomes a function).
2. **Schema initialization for the local DB.** `db_init.py::bronze_init()` is the right starter, but it pulls from the live `DATABASE_URL`. Need to verify it works against a fresh local DB with just env var redirected. Probably fine but test.
3. **`silver.point_detail` schema** also needs creation on the local DB. Check that `db_init.py` or `gold_init.py` create it idempotently with `IF NOT EXISTS` — looks like yes per CLAUDE.md schema-DDL section, but confirm.
4. **Bronze tables `bronze_export.py` writes** vs schema in `db_init.py` — confirm the same DDL exists on both sides.
5. **Practice silver** (`build_silver_practice.py`) deserves the same bench? Probably yes, but defer to a follow-up — match silver is the more-used path.
6. **T5 vs SportAI silver in the same fixture** — `silver.point_detail` is multi-model. Bench needs to handle `model='t5'` and `model='sportai'` rows separately. Keep them in the same fixture file or split?

---

## 9. When NOT to build this

- If WASB integration lands and dramatically improves Phase 5 — the silver bench's primary justification (catch silver-builder regressions) loses urgency because silver-builder isn't actively changing.
- If the project pivots away from T5 silver (unlikely in current direction).
- If a fresh design replaces `build_silver_v2.py` entirely (e.g. moves silver derivation out of SQL into Python) — then the bench has to be redesigned anyway.

In short: the bench is leverage when silver builder is being actively edited. If silver builder enters a stable period, build the bench WHEN the next edit is queued, not preemptively.

---

## 10. Cross-doc references

- `infrastructure_audit_2026-05-20.md` §8 item #2: the original "build this" recommendation. **Update that doc when this is built.**
- `infrastructure_audit_2026-05-20.md` §9 sequencing caveat: explicitly says baseline AFTER Phase 5a Step F. 5a IS shipped (commit `7d8bfaa`), so the gate is open.
- `ml_pipeline/diag/bench.py`: the serve-detector bench. **Mirror its shape.**
- `ml_pipeline/diag/snapshot_task.py`: the serve-detector snapshot tool. **Mirror its shape.**
- `.claude/handover_t5.md` §"TEST HARNESS": describes the serve bench architecture. Read before designing.
- `build_silver_v2.py`: the 5-pass silver builder — the thing under test.
- North star §"Phase 3": history of the two Phase 3 part 2 reverts that motivated this bench.

---

## 11. Bootstrapping playbook — capture the first fixture

Snapshot + bench orchestrator implemented 2026-05-22 (commit pending). Status:

- ✅ `db_helper.py` — Docker Postgres lifecycle (verified 2026-05-21)
- ✅ `snapshot.py` — bronze + ml_analysis dump + silver baseline JSON
- ✅ `bench.py` — orchestrator (restore → run silver → compare to baseline)
- ⏳ First fixture — **needs a Render-shell run** to capture from production

### Step 1 — on Render shell, capture for 880dff02

```bash
# Inside Render shell (any service that has DATABASE_URL — e.g. webhook-server)
cd ~/project   # adjust if the working dir differs
python -m ml_pipeline.diag.bench_silver.snapshot \
    --task 880dff02-58bd-412c-9a29-5c5151004447

# Verify the output:
ls -la ml_pipeline/fixtures_silver/
# Expect: 880dff02_bronze.sql.gz (~1-5 MB), 880dff02_silver_baseline.json (~2 KB)

# Upload to S3:
python -c "
import boto3
s3 = boto3.client('s3')
for f in ['880dff02_bronze.sql.gz', '880dff02_silver_baseline.json']:
    s3.upload_file(f'ml_pipeline/fixtures_silver/{f}',
                   'nextpoint-prod-uploads',
                   f'fixtures/silver/{f}')
    print(f'uploaded {f}')
"
```

### Step 2 — repeat for a798eff0 (the second locked fixture)

```bash
python -m ml_pipeline.diag.bench_silver.snapshot \
    --task a798eff0-...  # TODO confirm full task_id from production
# Same upload step
```

### Step 3 — locally, pull + bench

```bash
# Pull both fixtures from S3 to local checkout
aws s3 cp s3://nextpoint-prod-uploads/fixtures/silver/ \
          ml_pipeline/fixtures_silver/ --recursive

# Spin up bench Postgres + run
.venv/Scripts/python -m ml_pipeline.diag.bench_silver --setup
.venv/Scripts/python -m ml_pipeline.diag.bench_silver

# Expect green for each fixture (silver builder is deterministic given fixed bronze).
```

If green, the baseline JSON is correct — commit it (the `.sql.gz` stays gitignored per `.gitignore`):

```bash
git add ml_pipeline/fixtures_silver/880dff02_silver_baseline.json \
        ml_pipeline/fixtures_silver/a798eff0_silver_baseline.json
git commit -m "silver bench: lock baselines from production capture"
```

If red on first run, something's drifted between the captured bronze and the current silver builder logic — investigate with `--update-baseline` after confirming the new state is correct.

### Open follow-ups (not blocking step 1)

- CI integration per §6 of this doc — add a job to `.github/workflows/bench.yml` once baselines are committed. Needs IAM role for S3 read on the CI runner.
- The `tolerance` field defaults to exact-match for row counts; verify that holds in production captures before locking strict.
- Practice silver (`build_silver_practice.py`) deserves a parallel bench — defer until the match-silver one proves itself.
