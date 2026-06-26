# analytics/traffic.py — guarded website-traffic aggregation over core.usage_event.
#
# Portable + replicable: pass any SQLAlchemy session/connection (.execute(text(...)) works on
# both). page_view metadata fields (set by marketing_crm/tracking/beacon.py): path, referrer,
# anon_id, country, device, browser, os, tz, lang, screen_w, utm_*. page_leave carries duration_ms.

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text

log = logging.getLogger("analytics.traffic")

_PV = "event_type = 'page_view'"
_PL = "event_type = 'page_leave'"


def _window(days: int) -> str:
    return f"occurred_at >= now() - interval '{int(days)} days'"


def _club(club_id: Optional[str]) -> str:
    return "" if not club_id else " AND club_id = :club"


def _p(club_id: Optional[str], **extra) -> Dict[str, Any]:
    d: Dict[str, Any] = dict(extra)
    if club_id:
        d["club"] = str(club_id)
    return d


def _guard(session, fn, default):
    """Run a query; on any error log it, ROLL BACK (so a failed query doesn't poison the shared
    connection's transaction and cascade-empty every later panel), and return the safe default."""
    try:
        return fn()
    except Exception as e:
        log.info("traffic query skipped (%s)", e.__class__.__name__)
        try:
            session.rollback()
        except Exception:
            pass
        return default


def traffic_summary(session, *, days, club_id=None) -> Dict[str, Any]:
    def q():
        row = session.execute(text(f"""
            SELECT count(*) AS visits,
                   count(DISTINCT metadata->>'anon_id')
                     FILTER (WHERE metadata->>'anon_id' IS NOT NULL) AS unique_visitors
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)}{_club(club_id)}
        """), _p(club_id)).mappings().first()
        return {"visits": int(row["visits"] or 0),
                "unique_visitors": int(row["unique_visitors"] or 0)}
    return _guard(session, q, {"visits": 0, "unique_visitors": 0})


def new_vs_returning(session, *, days, club_id=None) -> Dict[str, Any]:
    """new = first-ever page view inside the window; returning = first seen before it."""
    def q():
        row = session.execute(text(f"""
            WITH active AS (
                SELECT DISTINCT metadata->>'anon_id' AS aid
                FROM core.usage_event
                WHERE {_PV} AND {_window(days)} AND metadata->>'anon_id' IS NOT NULL{_club(club_id)}
            ),
            firsts AS (
                SELECT metadata->>'anon_id' AS aid, min(occurred_at) AS first_seen
                FROM core.usage_event
                WHERE {_PV} AND metadata->>'anon_id' IS NOT NULL{_club(club_id)}
                GROUP BY 1
            )
            SELECT
              count(*) FILTER (WHERE f.first_seen >= now() - interval '{int(days)} days') AS new_visitors,
              count(*) FILTER (WHERE f.first_seen <  now() - interval '{int(days)} days') AS returning_visitors
            FROM active a JOIN firsts f ON f.aid = a.aid
        """), _p(club_id)).mappings().first()
        return {"new_visitors": int(row["new_visitors"] or 0),
                "returning_visitors": int(row["returning_visitors"] or 0)}
    return _guard(session, q, {"new_visitors": 0, "returning_visitors": 0})


def visits_daily(session, *, days, club_id=None) -> List[Dict[str, Any]]:
    def q():
        rows = session.execute(text(f"""
            SELECT date_trunc('day', occurred_at)::date AS day,
                   count(*) AS visits,
                   count(DISTINCT metadata->>'anon_id') AS unique_visitors
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)}{_club(club_id)}
            GROUP BY 1 ORDER BY 1
        """), _p(club_id)).mappings().all()
        return [{"day": str(r["day"]), "visits": int(r["visits"] or 0),
                 "unique_visitors": int(r["unique_visitors"] or 0)} for r in rows]
    return _guard(session, q, [])


def traffic_sources(session, *, days, club_id=None) -> List[Dict[str, Any]]:
    """utm_source if present, else the referrer host, else 'direct'."""
    def q():
        rows = session.execute(text(f"""
            SELECT CASE
                     WHEN COALESCE(metadata->>'utm_source','') <> '' THEN metadata->>'utm_source'
                     WHEN COALESCE(metadata->>'referrer','') <> ''
                       THEN split_part(split_part(metadata->>'referrer','//',2),'/',1)
                     ELSE 'direct' END AS source,
                   count(*) AS visits
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)}{_club(club_id)}
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """), _p(club_id)).mappings().all()
        return [{"source": r["source"] or "direct", "visits": int(r["visits"] or 0)} for r in rows]
    return _guard(session, q, [])


def top_pages(session, *, days, club_id=None) -> List[Dict[str, Any]]:
    def q():
        rows = session.execute(text(f"""
            SELECT metadata->>'path' AS path, count(*) AS visits
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)} AND metadata->>'path' IS NOT NULL{_club(club_id)}
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """), _p(club_id)).mappings().all()
        return [{"path": r["path"], "visits": int(r["visits"] or 0)} for r in rows]
    return _guard(session, q, [])


def by_country(session, *, days, club_id=None) -> List[Dict[str, Any]]:
    def q():
        rows = session.execute(text(f"""
            SELECT COALESCE(NULLIF(metadata->>'country',''), 'unknown') AS country,
                   count(*) AS visits
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)}{_club(club_id)}
            GROUP BY 1 ORDER BY 2 DESC LIMIT 15
        """), _p(club_id)).mappings().all()
        return [{"country": r["country"], "visits": int(r["visits"] or 0)} for r in rows]
    return _guard(session, q, [])


def by_device(session, *, days, club_id=None) -> List[Dict[str, Any]]:
    def q():
        rows = session.execute(text(f"""
            SELECT COALESCE(NULLIF(metadata->>'device',''), 'unknown') AS device,
                   count(*) AS visits
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)}{_club(club_id)}
            GROUP BY 1 ORDER BY 2 DESC
        """), _p(club_id)).mappings().all()
        return [{"device": r["device"], "visits": int(r["visits"] or 0)} for r in rows]
    return _guard(session, q, [])


def by_browser(session, *, days, club_id=None) -> List[Dict[str, Any]]:
    def q():
        rows = session.execute(text(f"""
            SELECT COALESCE(NULLIF(metadata->>'browser',''), 'unknown') AS browser,
                   count(*) AS visits
            FROM core.usage_event
            WHERE {_PV} AND {_window(days)}{_club(club_id)}
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """), _p(club_id)).mappings().all()
        return [{"browser": r["browser"], "visits": int(r["visits"] or 0)} for r in rows]
    return _guard(session, q, [])


def time_on_site(session, *, days, club_id=None) -> Dict[str, Any]:
    """Average + median seconds per page from the page_leave duration events."""
    def q():
        row = session.execute(text(f"""
            SELECT round(avg((metadata->>'duration_ms')::numeric) / 1000.0, 1) AS avg_seconds,
                   round((percentile_cont(0.5) WITHIN GROUP (
                         ORDER BY (metadata->>'duration_ms')::numeric) / 1000.0)::numeric, 1) AS median_seconds,
                   count(*) AS samples
            FROM core.usage_event
            WHERE {_PL} AND {_window(days)}
              AND (metadata->>'duration_ms') ~ '^[0-9]+$'{_club(club_id)}
        """), _p(club_id)).mappings().first()
        return {"avg_seconds": float(row["avg_seconds"] or 0),
                "median_seconds": float(row["median_seconds"] or 0),
                "samples": int(row["samples"] or 0)}
    return _guard(session, q, {"avg_seconds": 0, "median_seconds": 0, "samples": 0})


def overview(session, *, days: int = 30, club_id: Optional[str] = None) -> Dict[str, Any]:
    days = max(1, min(int(days), 365))
    traffic = traffic_summary(session, days=days, club_id=club_id)
    nvr = new_vs_returning(session, days=days, club_id=club_id)
    tos = time_on_site(session, days=days, club_id=club_id)
    return {
        "scope": {"days": days, "club_id": str(club_id) if club_id else None},
        "kpis": {
            "visits": traffic["visits"],
            "unique_visitors": traffic["unique_visitors"],
            "new_visitors": nvr["new_visitors"],
            "returning_visitors": nvr["returning_visitors"],
            "avg_seconds": tos["avg_seconds"],
            "median_seconds": tos["median_seconds"],
        },
        "visits_daily": visits_daily(session, days=days, club_id=club_id),
        "traffic_sources": traffic_sources(session, days=days, club_id=club_id),
        "top_pages": top_pages(session, days=days, club_id=club_id),
        "by_country": by_country(session, days=days, club_id=club_id),
        "by_device": by_device(session, days=days, club_id=club_id),
        "by_browser": by_browser(session, days=days, club_id=club_id),
    }
