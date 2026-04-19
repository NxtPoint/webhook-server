# technique/gold_technique.py — Gold presentation views for technique analysis.
#
# Pattern mirrors gold_init.py: DROP VIEW IF EXISTS + CREATE VIEW, each wrapped
# in try/except so one failure doesn't block the rest.
#
# Views:
#   gold.technique_report       — complete per-analysis report
#   gold.technique_comparison   — player vs benchmark by level
#   gold.technique_kinetic_chain_summary — simplified chain insights
#   gold.technique_progression  — cross-session improvement dashboard

from __future__ import annotations

import logging

from sqlalchemy import text

from db_init import engine

log = logging.getLogger(__name__)


# ============================================================================
# VIEW SQL
# ============================================================================

TECHNIQUE_REPORT_SQL = """
CREATE VIEW gold.technique_report AS
SELECT
    ts.task_id,
    ts.email,
    ts.sport,
    ts.swing_type,
    ts.dominant_hand,
    ts.player_height_mm,
    ts.overall_score,
    ts.level,
    ts.feature_count,
    ts.category_count,
    ts.top_strength,
    ts.top_improvement,
    ts.analysis_status,
    ts.warnings,
    ts.errors,
    ts.analysed_at,
    sc.player_a_name,
    sc.match_date   AS analysis_date,
    sc.location,

    -- Category scores as columns (top categories)
    (SELECT jsonb_object_agg(fc.category_name, fc.category_score)
     FROM bronze.technique_feature_categories fc
     WHERE fc.task_id = ts.task_id
    ) AS category_scores,

    -- Top 3 strengths (highest scoring features)
    (SELECT jsonb_agg(jsonb_build_object(
        'name', fe.feature_human_readable,
        'score', fe.score,
        'observation', fe.observation
     ) ORDER BY fe.score DESC)
     FROM (SELECT * FROM silver.technique_features_enriched
           WHERE task_id = ts.task_id AND score IS NOT NULL
           ORDER BY score DESC LIMIT 3) fe
    ) AS top_strengths,

    -- Top 3 improvements (lowest scoring features)
    (SELECT jsonb_agg(jsonb_build_object(
        'name', fe.feature_human_readable,
        'score', fe.score,
        'suggestion', fe.suggestion
     ) ORDER BY fe.score ASC)
     FROM (SELECT * FROM silver.technique_features_enriched
           WHERE task_id = ts.task_id AND score IS NOT NULL
           ORDER BY score ASC LIMIT 3) fe
    ) AS top_improvements,

    -- All features as JSON array
    (SELECT jsonb_agg(jsonb_build_object(
        'name', fe.feature_human_readable,
        'feature_name', fe.feature_name,
        'score', fe.score,
        'value', fe.value,
        'level', fe.level,
        'category', fe.category_name,
        'observation', fe.observation,
        'suggestion', fe.suggestion
     ) ORDER BY fe.category_name, fe.score DESC)
     FROM silver.technique_features_enriched fe
     WHERE fe.task_id = ts.task_id
    ) AS all_features

FROM silver.technique_summary ts
LEFT JOIN bronze.submission_context sc
    ON sc.task_id = ts.task_id
WHERE ts.analysis_status = 'done'
"""


TECHNIQUE_COMPARISON_SQL = """
CREATE VIEW gold.technique_comparison AS
SELECT
    fe.task_id,
    ts.email,
    ts.swing_type,
    fe.feature_name,
    fe.feature_human_readable,
    fe.score,
    fe.value,
    fe.level,
    fe.category_name,

    -- Extract benchmark ranges for comparison
    (fe.score_ranges->>'beginner')::jsonb     AS beginner_range,
    (fe.score_ranges->>'intermediate')::jsonb AS intermediate_range,
    (fe.score_ranges->>'advanced')::jsonb     AS advanced_range,
    (fe.score_ranges->>'professional')::jsonb AS professional_range,

    -- Value ranges
    (fe.value_ranges->>'beginner')::jsonb     AS value_beginner_range,
    (fe.value_ranges->>'intermediate')::jsonb AS value_intermediate_range,
    (fe.value_ranges->>'advanced')::jsonb     AS value_advanced_range,
    (fe.value_ranges->>'professional')::jsonb AS value_professional_range,

    fe.observation,
    fe.suggestion

FROM silver.technique_features_enriched fe
JOIN silver.technique_summary ts ON ts.task_id = fe.task_id
WHERE fe.score IS NOT NULL
"""


TECHNIQUE_KINETIC_CHAIN_SUMMARY_SQL = """
CREATE VIEW gold.technique_kinetic_chain_summary AS
WITH chain_agg AS (
    SELECT
        kc.task_id,
        COUNT(*)                                          AS segment_count,
        BOOL_AND(kc.is_sequential) FILTER (WHERE kc.peak_order > 1) AS chain_is_sequential,
        MAX(kc.peak_speed)                                AS max_peak_speed,
        MIN(kc.peak_speed)                                AS min_peak_speed,

        -- Ordered segment names by peak timing
        string_agg(kc.segment_name, ' -> ' ORDER BY kc.peak_order)
            AS chain_sequence,

        -- Segment with highest peak speed
        (SELECT segment_name FROM silver.technique_kinetic_chain_analysis sub
         WHERE sub.task_id = kc.task_id
         ORDER BY sub.peak_speed DESC NULLS LAST LIMIT 1)
            AS fastest_segment,

        -- Segment with lowest peak speed (potential lagging segment)
        (SELECT segment_name FROM silver.technique_kinetic_chain_analysis sub
         WHERE sub.task_id = kc.task_id AND sub.peak_speed IS NOT NULL
         ORDER BY sub.peak_speed ASC LIMIT 1)
            AS slowest_segment,

        -- Total time from first to last peak
        MAX(kc.peak_timestamp) - MIN(kc.peak_timestamp) AS chain_duration_s,

        -- All segments as JSON for charting
        jsonb_agg(jsonb_build_object(
            'segment', kc.segment_name,
            'peak_speed', kc.peak_speed,
            'peak_timestamp', kc.peak_timestamp,
            'order', kc.peak_order
        ) ORDER BY kc.peak_order) AS segments_json

    FROM silver.technique_kinetic_chain_analysis kc
    GROUP BY kc.task_id
)
SELECT
    ca.task_id,
    ts.email,
    ts.swing_type,
    ca.segment_count,
    ca.chain_is_sequential,
    ca.max_peak_speed,
    ca.min_peak_speed,
    ca.chain_sequence,
    ca.fastest_segment,
    ca.slowest_segment,
    ca.chain_duration_s,
    ca.segments_json
FROM chain_agg ca
JOIN silver.technique_summary ts ON ts.task_id = ca.task_id
"""


TECHNIQUE_PROGRESSION_SQL = """
CREATE VIEW gold.technique_progression AS
WITH ranked AS (
    SELECT
        tt.email,
        tt.swing_type,
        tt.feature_name,
        tt.task_id,
        tt.score,
        tt.value,
        tt.level,
        tt.analysed_at,
        ROW_NUMBER() OVER (
            PARTITION BY tt.email, tt.swing_type, tt.feature_name
            ORDER BY tt.analysed_at DESC
        ) AS recency_rank,
        COUNT(*) OVER (
            PARTITION BY tt.email, tt.swing_type, tt.feature_name
        ) AS session_count,
        AVG(tt.score) OVER (
            PARTITION BY tt.email, tt.swing_type, tt.feature_name
            ORDER BY tt.analysed_at
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS rolling_avg_5,
        LAG(tt.score) OVER (
            PARTITION BY tt.email, tt.swing_type, tt.feature_name
            ORDER BY tt.analysed_at
        ) AS prev_score,
        FIRST_VALUE(tt.score) OVER (
            PARTITION BY tt.email, tt.swing_type, tt.feature_name
            ORDER BY tt.analysed_at
        ) AS first_score
    FROM silver.technique_trends tt
)
SELECT
    r.email,
    r.swing_type,
    r.feature_name,
    r.task_id,
    r.score           AS latest_score,
    r.value           AS latest_value,
    r.level           AS latest_level,
    r.analysed_at,
    r.session_count,
    r.rolling_avg_5,
    r.prev_score,
    r.first_score,
    CASE WHEN r.prev_score IS NOT NULL
         THEN ROUND((r.score - r.prev_score)::numeric, 2)
         ELSE NULL END AS delta_vs_prev,
    CASE WHEN r.first_score IS NOT NULL AND r.session_count > 1
         THEN ROUND((r.score - r.first_score)::numeric, 2)
         ELSE NULL END AS delta_vs_first,
    CASE
        WHEN r.prev_score IS NULL THEN 'new'
        WHEN r.score > r.prev_score + 0.5 THEN 'improving'
        WHEN r.score < r.prev_score - 0.5 THEN 'declining'
        ELSE 'stable'
    END AS trend
FROM ranked r
WHERE r.recency_rank = 1
"""


# ============================================================================
# ORCHESTRATION
# ============================================================================

_TECHNIQUE_VIEWS = [
    ("gold.technique_report", TECHNIQUE_REPORT_SQL),
    ("gold.technique_comparison", TECHNIQUE_COMPARISON_SQL),
    ("gold.technique_kinetic_chain_summary", TECHNIQUE_KINETIC_CHAIN_SUMMARY_SQL),
    ("gold.technique_progression", TECHNIQUE_PROGRESSION_SQL),
]


def init_technique_gold_views() -> dict:
    """
    Idempotent recreation of gold technique views. Safe to call on every boot.
    Pattern mirrors gold_init_presentation().
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS gold"))
    except Exception:
        log.exception("[technique_gold] failed to ensure gold schema")
        return {"created": [], "failed": []}

    created: list[str] = []
    failed: list[tuple[str, str]] = []

    # Single transaction: readers block on DDL locks and see the new views
    # atomically at COMMIT, never a missing view mid-boot.
    try:
        with engine.begin() as conn:
            for name, sql in _TECHNIQUE_VIEWS:
                try:
                    conn.execute(text(f"DROP VIEW IF EXISTS {name} CASCADE"))
                    conn.execute(text(sql))
                    created.append(name)
                    log.info("[technique_gold] recreated %s", name)
                except Exception as e:
                    failed.append((name, str(e)))
                    log.error("[technique_gold] failed to recreate %s: %s", name, e)
                    raise
    except Exception:
        log.exception("[technique_gold] transaction rolled back — previous views retained")
        return {"created": [], "failed": failed or [("transaction", "rolled back")]}

    log.info("[technique_gold] %d recreated atomically", len(created))
    return {"created": created, "failed": failed}
