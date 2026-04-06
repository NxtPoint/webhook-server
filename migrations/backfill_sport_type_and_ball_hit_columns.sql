-- backfill_sport_type_and_ball_hit_columns.sql
-- One-time migration.  Safe to re-run (all updates are idempotent / WHERE … IS NULL guarded).
-- Run AFTER db_init / _run_bronze_init has created the new columns.

-- 1) submission_context.sport_type — default existing rows to tennis_singles
UPDATE bronze.submission_context
   SET sport_type = 'tennis_singles'
 WHERE sport_type IS NULL;

-- 2) player_swing — populate ball_hit_s, ball_hit_frame, ball_hit_location_x/y
--    from existing ball_hit / ball_hit_location JSONB blobs.
UPDATE bronze.player_swing
   SET ball_hit_s = CASE
         WHEN ball_hit IS NOT NULL
          AND jsonb_typeof(ball_hit) = 'object'
         THEN (ball_hit ->> 'timestamp')::double precision
         ELSE NULL
       END,
       ball_hit_frame = CASE
         WHEN ball_hit IS NOT NULL
          AND jsonb_typeof(ball_hit) = 'object'
         THEN (ball_hit ->> 'frame_nr')::integer
         ELSE NULL
       END,
       ball_hit_location_x = CASE
         WHEN ball_hit_location IS NOT NULL
          AND jsonb_typeof(ball_hit_location) = 'array'
         THEN (ball_hit_location ->> 0)::double precision
         ELSE NULL
       END,
       ball_hit_location_y = CASE
         WHEN ball_hit_location IS NOT NULL
          AND jsonb_typeof(ball_hit_location) = 'array'
         THEN (ball_hit_location ->> 1)::double precision
         ELSE NULL
       END
 WHERE ball_hit_s IS NULL
   AND ball_hit IS NOT NULL;

-- 3) player_swing — populate ball_impact_location_x/y from existing blob
UPDATE bronze.player_swing
   SET ball_impact_location_x = CASE
         WHEN ball_impact_location IS NOT NULL
          AND jsonb_typeof(ball_impact_location) = 'array'
         THEN (ball_impact_location ->> 0)::double precision
         ELSE NULL
       END,
       ball_impact_location_y = CASE
         WHEN ball_impact_location IS NOT NULL
          AND jsonb_typeof(ball_impact_location) = 'array'
         THEN (ball_impact_location ->> 1)::double precision
         ELSE NULL
       END
 WHERE ball_impact_location_x IS NULL
   AND ball_impact_location IS NOT NULL;

-- 4) player_swing — populate rally_start_s/rally_end_s from rally blob [start_s, end_s]
UPDATE bronze.player_swing
   SET rally_start_s = CASE
         WHEN rally IS NOT NULL
          AND jsonb_typeof(rally) = 'array'
         THEN (rally ->> 0)::double precision
         ELSE NULL
       END,
       rally_end_s = CASE
         WHEN rally IS NOT NULL
          AND jsonb_typeof(rally) = 'array'
         THEN (rally ->> 1)::double precision
         ELSE NULL
       END
 WHERE rally_start_s IS NULL
   AND rally IS NOT NULL;
