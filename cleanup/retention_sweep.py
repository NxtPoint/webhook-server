"""Retention sweep — enforce core.retention_rule (privacy scope v2).

⚠️ DRY-RUN BY DEFAULT. This deletes NOTHING unless called with {"dry_run": false}. Until you review
the dry-run report and explicitly disable dry_run, it only counts/reports candidates. It is also NOT
wired into any cron yet — it runs only when POSTed.

Two jobs:
  A. Closed-account data (N days after closure, N from core.retention_rule account_closure window,
     default 90): accounts with deactivated_at older than the window → SOFT-DELETE their match
     submissions (sets deleted_at, so the existing orphan-sweep cascades the child rows), DELETE
     their S3 videos (original + trimmed), and ANONYMISE account/member PII (the billing row stays,
     anonymised — financial is retained ~7y separately).
  B. Expired original videos (retention 0): originals of processed matches still in S3 → delete the
     S3 object + null s3_key (safety net; the pipelines already delete originals post-trim).

Never HARD-deletes billing rows (anonymise only). Reuses the soft-delete cascade (orphan_sweep).
Endpoint: POST /ops/retention-sweep — OPS_KEY-gated. Body: {"dry_run": true|false, "limit": 500}.
"""

from __future__ import annotations

import hmac
import logging
import os

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import text as sql_text

from db_init import engine

log = logging.getLogger(__name__)
bp = Blueprint("retention_sweep", __name__)

_DEFAULT_CLOSURE_DAYS = 90  # fallback if core.retention_rule is unreadable


def _guard_ops() -> bool:
    expected = (os.getenv("OPS_KEY") or "").strip()
    if not expected:
        return False
    cand = [request.headers.get("X-Ops-Key"), request.headers.get("X-OPS-Key")]
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        cand.append(auth.split(None, 1)[1])
    return any(c and hmac.compare_digest(c.strip(), expected) for c in cand)


def _closure_days(conn) -> int:
    """Smallest account_closure retention window that governs deletion of a closed account's data."""
    try:
        d = conn.execute(sql_text(
            "SELECT min(retention_days) FROM core.retention_rule "
            "WHERE applies_after = 'account_closure' AND is_active "
            "AND data_class IN ('account_pii', 'match_video', 'match_analysis')"
        )).scalar()
        return int(d) if d is not None else _DEFAULT_CLOSURE_DAYS
    except Exception:
        return _DEFAULT_CLOSURE_DAYS


def _s3_delete(keys_buckets, dry_run: bool) -> int:
    """keys_buckets: list of (bucket, key). Returns count deleted (or would-delete in dry-run)."""
    keys_buckets = [(b, k) for (b, k) in keys_buckets if k]
    if dry_run:
        return len(keys_buckets)
    import boto3
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION") or "eu-north-1")
    n = 0
    for bucket, key in keys_buckets:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            n += 1
        except Exception as e:
            log.warning("retention S3 delete failed %s/%s: %s", bucket, key, e)
    return n


def _closed_account_sweep(conn, dry_run: bool, limit: int) -> dict:
    days = _closure_days(conn)
    default_bucket = os.getenv("S3_BUCKET") or ""
    accts = conn.execute(sql_text(
        "SELECT id, email FROM billing.account "
        "WHERE active = false AND deactivated_at IS NOT NULL "
        "AND deactivated_at < now() - (:d || ' days')::interval "
        "LIMIT :lim"
    ), {"d": days, "lim": limit}).mappings().all()

    report = {"closure_window_days": days, "accounts": len(accts),
              "submissions_soft_deleted": 0, "s3_videos_deleted": 0,
              "accounts_anonymised": 0, "emails": [a["email"] for a in accts]}

    for a in accts:
        email = a["email"]
        subs = conn.execute(sql_text(
            "SELECT s3_bucket, s3_key, trim_output_s3_key FROM bronze.submission_context "
            "WHERE lower(email) = lower(:e) AND deleted_at IS NULL"
        ), {"e": email}).mappings().all()
        vids = []
        for s in subs:
            b = s["s3_bucket"] or default_bucket
            if s["s3_key"]:
                vids.append((b, s["s3_key"]))
            if s["trim_output_s3_key"]:
                vids.append((b, s["trim_output_s3_key"]))
        report["s3_videos_deleted"] += _s3_delete(vids, dry_run)
        report["submissions_soft_deleted"] += len(subs)

        if not dry_run:
            # soft-delete submissions → orphan-sweep cascades the child rows
            conn.execute(sql_text(
                "UPDATE bronze.submission_context SET deleted_at = now() "
                "WHERE lower(email) = lower(:e) AND deleted_at IS NULL"), {"e": email})
            # anonymise PII (keep the billing row; financial retained separately)
            conn.execute(sql_text(
                "UPDATE billing.member SET full_name = 'Deleted', surname = NULL, phone = NULL, "
                "email = NULL, dob = NULL, notes = NULL, profile_photo_url = NULL "
                "WHERE account_id = :aid"), {"aid": a["id"]})
            conn.execute(sql_text(
                "UPDATE billing.account SET email = :anon, primary_full_name = 'Deleted' "
                "WHERE id = :aid"),
                {"anon": f"deleted-{a['id']}@anonymised.invalid", "aid": a["id"]})
        report["accounts_anonymised"] += 1
    return report


def _expired_original_videos(conn, dry_run: bool, limit: int) -> dict:
    """Original upload retention = 0. Safety net for processed matches whose original still exists
    (the pipelines already delete originals post-trim; this catches stragglers). Trimmed clips are
    NOT touched here — they're 90-days-after-closure (job A)."""
    default_bucket = os.getenv("S3_BUCKET") or ""
    rows = conn.execute(sql_text(
        "SELECT task_id, s3_bucket, s3_key FROM bronze.submission_context "
        "WHERE ingest_finished_at IS NOT NULL AND s3_key IS NOT NULL AND deleted_at IS NULL "
        "LIMIT :lim"
    ), {"lim": limit}).mappings().all()
    vids = [((r["s3_bucket"] or default_bucket), r["s3_key"]) for r in rows]
    n = _s3_delete(vids, dry_run)
    if not dry_run and rows:
        ids = [str(r["task_id"]) for r in rows]
        conn.execute(sql_text(
            "UPDATE bronze.submission_context SET s3_key = NULL WHERE task_id::text = ANY(:ids)"),
            {"ids": ids})
    return {"candidates": len(rows), "s3_originals_deleted": n}


def retention_sweep(dry_run: bool = True, limit: int = 500) -> dict:
    with engine.begin() as conn:
        closed = _closed_account_sweep(conn, dry_run, limit)
        originals = _expired_original_videos(conn, dry_run, limit)
    return {"dry_run": dry_run, "closed_account_data": closed,
            "expired_original_videos": originals}


@bp.post("/ops/retention-sweep")
def retention_sweep_endpoint():
    if not _guard_ops():
        return Response("Forbidden", 403)
    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))   # SAFE DEFAULT — deletes nothing unless dry_run:false
    try:
        limit = int(body.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    try:
        out = retention_sweep(dry_run=dry_run, limit=limit)
        return jsonify({"ok": True, **out})
    except Exception as e:
        log.exception("retention-sweep failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 500
