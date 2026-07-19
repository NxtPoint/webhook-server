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
- **`ball_position` has two contradictory DDLs.** `db_init.py:252` declares `x/y/timestamp` `GENERATED ALWAYS`; `ingest_bronze.py:227` declares the same three as plain columns. Both use `ADD COLUMN IF NOT EXISTS`, so **boot order decides**, and the insert path writes them explicitly — if the GENERATED version ever wins on a fresh DB, every ingest dies with `cannot insert a non-DEFAULT value`.
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
