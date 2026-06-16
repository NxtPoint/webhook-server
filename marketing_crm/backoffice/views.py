# marketing_crm/backoffice/views.py — cockpit analytic views over core.* (+ bronze for ops).
#
# Lifecycle stages + business-health definitions follow marketing_crm/contracts/lifecycle_stages.md
# exactly — one definition, no drift. Idempotent (CREATE OR REPLACE). Aggregation in SQL (rule #2).
#
# Dependency: core.vw_account_credits / vw_subscription_current / vw_mrr (from core_db.schema).
# init_cockpit_views() calls core_init() first so those exist.

from sqlalchemy import text

from core_db.schema import core_init

_VIEWS = [
    # ── Base: per-account facts + derived lifecycle stage ────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_account_lifecycle AS
    SELECT
        a.id AS account_id, a.public_id, a.email, a.display_name, a.created_at,
        pp.role,
        COALESCE(mc.matches_uploaded, 0)  AS matches_uploaded,
        COALESCE(mc.matches_completed, 0) AS matches_completed,
        COALESCE(rv.reports_viewed, 0)    AS reports_viewed,
        act.last_activity,
        COALESCE(cr.matches_remaining, 0) AS matches_remaining,
        sc.plan_code, sc.plan_type, sc.status AS sub_status,
        COALESCE(sc.mrr_cents, 0)         AS mrr_cents,
        np.nps_latest,
        ((COALESCE(mc.matches_completed,0) >= 1) AND (COALESCE(rv.reports_viewed,0) >= 1)) AS activated,
        CASE
            WHEN sc.status = 'active' AND sc.plan_type = 'recurring' THEN
                CASE WHEN act.last_activity IS NULL OR act.last_activity < now() - interval '30 days'
                     THEN 'at_risk' ELSE 'paid' END
            WHEN COALESCE(cr.matches_remaining, 0) > 0 THEN 'payg'
            WHEN sc.status IN ('cancelled', 'expired')  THEN 'churned'
            WHEN COALESCE(mc.matches_uploaded, 0) >= 1  THEN 'trial'
            ELSE 'signup'
        END AS stage
    FROM core.account a
    LEFT JOIN LATERAL (
        SELECT role FROM core.person pr
        WHERE pr.account_id = a.id AND pr.deleted_at IS NULL
        ORDER BY pr.is_primary DESC, pr.id LIMIT 1
    ) pp ON true
    LEFT JOIN core.vw_account_credits cr     ON cr.account_id = a.id
    LEFT JOIN core.vw_subscription_current sc ON sc.account_id = a.id
    LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE m.deleted_at IS NULL) AS matches_uploaded,
               count(*) FILTER (WHERE m.status = 'complete' AND m.deleted_at IS NULL) AS matches_completed
        FROM core.match m WHERE m.account_id = a.id
    ) mc ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS reports_viewed FROM core.usage_event ue
        WHERE ue.account_id = a.id AND ue.event_type IN ('report_view','report_viewed','dashboard_view')
    ) rv ON true
    LEFT JOIN LATERAL (
        SELECT max(occurred_at) AS last_activity FROM core.usage_event ue WHERE ue.account_id = a.id
    ) act ON true
    LEFT JOIN LATERAL (
        SELECT score AS nps_latest FROM core.nps_response n
        WHERE n.account_id = a.id ORDER BY submitted_at DESC LIMIT 1
    ) np ON true
    WHERE a.deleted_at IS NULL
    """,

    # ── Tab 1: business health (single row of scalars) ───────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_business_health AS
    SELECT
        (SELECT count(*) FROM core.account WHERE deleted_at IS NULL)                           AS total_accounts,
        (SELECT mrr_cents_total FROM core.vw_mrr)                                              AS mrr_cents,
        (SELECT active_subscriptions FROM core.vw_mrr)                                         AS active_subscriptions,
        (SELECT count(*) FROM core.account
           WHERE deleted_at IS NULL AND created_at >= date_trunc('month', now()))             AS new_accounts_this_month,
        (SELECT count(*) FROM core.subscription
           WHERE cancelled_at >= date_trunc('month', now()))                                  AS churned_this_month,
        (SELECT count(*) FROM core.vw_account_lifecycle WHERE activated)                       AS activated_accounts,
        (SELECT count(*) FROM core.vw_account_lifecycle WHERE stage IN ('paid','at_risk'))     AS paid_accounts,
        ROUND( (SELECT count(*) FROM core.vw_account_lifecycle WHERE activated)::numeric
               / NULLIF((SELECT count(*) FROM core.account WHERE deleted_at IS NULL), 0), 4)   AS activation_rate,
        ROUND( (SELECT count(DISTINCT account_id) FROM core.subscription)::numeric
               / NULLIF((SELECT count(DISTINCT account_id) FROM core.match WHERE deleted_at IS NULL), 0), 4)
                                                                                              AS free_to_paid_rate,
        (SELECT COALESCE(SUM(p.price_cents), 0)
           FROM core.credit_ledger cl JOIN core.plan p ON p.code = cl.plan_code
          WHERE cl.source = 'payg_purchase' AND cl.entry_type = 'grant')                       AS payg_revenue_cents
    """,

    # ── Tab 1: active subscriptions by plan ──────────────────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_subs_by_plan AS
    SELECT COALESCE(plan_code, '(none)') AS plan_code, plan_type,
           count(*) AS active_count, COALESCE(SUM(mrr_cents), 0) AS mrr_cents
    FROM core.subscription
    WHERE status = 'active'
    GROUP BY plan_code, plan_type
    ORDER BY mrr_cents DESC
    """,

    # ── Tab 2: searchable customer list ──────────────────────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_customer_list AS
    SELECT l.account_id, l.public_id, l.email, l.display_name, l.role, l.stage, l.activated,
           l.plan_code, l.plan_type, l.sub_status, l.mrr_cents,
           l.matches_uploaded, l.matches_remaining, l.last_activity, l.nps_latest, l.created_at,
           COALESCE(ln.linked_count, 0) AS linked_count
    FROM core.vw_account_lifecycle l
    LEFT JOIN LATERAL (
        SELECT count(*) AS linked_count
        FROM core.relationship r
        JOIN core.person ps ON (ps.id = r.from_person_id OR ps.id = r.to_person_id)
        WHERE ps.account_id = l.account_id AND r.status = 'active'
    ) ln ON true
    """,

    # ── Tab 3: at-risk & opportunity (one row-set, category-tagged) ──────────
    """
    CREATE OR REPLACE VIEW core.vw_at_risk AS
    SELECT 'trial_no_upload' AS category, account_id, email, display_name,
           'No match uploaded yet' AS detail, 0::numeric AS metric, last_activity
    FROM core.vw_account_lifecycle
    WHERE matches_uploaded = 0
    UNION ALL
    SELECT 'inactive_subscriber', account_id, email, display_name,
           'No activity 30+ days', EXTRACT(day FROM now() - last_activity)::numeric, last_activity
    FROM core.vw_account_lifecycle
    WHERE stage = 'at_risk'
    UNION ALL
    SELECT 'coach_linkable', l.account_id, l.email, l.display_name,
           'Coach — ' || COALESCE(lc.cnt, 0) || ' linked player(s)', COALESCE(lc.cnt, 0)::numeric, l.last_activity
    FROM core.vw_account_lifecycle l
    JOIN LATERAL (
        SELECT count(*) AS cnt FROM core.relationship r
        JOIN core.person cp ON cp.id = r.from_person_id
        WHERE cp.account_id = l.account_id AND r.type = 'coach_player' AND r.status = 'active'
    ) lc ON true
    WHERE l.role = 'coach'
    """,

    # ── Tab 4: match / processing ops ────────────────────────────────────────
    # Reads bronze.submission_context (the LIVE ingest/processing state) so this tab is
    # useful with real data immediately — core.match mirrors it post-migration.
    """
    CREATE OR REPLACE VIEW core.vw_processing_ops AS
    SELECT
        sc.task_id, sc.email, sc.sport_type,
        CASE
            WHEN sc.ingest_error IS NOT NULL    THEN 'failed'
            WHEN sc.ingest_finished_at IS NOT NULL THEN 'complete'
            WHEN sc.ingest_started_at IS NOT NULL  THEN 'processing'
            ELSE 'queued'
        END AS derived_status,
        sc.last_status, sc.trim_status,
        sc.ingest_started_at, sc.ingest_finished_at, sc.ingest_error
    FROM bronze.submission_context sc
    WHERE sc.deleted_at IS NULL
    """,

    # ── Feedback: NPS summary (scalars) ──────────────────────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_nps_summary AS
    SELECT
        count(*)                                          AS responses,
        count(*) FILTER (WHERE bucket = 'promoter')       AS promoters,
        count(*) FILTER (WHERE bucket = 'passive')        AS passives,
        count(*) FILTER (WHERE bucket = 'detractor')      AS detractors,
        CASE WHEN count(*) = 0 THEN NULL
             ELSE ROUND((count(*) FILTER (WHERE bucket='promoter')
                         - count(*) FILTER (WHERE bucket='detractor')) * 100.0 / count(*), 1)
        END                                               AS nps
    FROM core.nps_response
    """,

    # ── Feedback: NPS by month (trend) ───────────────────────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_nps_monthly AS
    SELECT date_trunc('month', submitted_at) AS month,
           count(*) AS responses,
           ROUND((count(*) FILTER (WHERE bucket='promoter')
                  - count(*) FILTER (WHERE bucket='detractor')) * 100.0 / NULLIF(count(*),0), 1) AS nps
    FROM core.nps_response
    GROUP BY 1 ORDER BY 1 DESC
    """,
]


def init_cockpit_views(engine=None):
    """Create/refresh the cockpit views (idempotent). Ensures core base schema/views first."""
    engine = core_init(engine)  # guarantees core.* tables + vw_account_credits/subscription_current/mrr
    with engine.begin() as conn:
        for stmt in _VIEWS:
            conn.execute(text(stmt))
    return engine


if __name__ == "__main__":
    eng = init_cockpit_views()
    print(f"cockpit views initialised on {eng.url.render_as_string(hide_password=True)}")
