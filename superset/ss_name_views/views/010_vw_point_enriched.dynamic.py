def make_sql(cur):
    return r"""
    -- 010: enrich point rows with selected fields from public.submission_context.raw_meta
    CREATE SCHEMA IF NOT EXISTS ss_;

    -- (Optional) helpful index
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname='public' AND indexname='ix_submission_context_session_id'
      ) THEN
        CREATE INDEX ix_submission_context_session_id
          ON public.submission_context (session_id);
      END IF;
    END$$;

    CREATE OR REPLACE VIEW ss_.vw_point_enriched AS
    WITH ctx AS (
      SELECT
        sc.session_id,
        sc.task_id,
        sc.created_at,

        -- exact keys as requested:
        (sc.raw_meta->>'email')::text           AS email,
        (sc.raw_meta->>'customer_name')::text   AS customer_name,

        -- match_date as DATE if in YYYY-MM-DD; else NULL to avoid cast errors
        CASE
          WHEN (sc.raw_meta->>'match_date') ~ '^\d{4}-\d{2}-\d{2}$'
            THEN (sc.raw_meta->>'match_date')::date
          ELSE NULL
        END AS match_date,

        -- start_time as TIME if HH:MM or HH:MM:SS; else NULL
        CASE
          WHEN (sc.raw_meta->>'start_time') ~ '^\d{2}:\d{2}(:\d{2})?$'
            THEN (sc.raw_meta->>'start_time')::time
          ELSE NULL
        END AS start_time,

        (sc.raw_meta->>'location')::text        AS location,
        (sc.raw_meta->>'player_a_name')::text   AS player_a_name,
        (sc.raw_meta->>'player_b_name')::text   AS player_b_name,

        -- safe numeric casts for UTR
        CASE
          WHEN (sc.raw_meta->>'player_a_utr') ~ '^\d+(\.\d+)?$'
            THEN (sc.raw_meta->>'player_a_utr')::numeric
          ELSE NULL
        END AS player_a_utr,
        CASE
          WHEN (sc.raw_meta->>'player_b_utr') ~ '^\d+(\.\d+)?$'
            THEN (sc.raw_meta->>'player_b_utr')::numeric
          ELSE NULL
        END AS player_b_utr

      FROM public.submission_context sc
    )
    SELECT
      p.*,
      c.task_id,
      c.created_at,
      c.email,
      c.customer_name,
      c.match_date,
      c.start_time,
      c.location,
      c.player_a_name,
      c.player_b_name,
      c.player_a_utr,
      c.player_b_utr
      -- NOTE: session_id already comes from p.* (silver.vw_point_silver)
    FROM silver.vw_point_silver p
    LEFT JOIN ctx c
      ON c.session_id = p.session_id;
    """
