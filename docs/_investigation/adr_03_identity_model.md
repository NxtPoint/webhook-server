# ADR-03: Player identity model (stable A/B across changeovers)

**Status:** APPROVED 2026-05-28 (architectural + research-grounded spec). v1 rule build can start immediately. v2 CNN build queued after corpus accumulates >= 10 matches of dual-submit identity labels.
**Owner:** Tomo decides; any agent can implement post-approval.
**Sequence:** see [ADR-05](./adr_05_detector_build_sequencing.md). Independent of bounce (ADR-01) and swing-type (ADR-02) — can run in parallel.
**Last updated:** 2026-05-28.

## Context

Per [bronze_silver_18_audit.md](./bronze_silver_18_audit.md):

> "player_id (who) | ⚠️ only side-based in every table; **no stable identity** | RE-DERIVES by court side | model gap (identity) — stopgap"

Today every bronze table identifies players by track-id (1/2 from YOLOv8 tracker) and silver maps them to "near/far" by court_y. This works within a game but fails across **changeovers** — every odd game (1, 3, 5, 7…) per ITF tennis rules the players swap sides, and no model emits the resulting A/B identity flip. CLAUDE.md "Things not to do" #11 cites this as one of the four far-court ceiling fields: "A/B identity NOT solved (Q2-B blocked)."

This blocks the `T5_STROKE_DRIVEN_SILVER` path (currently gated OFF per CLAUDE.md rule #11) and means dashboards that show per-player stats are unreliable across set boundaries.

## Sub-questions

1. **Approach** — appearance-based re-identification CNN, rule-based changeover detector, or formally accept "Near/Far" as final?
2. **Initial A/B mapping** — how does the user tell us which player is A vs B at match start?
3. **Storage** — per-game flip table, single-global flip column, or per-frame identity stream?
4. **Where the work lives** — Batch-side (during detection), Render-side (post-process), or part of an existing module?

## Options

### Q1 — approach
| Option | Pros | Cons |
|---|---|---|
| **A. Rule-based changeover detector** (consumes ball/player positions between games + ITF odd-game rule) | Cheap; no training; tennis rules are deterministic; can be built immediately with no corpus extension; floor accuracy probably 80-90% | Fails when changeover doesn't happen at the expected moment (injuries, time-violations, etc.); needs a clean "game boundary" signal from upstream point/game structure |
| **B. Appearance-based re-identification CNN** (gait, body shape, dominant hand) | Catches edge cases the rule misses; gets stronger with more data | Requires training corpus + model + weights; weeks of work; pose-feature based re-id is mature but adds Render inference cost |
| **C. Formally accept "Near/Far"** as final state; users tag A/B once per match at upload | Zero code; honest about what we can deliver | Doesn't solve the Q2-B blocker for stroke-driven silver; coach UX worse (they think in player names, not sides) |
| **D. Rule first, CNN later** | Build phase = rule (option A); training phase = CNN (option B) for the residual; matches "build-first/train-LAST" recipe | Two builds — but that's the project's standard recipe, not a cost |

### Q2 — initial A/B mapping
| Option | Pros | Cons |
|---|---|---|
| **A. Already-existing upload form fields** (`player_a_name`, `player_b_name` in `bronze.submission_context`) | Already collected; user already maps them to A/B before submitting | User has to know which side they're on — but they do (it's their match) |
| **B. Add a "near at match start" picker to upload form** | Explicit | Extra friction on every upload |

### Q3 — storage
| Option | Pros | Cons |
|---|---|---|
| **A. Single global flip column** on `video_analysis_jobs` (e.g. `players_switched_first_change=true`) | Trivial schema | Only models one changeover; fails for 3-set matches with multiple sides |
| **B. Per-game flip table** `ml_analysis.player_identity_segments (job_id, game_number, player_a_side, player_b_side)` | Generalises to any number of side-swaps; clean join from silver | One more table |
| **C. Per-frame identity stream** | Maximum granularity | Massively overkill for what is fundamentally a discrete per-game event |

### Q4 — placement
| Option | Pros | Cons |
|---|---|---|
| **A. Render-side, after `serve_detector` runs** (it produces the rally state needed for game boundaries) | Has all inputs available; no Batch deploy; lives in `ml_pipeline/identity_detector/` parallel to serve_detector | Adds another step to the Render ingest flow |
| **B. Inside `build_silver_match_t5.py` Pass 1** | One less module | Violates "silver does no work" — identity is a base fact; it belongs in bronze |
| **C. Inside an existing module** (e.g. `serve_detector` extends to emit identity events) | One fewer module | Overloads serve_detector's responsibility; harder to bench in isolation |

## Recommendation

**Q1: D — Rule first, CNN later.**
Build phase: rule-based changeover detector consuming `serve_events` (rally state) + `player_detections` (court_y per player_id over time). The ITF odd-game rule + a check that both players cross sides during the inter-game gap is a high-precision heuristic. Floor target: 90% identity accuracy across 5-game matches. Training phase: a small re-id CNN trained on dual-submit corpus (where SA has stable A/B labels) fills the residual.

**Q2: A — Reuse the existing `player_a_name` / `player_b_name` fields** from the upload form. The model maps "track-id 1 at game 1 = which name" by combining: (1) the upload form's implicit "owner is on the near side" assumption (validate this is true in the form copy), or (2) an explicit "are you near or far at the start?" radio button on the form (minor friction). Discussion at build time. Either way: no new model input needed.

**Q3: B — Per-game flip table.** Schema:
```sql
CREATE TABLE ml_analysis.player_identity_segments (
  id BIGSERIAL PRIMARY KEY,
  job_id UUID NOT NULL,
  game_number INT NOT NULL,
  player_a_side TEXT NOT NULL,  -- 'near' or 'far'
  player_b_side TEXT NOT NULL,
  confidence FLOAT,
  source TEXT,  -- 'rule_v1' / 'reid_cnn_v1'
  UNIQUE (job_id, game_number)
);
```
Silver joins on `game_number` for any per-player aggregation.

**Q4: A — Render-side standalone module** `ml_pipeline/identity_detector/`. Mirrors the proven serve_detector / bounce_detector shape (post-ADR-01). Inputs: `serve_events` (game boundaries), `player_detections` (per-player court_y per frame), `submission_context` (initial A/B mapping). Output: `ml_analysis.player_identity_segments`.

## Open follow-ups (decide at build time)

1. **Game-boundary signal source** — Where does "game N just ended" come from at the moment identity_detector runs? Need to derive game_number from `serve_events` (one game = one server holding serve through their service game) — this overlaps with point/game structure currently derived in silver pass-3. A bronze-side derivation may be needed. Coordinate with parallel agent.
2. **Tie-break handling** — players also swap every 6 points in a tie-break. Model has to handle this.
3. **Two-player only** — assumes singles. Doubles requires more thought; out of scope for v1.
4. **Failure mode UI** — when the model is uncertain (low confidence), how does the dashboard surface it? Probably as "identity uncertain — review and tag manually" in the locker room.

---

## Build spec v1 (research-grounded, 2026-05-28)

### v1 rule-based algorithm (build phase, no training)

Inputs:
- `serve_events`: {job_id, point_id, set_no, game_no, server_track_id, t_start, t_end}
- `player_detections`: {job_id, frame, track_id, court_x, court_y}
- `submission_context`: {player_a_name, player_b_name, **`a_starts_near` BOOLEAN — new field, see UX section**}

Algorithm:
```
# 1. Game-boundary derivation: see next section
# 2. Inter-game gap window = [t_end_game_N, t_start_game_N+1]
# 3. For each track_id:
#      side_before = "near" if median(court_y, [gap_start - 5s, gap_start]) > court_center else "far"
#      side_after  = "near" if median(court_y, [gap_end,    gap_end + 5s]) > court_center else "far"
# 4. CHANGEOVER_DETECTED = (side_before[1] != side_after[1]) AND (side_before[2] != side_after[2])
# 5. EXPECTED_CHANGEOVER per ITF: True if game_no in {1, 3, 5, 7, 9, 11} AND every 6 points in tiebreak
# 6. Decision matrix:
#    - rule fires cleanly (detected == expected):          confidence = 0.95
#    - expected but not detected, gap > 90s:               assume changeover (medical break), conf = 0.6
#    - expected but not detected, gap <= 90s:              assume no changeover (towel only),  conf = 0.5
#    - not expected but detected:                          tracker ID swap anomaly, conf = 0.4, source='rule_v1_anomaly'
# 7. Persist one row per game in ml_analysis.player_identity_segments.
```

### Game-boundary derivation (bronze-side, no silver dependency)

Server-alternation invariant from `serve_events`:
```
games = []
current_game_serves = []
current_server = serve_events[0].server_track_id
for s in serve_events:
    if s.server_track_id == current_server:
        current_game_serves.append(s)
    else:
        games.append((median(s.t_start for s in current_game_serves[:3]),
                      current_game_serves[-1].t_end,
                      current_server))
        current_game_serves = [s]; current_server = s.server_track_id
games.append(...)  # flush last
# game_number = index + 1
# Tiebreak: detect when previous game count in set >= 12; during tiebreak server alternates every 2 points,
#   wrap with a tiebreak-specific state machine emitting a single game_number for the whole tiebreak
```

### v2 re-id CNN architecture (training-phase upgrade)

**OSNet_x1_0** (Zhou et al. ICCV 2019, arXiv 1905.00953) as appearance backbone — 2.2M params (10× lighter than ResNet50's 24M), production-proven for sports re-id. Crop from YOLOv8-pose bbox, 256×128 input. Fine-tune on dual-submit corpus crops where SportAI provides stable A/B labels. Output: 512-dim embedding; cosine-similarity to per-game running centroid for each identity. Fuse with motion (Kalman/IoU) following **BoT-SORT** (Aharon 2022) pattern with camera-motion compensation.

Why not TransReID/ViT-B: tennis has 2 IDs (not 22 in soccer), fixed wide-angle camera (not broadcast cuts), Render inference budget. OSNet's 10× param savings is the right tradeoff (Suglia 2022 confirmed tennis re-id is easier than team-sport re-id).

### Confidence + uncertainty model

Three-tier output per game row: `(player_a_side, player_b_side, confidence FLOAT, source TEXT)`.

| Confidence range | Downstream behaviour |
|---|---|
| ≥ 0.9 | Silver uses A/B labels directly |
| 0.5–0.9 | Silver falls back to near/far for this game; flag `identity_uncertain=true` on join |
| < 0.5 | Row written with `source='needs_review'`; dashboard surfaces "identity uncertain — review and tag manually" |

### Initial A/B mapping (per ADR-03 decision, 2026-05-28)

**Reuse existing `player_a_name` / `player_b_name` AND add one new boolean** `a_starts_near` (defaults to `true` with helper text "Player A is on the camera side at the start of the match"). One extra tap per upload; eliminates the misattribution failure mode for coach-uploaded and third-party-recorded matches. SwingVision precedent confirms the industry pattern.

### Output table

```sql
CREATE TABLE ml_analysis.player_identity_segments (
  id BIGSERIAL PRIMARY KEY,
  job_id UUID NOT NULL,
  game_number INT NOT NULL,
  player_a_side TEXT NOT NULL,   -- 'near' or 'far'
  player_b_side TEXT NOT NULL,
  confidence FLOAT,
  source TEXT,                   -- 'rule_v1' | 'rule_v1_anomaly' | 'rule_v1_terminated' | 'reid_cnn_v1' | 'needs_review'
  UNIQUE (job_id, game_number)
);
```

### Top 3 references
1. Zhou et al. ICCV 2019, *Omni-Scale Feature Learning for Person Re-Identification*, arXiv 1905.00953 — OSNet backbone choice.
2. Suglia et al. 2022, *Sports Re-ID*, arXiv 2206.02373 — sports vs pedestrian re-id benchmarking; informs why tennis is easier than soccer.
3. Aharon et al. 2022, BoT-SORT (GitHub NirAharon/BoT-SORT) — motion + appearance fusion with camera-motion compensation.

---

## Cross-references

- [bronze_silver_18_audit.md](./bronze_silver_18_audit.md) — model-gap framing.
- [far_player_accuracy.md](./far_player_accuracy.md) — why this is a far-court ceiling field.
- CLAUDE.md "Things not to do" #11 — references this gap; the `T5_STROKE_DRIVEN_SILVER` gate is blocked on identity.
- [ADR-05](./adr_05_detector_build_sequencing.md) — sequencing (identity rule is the fastest win — no corpus extension needed).
