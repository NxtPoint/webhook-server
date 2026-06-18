# marketing_crm/backoffice/views.py — cockpit analytic views.
#
# OPTION C (2026-06-17): the cockpit reads the LIVE system-of-record directly —
#   • billing.* (subscription_state, vw_customer_usage, entitlement_grant, coaches_permission)
#   • bronze.submission_context (match / processing state)
# and LEFT JOINs core.* ONLY for the extras core actually feeds today (usage_event for
# activity/report-views, nps_response). No payment→core mirror; billing.* stays SoR, so the
# cockpit can never drift from it. See docs/_investigation/core_db_billing_strategy.md.
#
# Population spine = billing.account (every paying/uploading customer), bridged to core.account
# by lower(email) for the live usage/NPS extras (sparse until core fills — degrades to 0/NULL).
#
# Plan economics (MRR / PAYG revenue) need a price per plan_code, which the SoR has no column
# for. core.vw_plan_pricing supplies it, built from the canonical paypal_billing.plans table
# (PRICES/PLANS) + an explicit legacy-Wix code map below — ONE place to edit plan economics.
#
# Aggregation in SQL (rule #2). Idempotent (CREATE OR REPLACE). View names + output columns are
# unchanged so blueprint.py and frontend/cockpit.html need no edits.

from sqlalchemy import text

from core_db.schema import core_init
from paypal_billing.plans import PLANS, PRICES

# ── Legacy Wix plan_codes seen in the live DB but absent from the current catalogue ──────────
# (plan_class, price_major_or_None). Mapped to the closest current tier so legacy active subs
# still contribute MRR. price=None → counted as $0 MRR but still visible (it only appears in
# entitlement_grant history, never as an active subscription, so it can't affect live MRR).
# CONFIRM these legacy mappings with the business; this dict is the single source for them.
_LEGACY_PLAN_PRICING = {
    "MONTHLY_10":     ("recurring", 70.00),  # 10 matches/mo ≈ Advanced ($70)
    "coach_sub_ong":  ("recurring", 50.00),  # coach ongoing  ≈ Coach Pro ($50)
    "player_sub_5":   ("recurring", 40.00),  # 5 matches/mo   ≈ Standard ($40)
    "player_sub_100": ("recurring", None),   # legacy bulk pack — price unknown (grants only)
    "free membership": ("free", 0.00),
    "signup_trial":   ("free", 0.00),
}


def _plan_pricing_rows():
    """plan_code → (plan_class, mrr_cents, payg_cents). Recurring carries mrr_cents; payg carries
    payg_cents (per-pack price); free carries neither. Single source = plans.py + legacy map."""
    rows = {}
    for p in PLANS:
        code = p["code"]
        price = PRICES.get(code)
        cents = int(round(price * 100)) if price else 0
        if p["plan_type"] == "recurring":
            rows[code] = ("recurring", cents, 0)
        elif p["plan_type"] == "payg":
            rows[code] = ("payg", 0, cents)
        else:
            rows[code] = ("free", 0, 0)
    for code, (cls, price) in _LEGACY_PLAN_PRICING.items():
        cents = int(round(price * 100)) if price else 0
        rows.setdefault(code, (cls, cents if cls == "recurring" else 0,
                               cents if cls == "payg" else 0))
    return rows


def _pricing_view_sql():
    rows = _plan_pricing_rows()
    values = ",\n        ".join(
        "('{}', '{}', {}, {})".format(code.replace("'", "''"), cls, mrr, payg)
        for code, (cls, mrr, payg) in sorted(rows.items())
    )
    return f"""
    CREATE OR REPLACE VIEW core.vw_plan_pricing AS
    SELECT * FROM (VALUES
        {values}
    ) AS p(plan_code, plan_class, mrr_cents, payg_cents)
    """


_VIEWS = [
    # ── Base: per-account facts + derived lifecycle stage (billing-account-driven) ──────────
    """
    CREATE OR REPLACE VIEW core.vw_account_lifecycle AS
    SELECT
        b.account_id, b.public_id, b.email, b.display_name, b.created_at, b.role,
        b.matches_uploaded, b.matches_completed, b.reports_viewed, b.last_activity,
        b.matches_remaining, b.plan_code, b.plan_type, b.sub_status,
        CASE WHEN UPPER(COALESCE(b.sub_status,'')) = 'ACTIVE' AND b.plan_class = 'recurring'
             THEN b.plan_mrr_cents ELSE 0 END                          AS mrr_cents,
        b.nps_latest,
        (b.matches_completed >= 1)                                     AS activated,
        CASE
            WHEN UPPER(COALESCE(b.sub_status,'')) = 'ACTIVE' AND b.plan_class = 'recurring' THEN
                CASE WHEN b.last_activity IS NULL OR b.last_activity < now() - interval '30 days'
                     THEN 'at_risk' ELSE 'paid' END
            WHEN COALESCE(b.matches_remaining, 0) > 0                   THEN 'payg'
            WHEN UPPER(COALESCE(b.sub_status,'')) IN ('CANCELLED','EXPIRED')
                 OR b.cancelled_at IS NOT NULL                         THEN 'churned'
            WHEN COALESCE(b.matches_uploaded, 0) >= 1                   THEN 'trial'
            ELSE 'signup'
        END AS stage
    FROM (
        SELECT
            ba.id AS account_id, ca.public_id, ba.email,
            ba.primary_full_name AS display_name, ba.created_at,
            COALESCE(pm.role, 'player_parent') AS role,
            COALESCE(mc.matches_uploaded, 0)  AS matches_uploaded,
            COALESCE(mc.matches_completed, 0) AS matches_completed,
            COALESCE(ue.reports_viewed, 0)    AS reports_viewed,
            GREATEST(mc.match_last_activity, ue.usage_last) AS last_activity,
            COALESCE(cu.matches_remaining, 0) AS matches_remaining,
            ss.plan_code, ss.plan_type, ss.status AS sub_status, ss.cancelled_at,
            pp.plan_class,
            COALESCE(pp.mrr_cents, 0)         AS plan_mrr_cents,
            np.nps_latest
        FROM billing.account ba
        LEFT JOIN core.account ca
               ON lower(ca.email) = lower(ba.email) AND ca.deleted_at IS NULL
        LEFT JOIN billing.subscription_state ss ON ss.account_id = ba.id
        LEFT JOIN core.vw_plan_pricing pp       ON pp.plan_code = ss.plan_code
        LEFT JOIN billing.vw_customer_usage cu  ON cu.account_id = ba.id
        LEFT JOIN LATERAL (
            SELECT role FROM billing.member m
            WHERE m.account_id = ba.id AND m.active
            ORDER BY m.is_primary DESC, m.id LIMIT 1
        ) pm ON true
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE sc.deleted_at IS NULL) AS matches_uploaded,
                   count(*) FILTER (WHERE sc.ingest_finished_at IS NOT NULL
                                      AND sc.deleted_at IS NULL)  AS matches_completed,
                   max(COALESCE(sc.ingest_finished_at, sc.ingest_started_at)) AS match_last_activity
            FROM bronze.submission_context sc
            WHERE lower(sc.email) = lower(ba.email)
        ) mc ON true
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE ue.event_type IN
                       ('report_view','report_viewed','dashboard_view')) AS reports_viewed,
                   max(ue.occurred_at) AS usage_last
            FROM core.usage_event ue WHERE ue.account_id = ca.id
        ) ue ON true
        LEFT JOIN LATERAL (
            SELECT score AS nps_latest FROM core.nps_response n
            WHERE n.account_id = ca.id ORDER BY submitted_at DESC LIMIT 1
        ) np ON true
        WHERE ba.active
    ) b
    """,

    # ── Tab 1: business health (single row of scalars) ───────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_business_health AS
    SELECT
        (SELECT count(*) FROM billing.account WHERE active)                                   AS total_accounts,
        (SELECT COALESCE(SUM(mrr_cents), 0) FROM core.vw_account_lifecycle)                   AS mrr_cents,
        (SELECT count(*) FROM core.vw_account_lifecycle WHERE mrr_cents > 0)                  AS active_subscriptions,
        (SELECT count(*) FROM billing.account
           WHERE active AND created_at >= date_trunc('month', now()))                        AS new_accounts_this_month,
        (SELECT count(*) FROM billing.subscription_state
           WHERE COALESCE(cancelled_at, payment_cancelled_at) >= date_trunc('month', now())) AS churned_this_month,
        (SELECT count(*) FROM core.vw_account_lifecycle WHERE activated)                      AS activated_accounts,
        (SELECT count(*) FROM core.vw_account_lifecycle WHERE stage IN ('paid','at_risk'))    AS paid_accounts,
        ROUND( (SELECT count(*) FROM core.vw_account_lifecycle WHERE activated)::numeric
               / NULLIF((SELECT count(*) FROM billing.account WHERE active), 0), 4)           AS activation_rate,
        ROUND( (SELECT count(*) FROM core.vw_account_lifecycle WHERE mrr_cents > 0)::numeric
               / NULLIF((SELECT count(*) FROM core.vw_account_lifecycle
                          WHERE matches_uploaded >= 1), 0), 4)                                AS free_to_paid_rate,
        (SELECT COALESCE(SUM(pp.payg_cents), 0)
           FROM billing.entitlement_grant eg
           JOIN core.vw_plan_pricing pp ON pp.plan_code = eg.plan_code
          WHERE eg.source ILIKE '%payg%')                                                    AS payg_revenue_cents,
        (SELECT count(*) FROM billing.subscription_state ss
           LEFT JOIN core.vw_plan_pricing pp ON pp.plan_code = ss.plan_code
          WHERE UPPER(COALESCE(ss.status,'')) = 'ACTIVE' AND pp.plan_code IS NULL)            AS unpriced_active_subs
    """,

    # ── Tab 1: active subscriptions by plan ──────────────────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_subs_by_plan AS
    SELECT COALESCE(ss.plan_code, '(none)') AS plan_code, ss.plan_type,
           count(*) AS active_count,
           COALESCE(SUM(CASE WHEN pp.plan_class = 'recurring' THEN pp.mrr_cents ELSE 0 END), 0) AS mrr_cents
    FROM billing.subscription_state ss
    LEFT JOIN core.vw_plan_pricing pp ON pp.plan_code = ss.plan_code
    WHERE UPPER(COALESCE(ss.status,'')) = 'ACTIVE'
    GROUP BY ss.plan_code, ss.plan_type
    ORDER BY mrr_cents DESC
    """,

    # ── Tab 2: searchable customer list ──────────────────────────────────────
    """
    CREATE OR REPLACE VIEW core.vw_customer_list AS
    SELECT l.account_id, l.public_id, l.email, l.display_name, l.role, l.stage, l.activated,
           l.plan_code, l.plan_type, l.sub_status, l.mrr_cents,
           l.matches_uploaded, l.matches_remaining, l.last_activity, l.nps_latest, l.created_at,
           COALESCE(cp.linked_count, 0) AS linked_count
    FROM core.vw_account_lifecycle l
    LEFT JOIN LATERAL (
        SELECT count(*) AS linked_count
        FROM billing.coaches_permission cp
        WHERE cp.coach_account_id = l.account_id AND cp.active
    ) cp ON true
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
        SELECT count(*) AS cnt FROM billing.coaches_permission cp
        WHERE cp.coach_account_id = l.account_id AND cp.active
    ) lc ON true
    WHERE lc.cnt > 0
    """,

    # ── Tab 4: match / processing ops (reads bronze — the live ingest state) ─────────────────
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

    # ── Feedback: NPS summary (scalars) — reads core.nps_response (fed live) ──────────────────
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

    # ── Customer 360: one row of SUMMARY SCALARS per active billing.account ───────────────────
    # Base = core.vw_account_lifecycle (already billing-account-driven, one row/account). LEFT JOIN
    # rollups so unfed sources show 0/NULL (COALESCE), never error. Lists live in the endpoint.
    #   • payments        → billing.payment        by account_id
    #   • support/coach   → *_bot/coach conversations by lower(email)
    #   • consent         → core.account → core.person → core.consent (latest marketing_email)
    #   • task failures   → bronze.submission_context.email → bronze.task_event (status='failed')
    """
    CREATE OR REPLACE VIEW core.vw_customer_360 AS
    SELECT
        l.account_id, l.email, l.display_name, l.role, l.stage,
        l.created_at, l.plan_code, l.plan_type, l.sub_status, l.mrr_cents,
        l.matches_remaining, l.matches_uploaded, l.matches_completed,
        COALESCE(pay.payments_count, 0)        AS payments_count,
        COALESCE(pay.payments_total_cents, 0)  AS payments_total_cents,
        COALESCE(pay.refunds_total_cents, 0)   AS refunds_total_cents,
        pay.last_payment_at,
        COALESCE(sup.support_msgs, 0)          AS support_msgs,
        COALESCE(sup.support_escalations, 0)   AS support_escalations,
        COALESCE(cc.coach_msgs, 0)             AS coach_msgs,
        l.nps_latest,
        l.last_activity,
        cons.consent_marketing,
        COALESCE(tf.tasks_failed, 0)           AS tasks_failed
    FROM core.vw_account_lifecycle l
    LEFT JOIN LATERAL (
        SELECT count(*) AS payments_count,
               COALESCE(SUM(amount_cents) FILTER (WHERE amount_cents > 0), 0) AS payments_total_cents,
               COALESCE(SUM(amount_cents) FILTER (WHERE amount_cents < 0), 0) AS refunds_total_cents,
               max(occurred_at) AS last_payment_at
        FROM billing.payment p WHERE p.account_id = l.account_id
    ) pay ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS support_msgs,
               count(*) FILTER (WHERE escalated_at IS NOT NULL) AS support_escalations
        FROM support_bot.conversations sb WHERE lower(sb.email) = lower(l.email)
    ) sup ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS coach_msgs
        FROM tennis_coach.conversations tc WHERE lower(tc.email) = lower(l.email)
    ) cc ON true
    LEFT JOIN LATERAL (
        SELECT (cn.status = 'granted') AS consent_marketing
        FROM core.account ca
        JOIN core.person pe ON pe.account_id = ca.id
        JOIN core.consent cn ON cn.subject_person_id = pe.id
                            AND cn.consent_type = 'marketing_email'
        WHERE lower(ca.email) = lower(l.email)
        ORDER BY cn.granted_at DESC NULLS LAST, cn.created_at DESC
        LIMIT 1
    ) cons ON true
    LEFT JOIN LATERAL (
        SELECT count(*) AS tasks_failed
        FROM bronze.submission_context sc
        JOIN bronze.task_event te ON te.task_id = sc.task_id AND te.status = 'failed'
        WHERE lower(sc.email) = lower(l.email)
    ) tf ON true
    """,

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 2 — Business-Performance rollups (time-series over the live SoR).
    # All reconcile to raw counts: e.g. SUM(vw_usage_daily.events) == count(usage_event).
    # ════════════════════════════════════════════════════════════════════════

    # Engagement: events + distinct accounts per day, by event_type.
    """
    CREATE OR REPLACE VIEW core.vw_usage_daily AS
    SELECT date_trunc('day', occurred_at)::date AS day,
           event_type,
           count(*)                                       AS events,
           count(DISTINCT account_id)                     AS distinct_accounts
    FROM core.usage_event
    GROUP BY 1, 2
    ORDER BY 1 DESC, 2
    """,

    # DAU — distinct active accounts per calendar day (+ raw event + page-view volume).
    # account_id is NULL for not-yet-linked anonymous traffic, so dau counts logged-in actives.
    """
    CREATE OR REPLACE VIEW core.vw_dau AS
    SELECT date_trunc('day', occurred_at)::date AS day,
           count(DISTINCT account_id) FILTER (WHERE account_id IS NOT NULL) AS dau,
           count(*)                                                          AS events,
           count(*) FILTER (WHERE event_type = 'page_view')                  AS page_views
    FROM core.usage_event
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # MAU — distinct active accounts per calendar month.
    """
    CREATE OR REPLACE VIEW core.vw_mau AS
    SELECT date_trunc('month', occurred_at)::date AS month,
           count(DISTINCT account_id) FILTER (WHERE account_id IS NOT NULL) AS mau,
           count(*)                                                          AS events
    FROM core.usage_event
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # New accounts per month (matches vw_business_health.new_accounts_this_month: active only).
    """
    CREATE OR REPLACE VIEW core.vw_new_accounts_monthly AS
    SELECT date_trunc('month', created_at)::date AS month, count(*) AS new_accounts
    FROM billing.account
    WHERE active
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # Revenue per month — ACTUAL money from billing.payment (gross in, refunds out, net).
    # This is the reconcilable revenue trend (MRR is a current snapshot in vw_business_health;
    # historical MRR isn't reconstructable — we never stored subscription snapshots).
    """
    CREATE OR REPLACE VIEW core.vw_revenue_monthly AS
    SELECT date_trunc('month', occurred_at)::date AS month,
           COALESCE(SUM(amount_cents) FILTER (WHERE amount_cents > 0), 0)  AS gross_cents,
           COALESCE(-SUM(amount_cents) FILTER (WHERE amount_cents < 0), 0) AS refunds_cents,
           COALESCE(SUM(amount_cents), 0)                                  AS net_cents,
           count(*) FILTER (WHERE amount_cents > 0)                        AS payments,
           count(*) FILTER (WHERE amount_cents < 0)                        AS refunds
    FROM billing.payment
    WHERE occurred_at IS NOT NULL
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # Churn per month — subscription cancellations (matches vw_business_health.churned_this_month).
    """
    CREATE OR REPLACE VIEW core.vw_churn_monthly AS
    SELECT date_trunc('month', COALESCE(cancelled_at, payment_cancelled_at))::date AS month,
           count(*) AS churned
    FROM billing.subscription_state
    WHERE COALESCE(cancelled_at, payment_cancelled_at) IS NOT NULL
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # Processing throughput per day — submissions / completed / failed / trimmed + avg duration.
    """
    CREATE OR REPLACE VIEW core.vw_processing_daily AS
    SELECT date_trunc('day', COALESCE(ingest_started_at, last_status_at))::date AS day,
           count(*)                                              AS submissions,
           count(*) FILTER (WHERE ingest_finished_at IS NOT NULL) AS completed,
           count(*) FILTER (WHERE ingest_error IS NOT NULL)       AS failed,
           count(*) FILTER (WHERE trim_status = 'completed')      AS trimmed,
           ROUND(AVG(EXTRACT(epoch FROM (ingest_finished_at - ingest_started_at)))
                 FILTER (WHERE ingest_finished_at IS NOT NULL
                           AND ingest_started_at IS NOT NULL))::int AS avg_complete_seconds
    FROM bronze.submission_context
    WHERE deleted_at IS NULL
      AND COALESCE(ingest_started_at, last_status_at) IS NOT NULL
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # Support chatbot per day — questions / resolved / escalated / cost.
    # resolved = answered without escalation and not flagged needs_human.
    """
    CREATE OR REPLACE VIEW core.vw_support_daily AS
    SELECT date_trunc('day', created_at)::date AS day,
           count(*)                                                            AS questions,
           count(*) FILTER (WHERE escalated_at IS NULL AND needs_human = false) AS resolved,
           count(*) FILTER (WHERE escalated_at IS NOT NULL)                     AS escalated,
           count(*) FILTER (WHERE needs_human)                                  AS needs_human,
           COALESCE(SUM(cost_cents), 0)                                         AS cost_cents
    FROM support_bot.conversations
    GROUP BY 1
    ORDER BY 1 DESC
    """,

    # Support chatbot health — rolling-window scalars (mirrors support_bot.health_metrics in SQL).
    """
    CREATE OR REPLACE VIEW core.vw_support_health AS
    SELECT
        count(*) FILTER (WHERE created_at >= now() - interval '24 hours')                       AS q_24h,
        count(*) FILTER (WHERE created_at >= now() - interval '7 days')                          AS q_7d,
        count(*) FILTER (WHERE created_at >= now() - interval '30 days')                         AS q_30d,
        count(*) FILTER (WHERE escalated_at IS NOT NULL AND created_at >= now() - interval '30 days') AS escalated_30d,
        count(*) FILTER (WHERE escalated_at IS NULL AND needs_human = false
                           AND created_at >= now() - interval '30 days')                         AS resolved_30d,
        ROUND(100.0 * count(*) FILTER (WHERE escalated_at IS NULL AND needs_human = false
                                         AND created_at >= now() - interval '30 days')
              / NULLIF(count(*) FILTER (WHERE created_at >= now() - interval '30 days'), 0), 1)  AS resolution_rate_30d,
        COALESCE(SUM(cost_cents) FILTER (WHERE created_at >= now() - interval '30 days'), 0)      AS cost_cents_30d
    FROM support_bot.conversations
    """,

    # AI-coach usage per day — queries + distinct askers (durable conversations log).
    """
    CREATE OR REPLACE VIEW core.vw_coach_daily AS
    SELECT date_trunc('day', created_at)::date AS day,
           count(*)                       AS queries,
           count(DISTINCT email)          AS distinct_users
    FROM tennis_coach.conversations
    GROUP BY 1
    ORDER BY 1 DESC
    """,
]


def init_cockpit_views(engine=None):
    """Create/refresh the cockpit views (idempotent). Ensures core base schema/views first
    (core_init guarantees core.* tables + vw_account_credits/subscription_current/mrr exist, even
    though Option C no longer reads the billing-slice ones)."""
    engine = core_init(engine)
    with engine.begin() as conn:
        conn.execute(text(_pricing_view_sql()))
        for stmt in _VIEWS:
            conn.execute(text(stmt))
    return engine


if __name__ == "__main__":
    eng = init_cockpit_views()
    print(f"cockpit views initialised on {eng.url.render_as_string(hide_password=True)}")
