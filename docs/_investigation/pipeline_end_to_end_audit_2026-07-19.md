# End-to-end pipeline audit — SportAI JSON → bronze → silver → gold → dashboard

**Date:** 2026-07-19 · **Scope:** the whole SportAI (`tennis_singles`) analytics path · **Method:** five parallel read-only code audits, one per layer, with every sharp claim re-verified at the line level before inclusion here. **No database was contacted** (see "Validation still owed").

**Verdict: the pipeline is structurally sound but analytically wrong in specific, fixable places.** Data moves through the layers without loss of *rows*; what is broken is (a) the correctness of several derived tennis facts, most seriously serve legality, (b) a family of NULL-becomes-zero bugs that spans all four layers, and (c) an ingest that cannot tell a good result from an empty one, yet bills for both.

Severity key: **P1** = produces a visibly wrong number for a user today · **P2** = wrong under a realistic input · **P3** = latent / hygiene.

---

## P1 — The serve is never checked against the service box

`build_silver_v2.py:945`

```sql
WHEN sp.serve_d IS TRUE AND ABS(sp.court_y - :half_y) <= 1.60 THEN 'Error'
```

This is the complete serve-legality test. It asks one question: *did the ball land within 1.60 m of the net?* There is **no service-line (depth) test** and **no service-box-half (width) test**. The only other bounds applied are the full singles sidelines and the full court length — i.e. the whole opponent half, not the service box.

**Traced failure.** Near-end server, 2nd serve, bounce at `court_x = 5.00, court_y = 4.00`. The far service box is `y ∈ [5.485, 11.885]`, so this serve is **1.485 m long — a fault**, and the point is a double fault.

| test at `:941-947` | result |
|---|---|
| `court_x` 5.00 within `[1.37, 9.60]` | passes |
| `court_y` 4.00 within `[0, 23.77]` | passes |
| `\|4.00 − 11.885\| = 7.885 > 1.60` | passes |
| → falls through to `ELSE` | **`'Winner'`** |

So the long double fault is recorded as a serve winner. `double_pts` never fires; with no return detected `ace_pts` *does* fire, and the point is awarded to the server. **A long double fault is booked as an ace.**

The same predicate accepts a deuce-court serve that lands in the ad box, and it *inverts* on legal short serves: a serve landing 0.885 m past the net (`court_y = 11.0`) is inside the box and legal, but `|11.0 − 11.885| = 0.885 ≤ 1.60` → scored `'Error'`.

**Fix.** Replace with a real box test keyed on `server_end_d` + `serve_side_d`, using the true centre service line at `x = 5.485` and service lines at `y = 5.485 / 18.285`. Retain the `≤ 1.60` net-cord test as an *additional* reject, not the only one.

---

## P1 — First-serve percentage is inflated for exactly the players who serve worst

Two correct-looking pieces of code compose into a wrong metric.

1. `build_silver_v2.py:1331-1335` stamps `serve_try_ix_in_point = 'Double'` on **every row of a double-fault point** — including the row for the *faulted first serve*.
2. `gold_init.py:255` computes the denominator as `COUNT(*) FILTER (WHERE serve_d AND serve_try_ix_in_point = '1st')`.

Every double fault therefore **removes one attempt from the first-serve-% denominator**.

**Traced failure.** 10 first serves, 5 in, 5 faults, 2 of which become double faults. True first-serve-in% = **50%**. Computed: `5/8` = **62.5%**. The error scales with double-fault rate, so the metric flatters the weakest servers. Inherited identically by `gold.player_match_kpis.kpi_first_serve_in_pct` (`gold_init.py:737`), so the Performance Scorecard carries it too.

**Fix.** Apply `'Double'` only to the point's last serve row, or add a separate `double_fault_d` boolean and leave `serve_try_ix_in_point` as 1st/2nd. Denominator should be distinct service points.

---

## P1 — Deuce/ad is decided by a data-derived midline, not the centre mark

`build_silver_v2.py:497-511` computes `mid = AVG(ball_hit_location_x)` over all detected serve hits, and `:604-606` splits deuce/ad on it. The real centre service line is fixed at **x = 5.485**; it is absent from `SPORT_CONFIG` entirely.

**Traced failure.** A server standing at x ≈ 5.70 on deuce and x ≈ 4.20 on ad, with a 2:1 detected deuce:ad ratio (detection and faults are never balanced) gives `mid = 5.20`. An ad serve struck at `x = 5.35` — genuinely left of the centre mark — evaluates `5.35 > 5.20` → **`'deuce'`**.

That single flip is not cosmetic: `point_number` increments whenever `serve_side` changes (`:645-651`), so a mislabelled side **splits one point into two or merges two into one**, corrupting `rally_length`, `point_winner` and everything keyed on `point_key`.

**Related (same root):** deuce serve-location bins are anchored on the fixed sideline while ad bins are anchored on the floating `mid` (`:1099-1116`), so deuce and ad wide/body/T rates are not mirror-symmetric and are not comparable to each other.

**Fix.** Add `centre_service_line_x: 5.485` to `SPORT_CONFIG`; use it for the deuce/ad split and all four location anchors. Keep the dynamic value only as a logged diagnostic.

---

## P1 — Court geometry constants use the wrong axis origin

`build_silver_v2.py:87-88`

| constant | value | true absolute-y value | error |
|---|---|---|---|
| `service_line_m` | 6.40 | **5.485** | 0.915 m |
| `far_service_line_m` | 17.37 | **18.285** | 0.915 m |

The service line *is* 6.40 m from the net — but these are consumed as **absolute y coordinates**, and 17.37 is plainly `23.77 − 6.40`, i.e. measured from the baseline. Both are 0.915 m off.

Everything else in `SPORT_CONFIG` checks out exactly against ITF: court 23.77 × 10.97, singles width 8.23, alleys 1.37, `half_y` 11.885.

The consumer is `shot_phase_d` (`:919-921`), and fixing the constants alone will not fix it — the zone *semantics* are also wrong:

- `y = 24.5` — near player 0.73 m behind his own baseline, an ordinary rally position → classified **`'Rally'`** only because it is outside the court
- `y = 22.0` — 1.77 m inside the baseline → **`'Transition'`**
- `y = 6.50` — classic no-man's-land → **`'Net'`**

"Net" spans 10.97 m of court; "Transition" is applied to a player standing on his own baseline. Since `match_kpi` uses `shot_phase_d IN ('Rally','Transition','Net')` to scope FH/BH speed averages (`gold_init.py:277`), those averages are computed over a mis-defined population.

**Note:** four more geometry numbers live outside `SPORT_CONFIG` despite its docstring claiming to be the single source of truth — aggression 24/26 and depth 18/20 (`:1666-1676`), and the serve net test 1.60 (`:945`).

---

## P1 — A hollow ingest is indistinguishable from a good one, and it bills the customer

Three facts compose:

1. `ingest_bronze_strict()` validates **nothing but the presence of `task_id`** (`ingest_bronze.py:822-825`). The `counts` dict is computed and never checked.
2. The worker writes `last_status='completed'` **unconditionally**, never inspecting the return value (`ingest_worker_app.py:236-244`).
3. Billing gates **only** on `status == 'completed'` (`billing_import_from_bronze.py:175`).

**Traced failure.** SportAI returns HTTP 200 with `{"status":"failed","detail":"court not detected"}`. `_as_list(payload.get("players"))` → `[]`. Every insert returns 0. Ingest reports success, silver builds 0 rows, the video-complete email fires, **a credit is consumed**, and the customer opens an empty dashboard. Nothing anywhere records that the ingest was hollow.

Compounding: `_as_list` returns `[]` for *any* non-list (`:88-90`), so if SportAI ever sends `players` as a **dict** — the same dict-keyed shape it already uses for `player_positions`, which the code explicitly handles at `:861-866` — every player and every swing is silently dropped down this same path.

**Fix.** Raise (or flag `ingest_error`) when `player_swing` and `ball_position` counts are both zero, before anything marks the task complete.

---

## P1 — "Not measured" is rendered as a real `0%` across Match Analytics

`frontend/match_analysis.html:2072`

```js
function pctW(v) { return Math.round(Number(v) || 0); }
```

`gold_init.py:358-442` deliberately emits `NULL` for every rate with a zero denominator (`CASE WHEN ps.pa_service_points > 0 THEN … ELSE NULL END`). `pctW` collapses that to `0`.

**What the user sees.** A player whose service games were all excluded shows **"Service Pts Won — 0%"**, styled as a real value, and `compareBar` awards the head-to-head win to the opponent. The truth is "no service points measured". Applies to 1st Serve %, 2nd Serve %, 1st/2nd Serve Win %, and Rally Pts Won %.

`locker_room.html:1669` already does this correctly (renders `—`); the fix is to match it.

---

## P1 — Serve Strategy totals double-count points

`gold.match_serve_breakdown` emits `points_played = COUNT(DISTINCT s.point_key)` **within** each `(side, bucket, serve_try)` group (`gold_init.py:494`). The frontend re-keys on `side|bucket` only and **sums** across serve_try (`match_analysis.html:1404-1412`).

A point whose 1st serve faults Wide and whose 2nd goes down the T contributes `1` to the Wide row **and** `1` to the T row. A distinct-count is not additive.

**What the user sees.** A server who played 26 service points with 9 first-serve faults sees a Serve Strategy total of **`18/35`** where the truth is `18/26` — and every per-direction win% is therefore computed on an inflated denominator. Same bug in the Outcomes-by-Direction stacked bar.

**Fix.** The view must own this — add a `serve_try`-agnostic point count. Never sum a `COUNT(DISTINCT)` in JS.

---

## P1 — Soft-deleted matches never leave the dashboards

`gold.vw_player` (`gold_init.py:48-49`) filters only `sport_type = 'tennis_singles'` — **no `deleted_at IS NULL`** — and eleven downstream views inherit it. `client_api._gold_one` checks ownership but not deletion.

**What the user sees.** A deleted match vanishes from the sidebar (`vw_client_match_summary` *does* filter, `db_init.py:515`) but `GET /api/client/match/kpi/<task_id>` still serves the full dashboard, and `gold.player_match_kpis` keeps feeding it into the rolling-5 scorecard **forever**. The user cannot remove it.

**Fix.** One line at `gold_init.py:49` closes this across all eleven views. Same edit should admit legacy `sport_type IS NULL` rows, which currently render as all-zero stat cards rather than being excluded (indistinguishable from a failed ingest).

---

## P2 — Selected findings

**A typeless bounce outranks the real floor bounce.** `build_silver_v2.py:424` — `ORDER BY (type = 'floor') DESC, timestamp`. In Postgres `DESC` implies **NULLS FIRST**, and `(NULL = 'floor')` is NULL, so any bounce with `type IS NULL` sorts *ahead* of the true floor bounce regardless of timing. The shot gets the wrong landing coordinate or none — cascading into a false double fault, NULL depth, and a missing heatmap point. Fix: `ORDER BY (COALESCE(type,'') = 'floor') DESC, timestamp`.

**`serve_location` fabricates `body` when the bounce is missing.** `:1095-1096` maps `court_x IS NULL` to bin 2 or 7, which `:1633` buckets as **`body`** — indistinguishable from a measured body serve. Given that bounce recall is the acknowledged weak point of both pipelines, this **systematically biases the entire wide/body/T distribution toward body**. Fix: return NULL.

**`depth_d` inverts when hitter pose is missing.** `:1578-1582` mirrors the bounce on the *hitter's* y; when `ball_hit_location_y IS NULL` it falls through to raw `court_y`. A deep ball (0.5 m inside the far baseline) is classified **`'Short'`** instead of `'Deep'`. The frontend already documents and works around exactly this (`match_analysis.html:2777-2785`), so the chart is right while the stored column is wrong.

**An overhead smash behind the baseline is detected as a serve.** `:551-553` — `'smash'`/`'overhead'` are serve swing types and the y gate has no upper bound. A put-away smash from 1.6 m behind the baseline sets `serve_d = TRUE`, which can increment `point_number` mid-rally and truncate the rally.

**Unknown outcome silently awards the point to the opponent.** `:1047-1054` falls through to "opponent of last hitter" without testing that the outcome was actually an error. Combined with the missing-bounce→`'Error'` path, `point_winner` is biased toward whichever player is detected *less* reliably.

**The advisory lock is taken after the writes.** `_task_lock()` (`ingest_bronze.py:736`) is called only from `_post_ingest_transforms`, which runs at `:886` — after the deletes (`:827-831`) and all 13 inserts. Two concurrent ingests of one task (the 5-min orphan cron plus a browser poll, on different gunicorn processes — the `_active_ingests` guard is per-process) both delete zero rows, both insert, both commit. Silver's `ON CONFLICT` cannot dedupe them because the two batches got different `BIGSERIAL` ids.

**`task_id::uuid` against a TEXT column.** `build_silver_v2.py:358` and `:412`. Defeats the index (casts every scanned row), and **one non-UUID `task_id` anywhere in bronze fails every SportAI silver build**, not just that task's.

**Return Points Won % means two different things on two tabs.** `match_analysis.html:1489` divides by `returns_played`; `:1556` divides by `vs_first_played + vs_second_played`. Same player, same match, two numbers, neither labelled with its denominator.

**Two incompatible definitions of Total Points.** `db_init.py:406` uses `MAX(point_number)`; `gold_init.py:197` uses `COUNT` over deduped points. `MAX` is an index, not a count, so it includes excluded points. The Locker Room hero card and the Match Analysis header disagree — visibly, because the number changes once the async fetch lands.

**`match_return_breakdown` mislabels unknown players as player_b.** `gold_init.py:541` lacks the `player_id IN (player_a_id, player_b_id)` restriction its three sibling views all have; the `CASE … ELSE 'player_b'` then absorbs any third detected player.

**`vw_player` A/B resolution picks the partner by `MAX(player_id)`** over a set that is not filtered by `exclude_d` (`gold_init.py:51-68`) — so a warm-up spectator with a high tracker id can become "Player B", zeroing every `pb_*` column and dropping the real opponent's shots.

---

## P3 — Hygiene worth fixing while we are in here

- **Nothing filters `model` anywhere.** Not one silver pass (`:392, :431, :528, :1404, :1584, :1627, :1683`), not one gold view. Yet the unique key is `(task_id, id, model)`, explicitly created "to allow SportAI + T5 rows for same task" (`:212`). Latent **only** because `sport_type` routing keeps the two apart by convention. The moment any task carries both, pass 3 interleaves both streams by `ball_hit_s` and every gold count doubles. Defence-in-depth is cheap: add the predicate.
- **The raw payload is discarded.** `_persist_raw` (`ingest_bronze.py:353`) has **zero callers**; `:833` is a comment and a bare `pass`. So `/bronze/reingest-from-raw` 404s for every task — a parser fix cannot be replayed — and `payload["metadata"]` is never stored, meaning **no fps is recorded anywhere in bronze** while frame numbers are stored in SportAI-native fps.
- **`ball_position` has two contradictory DDLs — CONFIRMED FATAL on a fresh DB (2026-07-19).** `db_init.py:252` declares `x/y/timestamp` `GENERATED ALWAYS`; `ingest_bronze.py:227` declares the same three as plain columns. Both use `ADD COLUMN IF NOT EXISTS`, so **boot order decides**. Reproduced against a clean Postgres 16 (`devenv/`): running `db_init.bronze_init()` first creates them GENERATED, and `_insert_ball_positions` — which writes them explicitly — then fails with `psycopg.errors.GeneratedAlways: cannot insert a non-DEFAULT value into column "x"`, rolling back the **entire** ingest transaction. Prod is unaffected only because its columns were created plain by whichever path ran first historically. **Consequence: any fresh environment — disaster recovery, a new region, a new dev DB — cannot ingest SportAI data at all.** Fix: delete the plain DDL at `ingest_bronze.py:227-232` and let the insert path skip generated columns (it already has a `_generated_cols` helper, currently probing the wrong column list).
- **`0.0` confidence is stored as NULL.** The `or`-chain at `:651-661` treats a genuine zero as falsy, so a totally untracked match skips the `< 0.5` quality gate and reports clean.
- **No speed on Match Analytics carries a unit.** `grep -c 'km/h' frontend/match_analysis.html` → **0**, across 8 gauges and 4 stat cards. `gold.player_performance` ships a `unit` column and the scorecard renderer drops everything except `%` (`:3123`).
- **Practice aggregates in Python and again in JS** (`client_api.py:1676-1757`), with no gold view — the one real architecture violation. Its `first_serve_pct` key is also reused for a different metric computed a different way at `:1627`.
- Docstring drift: at least seven statements in `build_silver_v2.py` describe behaviour the code no longer has (invert-flag direction at `:46`, a self-contradictory serve-bucket map at `:1600`, "filters is_in_rally" at `:12`, and the double-fault definition at `:463` which describes the service-box test that does not exist).

---

## Validation still owed (needs Render)

Every finding above is derived from code. These are the questions where code cannot give the answer, ordered by how much they change the picture:

1. **What unit is `ball_speed`?** It is carried verbatim from SportAI JSON → bronze → silver → gold and **no layer declares or converts a unit**. The only assertion is a `130.0` benchmark constant. If SportAI returns m/s, **every speed on both dashboards is wrong by 3.6×** and nothing on screen would reveal it.
2. **Is the x-axis orientation what the code assumes?** Deuce/ad and every wide/T label depend on x increasing toward camera-right. Nothing in the repo pins this down. If it is reversed, every serve-side and serve-direction label is globally inverted. Check: for `server_end_d='near'`, do `serve_side_d='deuce'` rows cluster at `x > 5.485`?
3. **How far is the derived `mid` from 5.485** on real tasks? > ~0.15 m means the deuce/ad split is actively mislabelling.
4. **Rate of the P1 serve bug** — last-shot serve rows scored `'Winner'` whose bounce lies outside the true service box.
5. **`court_x IS NULL` rate on serves** (fabricated `body`) and **`court_y NOT NULL AND ball_hit_location_y IS NULL` rate** (inverted `depth_d`).
6. **Does any task carry both models?** `SELECT task_id FROM silver.point_detail GROUP BY 1 HAVING COUNT(DISTINCT model) > 1` — if non-empty, the `model` item escalates from P3 to P1 immediately.
7. **Is `bronze.ball_position.x` plain or GENERATED** on the live DB, and does `bronze.raw_result` hold any legacy rows?
8. **How often is `bronze.ball_bounce.type` NULL?** Sets the severity of the NULLS-FIRST bounce bug.
9. **A real SportAI payload.** No sample exists in the repo. The many fallback chains (`swings`∥`strokes`∥`swing_events`, four `team_sessions` shapes, five rally sources) show the format was reverse-engineered and never pinned — so several ingest findings are "correct given a stated input" rather than confirmed against production reality.

A ready-to-run harness for items 3-8 sits in the session scratchpad (`validate_pipeline.py`) — it drives the read-only `/ops/diag/sql` endpoint and needs only `OPS_KEY`.

---

## Suggested fix batching

Ordered so that each batch is independently shippable and verifiable, and so the ones that *change existing numbers* land only after we can measure them.

| Batch | Contents | Risk |
|---|---|---|
| **1 — Safety, no numbers move** | advisory-lock placement; fail/flag a zero-count ingest before billing; `deleted_at` filter in `vw_player`; `task_id::uuid` → text compare; reconcile the `ball_position` DDL | Low. Pure correctness of plumbing; no dashboard value changes except deleted matches disappearing, which is the intent. |
| **2 — Display truth, no SQL changes** | `pctW` NULL handling; speed units everywhere; scorecard `unit` column; W:E ratio; heatmap "win rate" label; practice `0`-vs-`—`; the histogram NaN bucket | Low. Frontend only, immediately visible, easy to eyeball. |
| **3 — Serve correctness** ⚠ | real service-box test; fixed centre service line; `'Double'` label scoping; `serve_location` NULL instead of fabricated body | **High — this rewrites historical numbers.** Serve %, aces, double faults and point winners all move. Wants a before/after on a known match. |
| **4 — Geometry + zones** ⚠ | service-line constants; `shot_phase_d` redefinition; aggression/depth thresholds into `SPORT_CONFIG`; `depth_d` NULL-hitter fallback; bounce `NULLS LAST` | **High — same.** Changes depth/aggression distributions and the FH/BH speed population. |
| **5 — Aggregation ownership** | serve-strategy point count into the view; return-% denominator into the view; `gold.practice_summary`; `model` predicates as defence-in-depth | Medium. Mostly moving existing math into SQL. |

Batches 3 and 4 should not ship until the Render validation above answers items 1-3 — a fix built on the wrong axis convention or the wrong speed unit would be worse than the bug.

---

# ADDENDUM — after reading the SportAI API documentation

Source: `sportai docs.docx` (Tennis Beta, "Result in detail"), read 2026-07-19. This addendum **supersedes** several findings above. Read it before acting on anything in this report.

## ~~NEW P0 — SportAI's X axis is the SINGLES court~~ — **RETRACTED 2026-07-19, the code was right**

> **This finding is WITHDRAWN. Do not act on it.** Measured against production:
>
> | statistic (floor bounces, `tennis_singles`, in-court y) | observed | doubles frame predicts | singles frame predicts |
> |---|---|---|---|
> | p05 `court_x` | **1.47** | 1.37 (`singles_left_x`) | ~0.4 |
> | p50 `court_x` | **5.49** | 5.485 (centre) | ~4.1 |
> | p95 `court_x` | **9.44** | 9.60 (`singles_right_x`) | ~7.8 |
>
> `bronze.ball_bounce.court_x` is in the **doubles frame [0, 10.97]**, exactly as `SPORT_CONFIG` declares. The existing constants are correct and every x-derived field below is fine.
>
> **Why the docs misled me:** SportAI uses *different frames for different fields*. `bounce_heatmap` really is a 23.77×8.23 singles grid — measured at **24 rows × 9 cols** in production, matching the docs — but `court_pos` is not on that grid. I generalised the documented heatmap dimensions to the bounce coordinates. The documentation's `court_pos — "[0:8.23, 0:23.77]"` line is simply **wrong** for delivered data.
>
> **Lesson for this codebase: vendor docs are a hypothesis, production data is the authority.** Every remaining docs-derived claim in this addendum (confidence key names, `warmups`, `swing_type` domain, `meta`) is therefore *unverified* and must be measured before being acted on. See "Docs-derived claims still needing measurement" at the end.
>
> Retained below strictly as a record of the reasoning and its refutation.

## ~~Original claim~~ — SportAI's X axis is the SINGLES court [0, 8.23]; our config assumes a DOUBLES court [0, 10.97]

The docs state the court coordinate range three separate times, unambiguously:

- Ball Bounces: *"Coordinate X is between 0 and 8.23 meters (27ft), and Y is between 0 and 23.77 meters (78ft)"*; `court_pos` — *"Units: meters [0:8.23, 0:23.77]"*
- Player Positions: `court_X, court_Y: scalar [0:8.23, 0:23.77]`, and *"distance, in meters, from top left corner of the court (as seen from camera)"*
- Bounce Heatmap: *"the bounce matrix dimensions are 23.77x8.23, which reflect the court dimensions"*

**27 ft is the singles width.** SportAI's X origin is the singles sideline; the doubles alleys are not represented at all.

Our `SPORT_CONFIG` (`build_silver_v2.py:79-97`) assumes the opposite frame:

```
doubles_width_m 10.97 · singles_left_x 1.37 · singles_right_x 9.60
```

`SX_LEFT = 1.37`, `SX_RIGHT = 9.60` (`:478-479`), `MID_X_DEFAULT = SX_LEFT + S_WIDTH/2 = 5.485` (`:485`), zone lanes `z2/z3/z4 = 3.4275 / 5.485 / 7.5425` (`:1497-1499`).

**This config is correct for T5** — `ml_pipeline/camera_calibration.py:56` maps to `COURT_WIDTH_DOUBLES_M`, centre `10.97/2` (`:783`). It is wrong for SportAI, and SportAI is the customer-facing path. One shared config cannot serve two different coordinate frames.

### Consequences for every x-derived field on the SportAI path

| Field | Code | Effect in SportAI's [0, 8.23] frame |
|---|---|---|
| **In/out sideline test** | `court_x < 1.37 OR court_x > 9.60` (`:942`) | **Left:** balls landing in `x ∈ [0, 1.37)` — the leftmost **16.6% of the singles court** — are wrongly scored `'Error'`. **Right:** `x > 9.60` is unreachable (max 8.23), so a ball out over the right sideline is **never** detected as out. Wrong in both directions at once. |
| **Deuce/ad split** | fallback `mid = 5.485` (`:485`) | True centre is **4.115**. The dynamic `AVG` (`:497-511`) partially self-corrects, but its own filter `BETWEEN 1.37 AND 9.60` discards every serve struck from `x < 1.37`, biasing the mean; with no qualifying serve it falls back to 5.485, which is 1.37 m off centre. |
| **`serve_location` wide/body/T** | anchored at `SX_LEFT`/`SX_RIGHT` (`:1099-1116`) | Bins start 1.37 m inside the true sideline; the wide/body/T distribution is shifted across the whole court. |
| **`rally_location` lanes A-D** | `z2/z3/z4 = 3.4275/5.485/7.5425` (`:1497-1499`) | True quartiles of [0, 8.23] are **2.06 / 4.115 / 6.17**. Lanes shift ~1.37 m; lane D collapses from a full quarter to the last 0.69 m. Placement heatmaps skew accordingly. |
| **x normalisation / mirroring** | `10.97 - x` (`:1559-1569`) | Should be `8.23 - x`. Far-player x is mirrored to a point **2.74 m** off, so near/far placement data are not comparable — silently corrupting every combined heatmap. |

**This outranks the serve-box defect.** That one affects serve legality; this affects *every* x-derived fact in the system.

**Must be confirmed against data before any fix** (one query):
```sql
SELECT MIN(court_x), MAX(court_x), AVG(court_x),
       COUNT(*) FILTER (WHERE court_x > 8.23) AS above_singles_width
FROM bronze.ball_bounce WHERE task_id = '<a sportai task>';
```
`MAX ≤ 8.23` with zero rows above → singles frame confirmed. Run the same against a T5 task, where the max should approach 10.97.

## Findings RETRACTED or downgraded by the docs

- **`ball_speed` unit — RESOLVED, no bug.** Docs: *"Estimated ball velocity just after the ball was hit. **Unit: km/h**"*. My earlier "if it's m/s everything is 3.6× wrong" risk is **withdrawn**. The missing on-screen unit labels remain a display defect, not a correctness one.
- **X-axis orientation — RESOLVED, code is correct.** *"distance from top left corner of the court (as seen from camera)"* ⇒ x increases camera-left→right, y increases far→near. The deuce/ad mirroring direction and `server_end_d` (`y < 1.5 → far`) are both right.
- **"Smash behind the baseline misdetected as a serve" — cannot fire on SportAI.** The documented `swing_type` domain is **exactly** `fh_overhead`, `fh`, `1h_bh`, `2h_bh`, `other`. No `smash`, no `overhead`, no `bh_overhead` — those branches at `:551-553` are dead code on this path (they may still matter for T5, which emits its own vocabulary).
- **`ball_impact_location` / `ball_impact_type` / `intercepting_player_id` / `ball_trajectory` are documented "Not in use yet"** — always null. The suggestion to reuse `ball_impact_location` instead of solving bounce-matching is void.
- **`ball_position.X/Y` units — RESOLVED.** Normalised *image* coordinates `[0,1]`, not court metres. Nothing consumes them, so this is inert.

## New defects the docs expose

1. **The confidence quality gate is 100% dead, not merely zero-sensitive.** Documented keys under `confidences` are `pose_confidences`, `ball_confidences`, `swing_confidences`, `final_confidences`. `_upsert_session_confidences` (`ingest_bronze.py:651-661`) probes for `tracking_confidence`, `tracking`, `court_detection`, `court` — **none of which exist**. Both typed columns are therefore *always* NULL, so `build_silver_v2.py:1761`'s `< 0.5` warning can never fire. The raw object *is* preserved in `data` jsonb, so nothing is lost — the extraction just needs remapping to `final_confidences.final` / `.ball` / `.pose`. This upgrades the earlier "0.0 is falsy" finding.

2. **`swing_type = 'slice'` is unreachable.** `stroke_d` maps `'slice'/'bh_slice'/'fh_slice'` → `'Slice'` (`:1647`), but SportAI never emits them. Any Slice category on a dashboard is permanently zero for SportAI matches.

3. **SportAI hands us `warmups` and we ignore it.** The docs define a top-level `warmups[]` with `start_time`, `end_time`, `warmup_confidence`, `method`, `reason`. `ingest_bronze_strict` does not read the key at all — while pass 3 hand-rolls an elaborate warm-up exclusion heuristic. A supplied bronze fact being re-derived (RULE 1 violation), and very likely a free accuracy win on `exclude_d`.

4. **The metadata key is `meta`, not `metadata`.** `_derive_task_id` (`:96-97`) reads `payload["metadata"]`. Per the docs the object is `meta`, and it carries `video_info` with **`fps`**, plus `sport_type`, `n_players`, `n_rallies`, `n_floor_bounces`. The fps that bronze lacks *is* in the payload, discarded twice over — wrong key name, and never persisted.

5. **`team_sessions` is documented ground truth for near/far.** `team_front` = *"close to the camera"*, `team_back` = far. `gold.vw_player` instead infers A/B by `MAX(player_id)` over an unfiltered set. The reliable signal is already ingested into `bronze.team_session` and unused.

6. **Bounce `type` is exactly `"floor"` or `"swing"`, where `"swing"` means a racket contact.** Confirms the Pass-2 fallback defect: with no floor bounce in the window, the code takes a *racket* contact as the shot's landing coordinate. It should return NULL. The documented domain being closed makes `type IS NULL` rare, which lowers the NULLS-FIRST bug's frequency without making it correct.

7. **`serve` is a documented, reasonably reliable flag** — *"The first swing of a rally almost always has serve=true"* — that we deliberately ignore in favour of geometry (`:533-560`). Worth revisiting as a conjunct rather than a replacement.

## Owner's stated ground truth (2026-07-19)

- far side `y = 0`, near side `y ≈ 23` — **matches docs and code.**
- x runs far-side left→right — **matches docs** (origin = top-left as seen from camera).
- **`serve_d` should be: a forehand overhead struck behind the baseline is assumed a serve** ("rare to have an overhead from behind the baseline"). Docs agree: `fh_overhead` = *"Forehand overhead (often a serve)"*. Note the current gate is `y < 1.5` / `y > 22.27`, i.e. it also admits overheads up to **1.5 m inside** the court — more permissive than "behind the baseline". That tolerance exists for T5 calibration error (the comment at `:89-96` records SportAI's own values sitting at ~0.0 or ~24.47, i.e. genuinely at/behind the baseline), so it can likely be tightened for the SportAI path specifically.

## Docs-derived claims — MEASURED against production 2026-07-19

The retracted P0 proved vendor docs do not reliably describe delivered data, so every remaining docs-derived claim was measured before being accepted. Results:

**Actual top-level payload keys** (from `bronze.session.meta->'keys'`, which stores what each payload really contained):

```
ball_bounces · ball_positions · bounce_heatmap · confidences · debug_data
highlights · meta · player_positions · players · rallies · team_sessions
thumbnail_crops · warmups
```

**Actual `swing_type` domain** (`bronze.player_swing`): `fh` 1296 · `fh_overhead` 1230 · `1h_bh` 506 · `other` 458 · `2h_bh` 394 — exactly the five documented values, nothing else.

**Actual `confidences` keys** (`bronze.session_confidences.data`): `ball_confidences`, `final_confidences`, `pose_confidences`, `swing_confidences`.

| claim | verdict |
|---|---|
| SportAI supplies `warmups[]` that we ignore | ✅ **CONFIRMED** — key present; `ingest_bronze_strict` never reads it while pass 3 hand-rolls warm-up exclusion |
| the object is `meta`, not `metadata` | ✅ **CONFIRMED** — `_derive_task_id`'s `payload["metadata"]` (`ingest_bronze.py:96`) has never matched. Masked only because both prod callers pass `task_id` explicitly. `meta.video_info.fps` is available and discarded |
| confidence typed columns are always NULL ⇒ quality gate dead | ✅ **CONFIRMED** — none of the four probed key names (`tracking_confidence`/`tracking`/`court_detection`/`court`, `ingest_bronze.py:651-661`) exists in the payload. The `< 0.5` gate at `build_silver_v2.py:1761` has never fired |
| `'Slice'` stroke unreachable | ✅ **CONFIRMED** — no `slice` variant is ever emitted; the bucket is permanently zero |
| serve-detection swing list is 3/4 dead code | ✅ **CONFIRMED** — of `('fh_overhead','bh_overhead','overhead','smash')` only `fh_overhead` can match. Retires the earlier "smash misdetected as serve" finding entirely |
| `team_sessions.team_front` = camera-side player | ⏳ not yet measured |

**New, found by measurement:** SportAI sends `debug_data`; the ingest looks for `debug_events`/`events_debug` (`ingest_bronze.py:846`), so `bronze.debug_event` is permanently empty. Same class as the `meta`/`metadata` bug — a guessed key name never checked against a payload.

**Why the confidence remap matters more than it looks.** The discarded block is the only quality signal SportAI gives us, and `ball_confidences` is its weakest component (the docs' own example shows `ball: 0.30984`, `ball_detection_frequency: 0`). Poor ball detection is the upstream cause of the missing-bounce cascade documented above — fabricated `body` serve locations, inverted `depth_d`, false double faults from absent coordinates. Remapping to `final_confidences.ball` / `.final` would let a match be flagged as low-quality *before* a customer opens a dashboard built on it.

**Sizing (`fh_overhead` ≈ serves).** `fh_overhead` is 31.7% of all swings — implausible for genuine overheads, entirely consistent with serves (≈⅓ of shots in singles). This supports the owner's rule that a forehand overhead behind the baseline is a serve, and means serve detection rests on a single swing type plus a geometric test.

## GROUND-TRUTH VALIDATION — task `052786b4` (owner-played, 2026-07-19)

The owner played and uploaded a known match, giving us the first true reference. **Truth: 2 games (1-1), 18 points — game 1 = 10 points, game 2 = 8 points, ~25 serves.**

### What the pipeline got RIGHT

| | truth | silver | |
|---|---|---|---|
| points | 18 | 18 | ✓ |
| games | 2 | 2 | ✓ |
| points per game | 10 + 8 | 10 + 8 | ✓ |
| deuce/ad alternation | strict | strict across all 18 | ✓ |
| server end | changes at game 2 | near (g1) → far (g2) | ✓ |

Point/game structure derivation is **correct**, not lossy. Several severity estimates in this report assumed otherwise and were wrong.

**The owner's serve rule is confirmed:** all 27 detected serves were struck *strictly behind* the baseline — near server `y ≈ 24.5` (baseline 23.77), far server `y ≈ -1.3` (baseline 0). None sits in the 0–1.5 m inside-court band the current gate at `:551-553` admits. Tightening `serve_d` to "fh_overhead strictly behind the baseline" costs zero recall here and drops 155→103 false serves on the pathological task `0336b82b`.

### F4 CONFIRMED with a measured impact

Point 5 was a double fault. Both its serve rows carry `serve_try_ix_in_point = 'Double'`, so its first serve is invisible to `gold.match_kpi`'s denominator (`gold_init.py:255`):

- computed: 9 in ÷ **17** attempts = **52.9%**
- truth: 9 in ÷ **18** attempts = **50.0%**

**One double fault inflated first-serve % by 2.9 points**, and the error scales with double-fault rate — flattering exactly the players who serve worst. This is no longer a code-reading inference; it is measured against a known match.

### NEW P2 — `_validate_rally_count` is anchored to the unreliable side

`build_silver_v2.py:1692-1720` compares silver points against `bronze.rally` and warns on divergence. On this match it emitted:

```
RALLY VALIDATION WARNING silver_points=18 vs bronze_rallies=27 (33.3%)
```

Silver's 18 is **correct**; SportAI's 27 rallies is the wrong number — it over-segments (27 rallies for 18 actual points). The validator treats the unreliable input as truth and flags the correct output. On `0336b82b` it fires at 92.9% for the same reason in the opposite direction (bronze reports only 8 rallies for a full match).

So the check produces false alarms in both directions and cannot be used as a quality signal. Either re-anchor it to serve-derived point counts, or drop it. Its current form actively misleads — it sent this audit chasing a non-existent "silver is losing a third of the match" defect until owner ground truth corrected it.

## VIDEO-RECONCILED GROUND TRUTH — `vw_point_hardening.xlsx` (task `840cacb7`)

The owner hand-reconciled 93 silver rows against video footage, logging 17 discrepancies. Categorised:

| class | n | nature |
|---|---|---|
| player id `118` vs `119` | 8 | stroke/phase/outcome identical, only the id differs — the `_resolve_two_players` de-ghosting fold (finding S4) |
| **false serves** | 4 | pipeline says Serve; video says Forehand (Rally / Transition / Return) |
| near/far mix-up (`118` vs `22`) | 1 | genuine cross-court misattribution |
| wrongly excluded | 2 | `exclude_d = TRUE` on shots the video confirms were real |
| outcome-only | 2 | serve correct, outcome wrong (incl. a Double Fault scored as "In") |

### The false serves are BRONZE errors and are not fixable in silver

Three candidate `_d`-layer fixes were tested against this ground truth. **All three failed:**

1. **Inherit SA's `serve` flag** — SportAI reports `serve = TRUE` on **all four** false serves, with `swing_type = 'fh_overhead'` and `ball_hit_location_y` of 24.47 / -2.17 / -1.58 / -1.92 (all strictly behind a baseline). SportAI classified a forehand struck ~2 m behind the baseline as an overhead *and* flagged it a serve. Both available signals are wrong in the same direction, so switching sources fixes nothing.
2. **Require a time gap before a serve** (a serve starts a point) — inverted. Gap before *real* serves: median 15.7 s, min 5.1 s. Gap before *false* serves: median 28.5 s, max 33.6 s. The false serves look *more* like point-starts than real ones, so this rule would reject real serves and keep the fakes.
3. **Use ball speed** (serves are fastest) — `ball_speed` is NULL on 24 of the 25 serve rows in this match. No signal to use.

At least one case is a **missed detection** compounding a misclassification: at t=602.40 the video shows a *return*, meaning the actual serve ~1 s earlier was never detected at all, and the return inherited the serve label.

**Conclusion — this is a genuine ceiling.** Under RULE 1 (bronze owns the fact; silver inherits and derives), these errors cannot be corrected in the derived layer, because every bronze signal that could discriminate is itself wrong. The honest responses are (a) flag and measure the uncertainty rather than presenting it as clean, and (b) fix it upstream — SportAI accuracy, or T5.

**Lesson on fixture durability:** this match's bronze and silver rows were **soft-deleted 2026-04-27 and swept**, so the annotated ground truth can no longer be rebuilt from source — only the spreadsheet survives. Any match used as a ground-truth reference must be protected from the orphan sweep.

### NEW — serve-speed KPIs are computed on a partial sample

`ball_speed` coverage on serve rows, measured locally: **73%** (`079d2c62`), **62%** (`052786b4`), **28%** (`0336b82b`). SQL `AVG` silently ignores NULLs, so `gold.match_kpi.pa_serve_speed_avg`, `pa_first_serve_speed_avg` and the fastest-serve tiles are averages over whichever subset SportAI happened to measure — presented on the dashboard as if they described the whole match. Nothing surfaces the coverage. On a match like `0336b82b` the "average serve speed" describes barely a quarter of the serves.

## NEW P2 — prod silver mixes code vintages, and has no provenance to tell them apart

Found while validating the local dev environment (2026-07-19). Rebuilding silver locally from the *same* bronze rows:

| task | ingested | local rebuild vs prod |
|---|---|---|
| `079d2c62` | 2026-06-16 | **identical** — every column, every row (94/94) |
| `0336b82b` | 2026-04-28 | **13 columns differ**: `game_number` (13.0% of rows), `game_winner_player_id` (13.6%), `exclude_d`, `shot_ix_in_point`, `shot_outcome_d`, `point_winner_player_id`, `rally_length*`, `serve_try_ix_in_point`, … |

The build is **deterministic** — rebuilding the same task twice locally yields byte-identical output (verified via `diff_silver --save` / `--vs`). So the divergence is not randomness: **production silver for older matches was derived by older code and never rebuilt.** `build_silver_v2`'s pass-3 logic has changed since April (the exclusion re-anchor, game numbering), and those matches still carry the old derivations.

Two consequences:

1. **Dashboards today render a mix of code vintages.** Two matches side by side in one customer's history can have had `exclude_d`, `game_number` and `point_winner_player_id` computed by materially different rules. Cross-match aggregates — `gold.player_performance`'s rolling-5 scorecard especially — average across that mix.
2. **`silver.point_detail` has no build provenance.** There is no `built_at`, no code version, no timestamp column of any kind, so you cannot tell which rows are stale, or audit the blast radius of a past change after the fact. Every silver-affecting fix from here on inherits this blind spot.

**Recommended alongside any batch-3/4 fix:** add `built_at TIMESTAMPTZ DEFAULT now()` and a `builder_version` text column to `silver.point_detail`, and rebuild historical silver once the derivation fixes land — otherwise the corrected logic applies only to matches ingested after the deploy, silently widening the vintage spread rather than closing it.

**Method note:** because prod is a mix of vintages, `--against-prod` is *not* a valid gate for the fixes. The correct gate is local-before vs local-after (`diff_silver --save` / `--vs`), which isolates exactly what a change moves.

## Revised priority (post-retraction)

1. **P1 — the serve service-box test** (`:945`). Now the top item. Unaffected by the retraction: it is a *y*-axis and box-membership defect, and the x bounds it needs are the existing, correct `1.37 / 9.60`. The centre service line is **5.485** — which is already `MID_X_DEFAULT`, so the fix is to use that fixed value rather than the drifting `AVG`.
2. **P1 — service-line constants** `6.40 / 17.37` → **5.485 / 18.285**. A pure y-axis error, untouched by the frame question, and it still mis-defines `shot_phase_d`.
3. **P1 — hollow-ingest billing; NULL-as-zero rendering; deleted matches on dashboards; the `ball_position` GENERATED DDL** (reproduced fatal on a fresh DB). All independent of geometry and safe to fix now.
4. **P2 — the docs-derived items above**, each only after its verifying query.

---

# JSON COVERAGE + DATA NUGGETS — test case `052786b4` (2026-07-22)

Downloaded the full 11.6 MB raw SportAI JSON and ran `devenv/coverage_check.py`
against live bronze. **Verified result: 322 leaf paths → 67 PROMOTED, 105
PRESERVED, 150 DROPPED — and the only two truly-dropped top-level blocks are
`meta` and `debug_data`** (plus `warmups`, empty here). Everything else is
captured (typed columns or `data` blobs).

## Where the 11.6 MB lives
`players` 10.16 MB (pose keypoints) · `debug_data` 1.14 MB · `highlights` 0.96 MB
· `player_positions` 0.58 MB · `ball_positions` 0.29 MB · rest < 0.05 MB each.

## What's DROPPED (not ingested anywhere)

**`meta` (19 fields) — the important loss.** Contains `fps: 25`, resolution
1920×1080, **`duration: 612.0`** (the video is 10.2 min; SportAI just found no
activity after 6.6 min), codec, bitrate, total_frames, start_timestamp,
`n_players`/`n_rallies`/`n_floor_bounces` (self-check counts), `sport_type_original`
(`tennis_unknown` → reclassified). Dropped only because ingest reads key
`metadata` where SportAI sends `meta`. **fps especially matters** — bronze has no
fps, the root of the "two frame spaces" hazard.

**`debug_data` (131 fields, 1.14 MB) — SportAI's internal reasoning, entirely
discarded.** Per-swing it exposes SportAI's OWN confidence for every decision we
currently re-derive: `serve_conf`/`serve_nn`/`serve_dyn1/3` (is-it-a-serve),
`conf_ball_in`/`conf_ball_out`/`conf_ball_hit` (in/out), `far` (near/far),
`nballs` (multi-ball/neighbouring court), `discarded`, `rally_start`/`rally_end`,
plus `sconf_*`/`vconf_*` sub-scores and `court_keypoints` (calibration).

## What's PRESERVED but UNUSED (stored in a blob, silver never reads it)

- **`highlights` (20 items, 965 KB)** — auto-detected `longest_rally` /
  `fastest_rally` with duration, ball_speed, `dynamic_score`, players_speed. A
  ready-made highlights reel.
- **`team_sessions` (17)** — `team_front` (near) / `team_back` (far) player IDs
  with time windows. **SportAI's own near/far assignment** — ground truth for the
  A/B identity we solve with de-ghosting heuristics.
- **player metrics** — `covered_distance` (362 m), `fastest_sprint` (16.8 km/h),
  `activity_score`, `swing_type_distribution` (fh 42% / fh_overhead 30% / 2h_bh
  12% / other 16%), `location_heatmap`. A whole fitness/style dashboard.
- **per-swing `confidence` / `confidence_swing_type` / `confidence_volley`** —
  PROMOTED to bronze columns but silver never filters on them.

## Phase-2 opportunities (ranked)

1. **Quality + serve/outcome accuracy** — ingest `debug_data` per-swing
   confidences (`serve_conf`, `conf_ball_in/out`, `far`) and `meta` (fps,
   resolution). These are SportAI's own answers to the exact questions we
   struggle to derive geometrically.
2. **Player identity** — use `team_sessions.team_front/back` as near/far truth.
3. **New features** — highlights reel; player fitness/style panel.
4. **Frame-space correctness** — store `meta.video_info.fps`.

## Rock-solid foundation shipped

- `raw_archive/` — every ingest now stores the whole payload to
  `s3://<bucket>/raw-json/<task>.json.gz` and alarms (log + ops email) on any new
  top-level key. Source of truth is never lost again; schema drift can't pass
  silently.
- `devenv/coverage_check.py` — on-demand raw-vs-bronze coverage + drift report.

---

# RALLY RECON — task `052786b4` + 2 seeded matches (2026-07-23)

The analogue of the serve recon. Spec under test, in the owner's terms:
**once a serve is in, the rally = return → the point-ending shot (winner by the
hitter, or error by the other player).** Method: rebuild silver locally at HEAD,
trace all 18 points shot-by-shot, then measure every candidate defect across all
three seeded matches rather than eyeballing one.

Rebuild reproduced prod exactly (100 rows, 18 points, the known false
`RALLY VALIDATION WARNING`). Bench green (`ea1e500c` 12/26, `880dff02` 23/24).

## R1 — the rally filter is DISARMED on the entire SportAI production path

`build_silver_v2.py:1413-1415`:

```sql
exclude_d = ec.exclude_d
  OR (has_bounce_data AND has_serves AND NOT in_rally_window)
```

`has_bounce_data = EXISTS (SELECT 1 FROM ball_bounces)`, and `ball_bounces` is
derived from `ml_analysis.ball_detections` + `ml_analysis.video_analysis_jobs`
(`:1276-1294`) — tables only the **T5 Batch** pipeline populates. A SportAI task
has no Batch run, so both are empty, `has_bounce_data` is FALSE, and the whole
between-point clause collapses to FALSE.

Verified in devenv: `ml_analysis.video_analysis_jobs` = 0 rows,
`ml_analysis.ball_detections` = 0 rows for all three seeded SportAI matches.

**Consequence: on `tennis_singles` — the live product — nothing bounds a rally in
time.** The only exclusions that fire are `excl_chain` (warm-up / pre-first-serve
/ per-point). The filter was built and tuned for T5 and is inert where the
customers are. This is not a tuning gap; it is a whole safety gate that never
engages in production.

## R2 — even when armed, it cannot close a rally

`rally_end_s = GREATEST(last_bounce + 1s, rally_start + 20s)`, capped at
`next_rally_start - 3s` (`:1339-1369`). The 20s floor is deliberate and
documented — it was added because `last_bounce + 1s` under-counted T5 rows.

But it makes the window a **between-point** fence, not a rally-end rule: any shot
within 20s of the serve is in-rally by construction. Point 15's 20.0s intra-rally
gap sits at +39.8s from its serve, inside a window that runs to `next_serve - 3s`
= +50s. Arming the filter on SportAI data would not have caught a single defect
below.

## R3 — measured: intra-rally gaps are cleanly bimodal, with an empty band

All 442 intra-point gaps across the three seeded matches:

| gap | 0-1 | 1-2 | 2-3 | 3-4 | 4-5 | **5-6** | 6-8 | 8-10 | 10-15 | 15-20 | 20-30 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| n | 17 | 339 | 45 | 24 | 10 | **0** | 2 | 1 | 3 | 0 | 1 |

Per match (median / p99 / max): `0336b82b` 1.53 / 4.70 / 9.27 · `052786b4`
1.32 / 20.04 / 20.04 · `079d2c62` 1.32 / 10.76 / 10.76.

99% of gaps are <=4.7s; **the 5–6s bin is empty in all three matches**, including
the 324-gap pathological one. The separation is physical, not fitted: a ball
crossing the court takes ~0.8–2.5s, so a 6s gap means the ball was not in play.
Any threshold in (4.7, 9.3) is defensible; 6s sits in the empty band.

## R4 — alternation violations (impossible in singles)

Consecutive shots by the same player, `052786b4`: **5** — points 8 (ix6->7),
11 (ix4->5), 15 (ix9->10 and ix16->17), 17 (ix1->2). Three coincide with an R3 gap;
two do not, so this is two causes overlaid: **unbounded rallies** admitting
post-point activity, and **missed stroke detections** leaving an apparent
same-player pair. Point 17 is the sharpest: the server's own shot 13.8s after
their serve is labelled `phase='Return'`.

## R5 — `rally_length_point` excludes the serve, but is presented as "shots"

`rally_length_point = max(shot_ix_in_point) - 1` (`:1705-1708`) — confirmed
= `shots - 1` on **18/18** points (ace = 0, serve+return = 1, the 17-shot point 15
= 16). Consumers label it in shots: `gold.match_rally_length` buckets
`'Short (1-4)' / 'Medium (5-8)' / 'Long (9+)'` (`gold_init.py:644-646`),
`avg_rally_length` / `max_rally_length` (`:208-209`), and "rally points" is
defined as `rally_length_point >= 5` (`:205-207`, `:771-772`).

Whether this is a bug is a **definition call the owner should make** — broadcast
convention counts the serve as shot 1, which would make every bucket and the
`>= 5` threshold off by one. Flagging, not fixing.

## R6 — a missing bounce is scored as an error, and it erases winners

`shot_outcome_d` (`:1029`): `WHEN court_x IS NULL OR court_y IS NULL THEN 'Error'`.
On the point-ending shot, "no bounce recorded" therefore becomes "the player
missed". Measured on the point-ending shot of every point:

| task | points | last-shot bounce NULL | **Winner %** |
|---|---|---|---|
| `0336b82b` | 112 | 110 | **0.0%** (0 winners in 112 points) |
| `052786b4` | 18 | 8 | 33.3% |
| `079d2c62` | 18 | 10 | 22.2% |

The rule is **often right** — a ball hit into the net has no in-court bounce, so
NULL->Error scores it correctly, and the two well-tracked matches land at a
plausible 22–33% winners. But it cannot distinguish "no bounce because the ball
was netted" from "no bounce because tracking dropped it", so the winner/error
split degrades with ball-tracking coverage **and is presented as fact either way**.
On `0336b82b` it reports *zero winners in 112 points* — physically impossible, and
nothing on the dashboard says the number is unreliable.

Winners-vs-errors is a headline tennis metric. This is the same class as the
serve-speed-coverage finding: an average over whatever subset happened to track.

**Principled fix is upstream, per RULE 1:** `debug_data` already carries SportAI's
own per-swing `conf_ball_in` / `conf_ball_out` / `conf_ball_hit` (see "JSON
COVERAGE" above — 131 fields, entirely unused). Inherit the in/out fact rather
than inferring it from bounce presence; keep a third `Unknown` state for when
neither side is confident, instead of defaulting to `Error`.

## What a max-gap rule would actually change — and why it must NOT ship yet

Simulated at 6s (truncate each rally at its first gap > 6s, then re-apply the
existing outcome + point-winner rules):

- **7 points truncated of 148** (3 in `0336b82b`, 3 in `052786b4`, 1 in `079d2c62`).
- `052786b4` point 15: 17 shots -> 15 · point 11: 5 -> 3 · point 17: 2 -> 1.
- **5 point winners would flip.**

But **4 of those 5 flips land on a shot whose bounce is NULL**, so the new
"point-ending" shot is scored `Error` purely by R6 — a detection gap converted
into a scored error, then into a point. The flips are artifacts, not adjudications.

**Conclusion — split the fix, and do not couple them:**

1. **Safe now:** bound the rally for *length and continuity* (R3/R4/R5 —
   `rally_length_point`, buckets, avg/max, the `>= 5` rally-points gate).
2. **Blocked on R6:** do not let a truncated rally re-decide the **point winner**
   while NULL-bounce still fabricates an error. Fix the outcome fact first.

This is the third proposal this sprint that measurement stopped from shipping on
a hunch (after the coordinate-frame P0 and the serve timing-gap rule).

## Retracted before it reached the report

"Point-ending error attributed to the server" — flagged as a defect class (5 cases
on `052786b4`), then discarded: a server hitting shot 3 into the net is ordinary
tennis. Points 3, 7, 12 are legitimate. Only 15 and 17 are suspect, and only via
R3/R4. The owner's phrase "error by the receiver" describes the *common* case,
not an invariant — either player can end a point with an error.

## Owed: video adjudication on `052786b4`

Everything above is measurement; these need the owner's footage to settle.

| point | t (s) | what the pipeline says | question for the video |
|---|---|---|---|
| **11** | 386.6–401.9 | 5 shots, 21 wins; 10.8s gap ix3->ix4, then two consecutive 21 shots | did the point end at ix3 (~388.7s)? who won it? |
| **15** | 502.6–547.0 | 17 shots (longest of the match), 21 wins; 20.0s gap ix15->ix16 | did the rally end at ix15 (~522.3s)? are ix16/ix17 the next point or off-ball? |
| **17** | 584.9–598.7 | serve + one shot, 21 wins; 13.8s gap, and the "return" is by the SERVER | was 21's return simply never detected? who won it? |
| 8 | 294.2–296.8 | ix6, ix7 both pid=154, 2.6s apart | is a shot by 21 missing between them? |
| 15 | 512.8–516.0 | ix9, ix10 both pid=154, 3.2s apart | same question |

If 11/15/17 truncate as predicted, `max_rally_length` on this match drops 16 -> 14
and the longest-rally tile changes.

## ADJUDICATED — owner video, 2026-07-23

Owner's ruling on the three contested points (`052786b4` = Tomo vs Jimbo Ma,
2026-07-19):

- **pt 11** — ends ~388.7s, **154 hit it out wide** → 21 wins
- **pt 15** — ends ~522.3s → 21 wins
- **pt 17** — **21 misses the return** → **154 wins** (the shipped pipeline says
  21 — a live, wrong point winner)

### Timing: the 6s rule is right 3/3

It truncates each point at exactly the moment the owner says it ended —
388.7s, 522.3s, 584.9s. R3's threshold is validated on every contested case.

### Winners: 2/3 shipped → 2/3 with the rule. It is winner-NEUTRAL.

| point | truth | shipped now | with 6s gap rule |
|---|---|---|---|
| 11 | 21 | 21 ✓ | 21 ✓ — ends on 154's shot, bounce `x=0.83` is **outside the singles sideline [1.37, 9.60]** |
| 15 | 21 | 21 ✓ *(by accident)* | **154 ✗** — new last shot has a **NULL bounce** → fabricated `Error` (R6) |
| 17 | 154 | **21 ✗** | 154 ✓ — serve bounces in at (9.58, 16.93) |

Three things follow, and they are the point of this whole exercise:

1. **Point 11 is independent corroboration of the coordinates.** The owner saw
   "out wide"; the pipeline's own bounce `x=0.83` sits outside the singles
   sideline and derives `Error` without being told. Two independent sources
   agreeing — the bounce coordinate is trustworthy *when it exists*.
2. **Point 15 is R6 caught doing real damage.** Truncation timing is correct, but
   the point-ending shot has no recorded bounce, so `NULL → Error` hands the point
   to the wrong player. Note the shipped pipeline gets this point *right by
   accident* — only because the phantom shots at 542/547s end with 154 erring.
3. **The gap rule alone buys nothing on point winners.** It fixes point 17 and
   breaks point 15: 2/3 either way. Shipping it standalone would churn two points
   and look like a wash. **Ship it with the R6 fix or not at all** — with NULL
   bounce no longer fabricating an error, point 15 lands correctly and all three
   are right.

For rally *length* the rule is safe and correct standalone, as already stated.

### New consequence — ace inflation (point 17)

Truncated to the serve alone, point 17 satisfies `ace_d` (serve is last shot +
`Winner` + no return). But the owner says 21 **swung and missed** — a return that
was never detected at all. So the gap rule would make the winner right and the
**ace count wrong**, reporting 2 aces on a match with 1. Any ace rule that infers
"no return" from "no detected return" inherits every stroke-recall miss. Guard
this before the split fix ships.

## Reproduce

```bash
docker compose -f devenv/docker-compose.yml up -d
# NOTE: this box's shell profile exports DATABASE_URL=...:55432/courtflow_dev.
# devenv is :55433/tf_dev — pin it explicitly or scripts hit the wrong database.
DATABASE_URL='postgresql+psycopg://tf:tf@localhost:55433/tf_dev' \
  .venv/Scripts/python -c "import build_silver_v2 as b; print(b.build_silver_v2('052786b4-dba8-4f2f-948c-8b9180c447f8', replace=True))"
```

---

# RALLY RING-FENCE — `exclude_d` vs `is_in_rally` (2026-07-23, same day)

Follow-up to the rally recon. Question: how much of what `is_in_rally` offers is
`exclude_d` already doing? Answer: **almost all of it, and better** — the gap
rule I proposed already exists at 5s. Two corrections to the recon follow.

## Correction 1 — R3's "empty 5–6s band" was an artifact of my own filter

The R3 histogram was measured on `NOT exclude_d` rows, i.e. *after* the existing
5s `gap_break` rule had already removed the long gaps. Re-measured including
excluded rows (n=558):

| gap | 4-5 | **5-6** | 6-8 | 8-10 | 10-15 | 15-20 | 20-30 |
|---|---|---|---|---|---|---|---|
| n | 11 | **16** | 34 | 15 | 24 | 2 | 1 |

The band is **not** empty. The distribution is not cleanly bimodal, and the
"physically motivated separation" claim does not survive. What survives is
narrower and more useful: the three gaps the recon found are the ones that
**escaped** an existing rule, not evidence that no rule exists.

## Correction 2 — `is_in_rally` was already tried, and removed on purpose

`pass1_load` carries the record inline: the `is_in_rally` gate was removed
because it "rejected 480/515 valid swings" on `0336b82b`. That is the same 6%
collapse measured in the recon. So the recon's "SportAI already tells us" was
re-deriving a known, documented decision — and the file's own business-rules
header still claimed the gate was active (fixed in this commit).

## What `exclude_d` actually does

`exclude_d = r1 OR gap_break OR r3` (pass 3):

- **r1** — a non-serve shot *before* the point's last serve (the returned
  first-serve-fault sequence).
- **gap_break** — a post-serve shot beyond `last_connected_s`, where
  `last_connected_s` was `MAX(ball_hit_s)` over post-serve shots within **5.0s**
  of their predecessor.
- **r3** — a non-serve shot with NULL hit coordinates.

## The defect: the re-anchor is a global MAX, not a contiguous chain

The 2026-06-04 re-anchor fixed a real bug (a single mid-rally *detection* gap
cascaded to kill a whole rally — 19/21 shots lost). But it implements
`last_connected_s` as a **global MAX** over every "connected" shot, while its own
comment describes "the last shot of a LIVE rally". So **any dense cluster after a
long gap re-anchors the chain onto its own end**:

- **pt 11** — 399.5s is 10.8s after the rally, but 401.9s is 2.4s after *that*,
  so 401.9 qualifies as "connected" and `last_connected_s` becomes 401.9. Nothing
  is beyond it → nothing excluded.
- **pt 15** — same shape: 542.4 (+20.0s) then 547.0 (+4.6s) → re-anchors to 547.0.
- **pt 17** — different cause: a *lone* post-serve shot 13.8s later produces **no**
  connected chain at all, `last_connected_s` is NULL, and the rule is deliberately
  conservative on NULL → kept.

This is `feedback_greedy_chain_rejection` one turn on: the re-anchor escape that
fixed the freeze now admits between-point clusters.

## Confusion matrix — the two signals agree where it matters

In-point rows, `exclude_d` vs `is_in_rally`:

| task | excl & not-IIR | excl & IIR | keep & not-IIR | keep & IIR |
|---|---|---|---|---|
| `0336b82b` | 35 | 1 | **445** | 34 |
| `052786b4` | 7 | 6 | **5** | 79 |
| `079d2c62` | 1 | 6 | **4** | 83 |

The 5 "keep & not-IIR" rows on `052786b4` are **exactly** the 5 the owner's video
says should be gone. The 6 "excl & IIR" are r1 rows — legitimately out of the
*point*, but inside a *rally* (SportAI counts the fault rally), so that
disagreement is a unit mismatch, not an error. `0336b82b`'s 445 is the collapse.

`bronze.rally` tells the same story from a third angle: 27 windows on `052786b4`
(**one per serve attempt**, not over-segmentation — which is also why the
`silver_points=18 vs bronze_rallies=27` validator warning is a unit mismatch),
and only those same 5 kept shots fall outside every window. On `0336b82b`: 8
windows for a whole match, 445 of 479 kept shots outside. All three SportAI rally
signals are the same underlying segmentation and collapse together.

## The fix (shipped, DEFAULT OFF behind `SILVER_RALLY_CONTIGUITY`)

Three surgical changes to `gap_break`, no new threshold and no new bronze field:

1. `last_connected_s` = end of the **contiguous** chain from the last serve
   (everything before the first >5s break) instead of a global MAX.
2. **Lone-shot guard** — when there is no connected chain, a post-serve shot more
   than 5s after the serve is gap-excluded instead of conservatively kept.
3. **`is_in_rally` escape, coverage-gated at 50%** — preserves exactly what the
   re-anchor protected: if SportAI still calls the shot in-rally *and* its flag is
   trustworthy on this task, keep it. On `0336b82b` (6%) the escape disarms.

Measured, all three seeded matches:

| | flag OFF | flag ON |
|---|---|---|
| `052786b4` | 18 pts, avg 3.17, **max 16** | 18 pts, avg 2.94, **max 14** |
| `079d2c62` | 18 pts, avg 3.39, max 14 | 18 pts, avg 3.33, max 14 |
| `0336b82b` | 112 pts, avg 2.89, max 14 | 112 pts, avg 2.76, max 11 |

- **Flag OFF reproduces the current view exactly** (052786b4 avg 3.17 / max 16).
- Point counts unchanged on every match — no structural regression.
- On `052786b4` exactly the 5 video-confirmed rows are dropped and nothing else;
  points 11/15/17 now end at 388.7 / 522.3 / 584.9 — the owner's timings.
- Every newly excluded row across all three matches (22 of them) independently
  agrees with `is_in_rally`.
- `0336b82b` moves 2.9% of rows — no catastrophe, unlike the hard IIR gate (94%).
- Bench green (`build_silver_v2.py` is a CI trigger path).

## Why it stays default OFF

It is **not** a strict improvement on point winners. On `052786b4` it fixes
point 17 (a live wrong winner: ships 21, truth 154) but breaks point 15, whose
newly-final shot has a NULL bounce so R6 fabricates an `Error` and hands the
point to 154. **2/3 either way** — the error moves rather than disappears.

Rally *membership and length* are strictly better (3/3 correct, `max_rally_length`
16 → 14 matching the video). So: enable it together with the R6 fix, after which
all three points are correct. Enabling alone buys rally length and churns two
point winners.

**Ace guard still owed** — point 17 truncated to the serve satisfies `ace_d`, but
the owner confirms the returner *did* return, into the net. An undetected return
becomes a fabricated ace.

## Other bronze signals — full sweep (17 tables)

Checked every bronze table on `052786b4`, not just the two sampled in the recon:

| signal | coverage | verdict |
|---|---|---|
| `player_swing.is_in_rally` | 78% / 88% / **6%** | used, coverage-gated (above) |
| `player_swing.rally_start_s` / `rally_end_s` | 87/112 | same segmentation as above |
| `bronze.rally` (start_ts/end_ts/len_s) | 27 windows | same segmentation, third view |
| `player_swing.confidence` | **100%** | **unused — best untapped signal** |
| `bronze.ball_position` | **5591 rows** | **unused — trajectory; the honest route to in/out and net contact (R6)** |
| `bronze.player_position` | 6636 rows | unused |
| `session_confidences` | 1 row | unused — the natural per-match quality gate |
| `player_swing.ball_impact_type` | **0%** | **NULL everywhere — "net is recorded" is NOT true** |
| `intercepting_player_id`, `ball_impact_location_x/y` | 0% | NULL everywhere |
| `bronze.debug_event` | 0 rows | the known `debug_data` key-name bug |

**Naming trap worth recording:** `shot_phase_d = 'Net'` (e.g. point 16's
"NetError") means *the player was standing near the net* — a court zone derived
from hit position between the service lines — **not** that the ball struck the
net. Net contact is not recorded anywhere in bronze today.
