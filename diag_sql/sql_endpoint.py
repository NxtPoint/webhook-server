"""POST /ops/diag/sql — read-only SQL endpoint for autonomous diagnostics.

Tier-2 autonomy infrastructure (see docs/north_star.md §Autonomy
infrastructure). Lets future Claude Code sessions run SELECT queries
against the live Render DB via WebFetch instead of asking the user to
paste shell output.

Auth: OPS_KEY, header-only (X-Ops-Key or Authorization: Bearer ...).
The same `_guard()` rule as the rest of /ops/* — query-string ?key= is
deliberately NOT accepted to keep OPS_KEY out of access logs.

Hardening:

  1. sqlparse: must be a single statement whose first non-comment
     keyword is SELECT or WITH. Strips trailing semicolons and leading
     comments before checking.
  2. Keyword denylist (word-boundary, case-insensitive): INSERT, UPDATE,
     DELETE, DROP, TRUNCATE, ALTER, CREATE, GRANT, REVOKE, COPY,
     VACUUM, ANALYZE, CALL, DO, LOCK, EXECUTE, SET, RESET, BEGIN,
     COMMIT, ROLLBACK. Plus substring checks for "FOR UPDATE" /
     "FOR SHARE" (SELECT ... FOR UPDATE acquires row locks).
  3. Per-query transaction with `SET LOCAL statement_timeout = '5s';
     SET LOCAL idle_in_transaction_session_timeout = '5s';` — rolls
     back on any exception.
  4. Hard row cap: min(body.limit or 100, 1000). Truncated flag set in
     the response when more rows existed.

Residual risk (documented for the caller — Tomo accepts because OPS_KEY
is server-to-server only):

  * Mutating server-side functions can still be invoked from inside a
    SELECT (e.g. `SELECT pg_terminate_backend(...)`). The keyword-level
    guard does not enumerate every dangerous function. statement_timeout
    bounds blast radius but does not prevent intent.
  * pg_advisory_lock / pg_advisory_unlock may be called from within
    SELECT. Locks are session-scoped; tied to the SQLAlchemy connection.
  * SECURITY-DEFINER user-defined functions, if any exist, would
    execute with their owner's privileges.

The DB role used by the Flask app should be locked down at the Postgres
level for true defence in depth (read-only role, no GRANT EXECUTE on
mutating functions). The endpoint is the second line, not the first.

Body shape:
    {"sql": "SELECT 1", "limit": 100}

Response shape (200):
    {
      "columns": ["?column?"],
      "rows": [[1]],
      "row_count": 1,
      "truncated": false,
      "elapsed_ms": 4
    }

Errors:
  400 — parse failure / forbidden keyword. Body: {"error": "...",
        "offending_keyword": "..." (when applicable)}.
  401 — missing / wrong OPS_KEY.
  408 — statement_timeout fired.
  500 — DB error or other internal failure. Traceback is included in
        the response body ONLY when the caller is authenticated, to
        avoid leaking schema details to unauth probes.
"""

from __future__ import annotations

import logging
import os
import re
import time
import traceback
from typing import Any

import sqlparse
from flask import Blueprint, jsonify, request, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import OperationalError

from db_init import engine

log = logging.getLogger(__name__)

bp = Blueprint("diag_sql", __name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
STATEMENT_TIMEOUT = "5s"
IDLE_IN_TX_TIMEOUT = "5s"

# Keywords that imply mutation, transaction control, or session-state
# changes. Matched as standalone tokens (word-boundary, case-insensitive)
# AFTER comments are stripped — `SELECT 'INSERT' AS x` is NOT rejected
# because the keyword is inside a string literal and sqlparse strips
# string contents before token extraction.
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE",
    "GRANT", "REVOKE", "COPY", "VACUUM", "ANALYZE", "CALL", "DO", "LOCK",
    "EXECUTE", "SET", "RESET", "BEGIN", "COMMIT", "ROLLBACK",
)

# Substring patterns that aren't single-word but are equally dangerous.
# `SELECT ... FOR UPDATE` / `FOR SHARE` acquire row locks; reject.
_FORBIDDEN_PHRASES = ("FOR UPDATE", "FOR SHARE")

# Compile once. Boundary characters: anything not a word character.
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _guard_ops() -> bool:
    """Mirror upload_app._guard — header-only OPS_KEY auth.

    Accepted headers:
      - X-Ops-Key
      - X-OPS-Key
      - Authorization: Bearer <key>

    Query-string ?key= is deliberately NOT supported.
    """
    import hmac
    expected = (os.getenv("OPS_KEY") or "").strip()
    if not expected:
        return False
    candidates = [
        request.headers.get("X-Ops-Key"),
        request.headers.get("X-OPS-Key"),
    ]
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        candidates.append(auth.split(None, 1)[1])
    for c in candidates:
        if c and hmac.compare_digest(c.strip(), expected):
            return True
    return False


def _strip_comments_and_whitespace(sql: str) -> str:
    """Remove SQL comments (-- and /* */) and trim. Used for the
    leading-keyword check so a query that starts with a comment block
    is still recognised as a SELECT.
    """
    # sqlparse.format with strip_comments=True handles both single-line
    # (-- ...) and block (/* ... */) comments correctly.
    stripped = sqlparse.format(sql, strip_comments=True).strip()
    # Trim trailing semicolons and whitespace.
    stripped = re.sub(r"\s*;\s*$", "", stripped)
    return stripped


def _validate_sql(sql: str) -> tuple[bool, str | None, str | None]:
    """Returns (ok, error_msg, offending_keyword).

    Performs in order:
      1. sqlparse.parse — exactly one statement.
      2. Statement type check — DML SELECT, or starts with WITH.
      3. Keyword denylist — word-boundary match anywhere in the cleaned
         text (excluding comments).
      4. Phrase denylist — simple substring search for FOR UPDATE etc.
    """
    if not sql or not sql.strip():
        return False, "sql is required and must not be empty", None

    parsed = sqlparse.parse(sql)
    # sqlparse sometimes emits trailing empty statements when the input
    # ends with `;`. Filter those out.
    parsed = [p for p in parsed if p.tokens and str(p).strip()]
    if len(parsed) == 0:
        return False, "no SQL statement found", None
    if len(parsed) > 1:
        return False, "only a single SQL statement is allowed", None

    stmt = parsed[0]
    stmt_type = stmt.get_type()  # 'SELECT', 'INSERT', 'UPDATE', 'UNKNOWN', ...

    # sqlparse classifies CTEs (`WITH ... SELECT`) as type 'UNKNOWN'.
    # Fall back to a leading-keyword check on the comment-stripped text.
    cleaned = _strip_comments_and_whitespace(sql)
    if not cleaned:
        return False, "sql contained only comments / whitespace", None

    leading = cleaned.split(None, 1)[0].upper() if cleaned else ""
    if stmt_type == "SELECT" or leading == "SELECT":
        pass  # ok
    elif leading == "WITH":
        # CTE — must contain a SELECT body. Parser-level check via the
        # fact that no forbidden keyword appears (DML CTEs would trip
        # the denylist below anyway).
        pass
    else:
        return False, f"only SELECT / WITH ... SELECT is allowed (got: {leading or stmt_type})", None

    # Keyword denylist — case-insensitive, word-boundary, on the
    # comment-stripped text. sqlparse.format() leaves string literals
    # intact, but they're enclosed in quotes so `\b` won't match
    # arbitrary text content unless adjacent to word characters. To be
    # safe we also strip out quoted strings before the regex check.
    no_strings = re.sub(r"'(?:''|[^'])*'", "''", cleaned)
    no_strings = re.sub(r'"(?:""|[^"])*"', '""', no_strings)

    m = _FORBIDDEN_RE.search(no_strings)
    if m:
        return False, f"forbidden keyword: {m.group(1).upper()}", m.group(1).upper()

    upper_no_strings = no_strings.upper()
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in upper_no_strings:
            return False, f"forbidden phrase: {phrase}", phrase

    return True, None, None


def _execute(sql: str, limit: int) -> dict[str, Any]:
    """Run the query in a transaction with statement_timeout, returning
    columns / rows / truncation info / elapsed_ms.

    Caller is responsible for ensuring `sql` has already passed
    _validate_sql.
    """
    capped = min(max(int(limit or DEFAULT_LIMIT), 1), MAX_LIMIT)
    fetch_n = capped + 1  # one extra to detect truncation

    started = time.perf_counter()
    with engine.connect() as conn:
        # Open an explicit transaction so SET LOCAL is bounded to it.
        with conn.begin():
            conn.execute(sql_text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
            conn.execute(sql_text(
                f"SET LOCAL idle_in_transaction_session_timeout = '{IDLE_IN_TX_TIMEOUT}'"
            ))

            res = conn.execute(sql_text(sql))
            cols = list(res.keys())
            rows = res.fetchmany(fetch_n)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    truncated = len(rows) > capped
    if truncated:
        rows = rows[:capped]

    # Coerce rows to plain lists of JSON-safe primitives. Anything
    # non-trivially serialisable (datetime, Decimal, UUID, dict, ...)
    # falls back to str().
    safe_rows: list[list[Any]] = []
    for r in rows:
        safe_rows.append([_jsonable(v) for v in r])

    return {
        "columns": cols,
        "rows": safe_rows,
        "row_count": len(safe_rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    # Lists / dicts are common from JSON columns.
    if isinstance(v, (list, dict)):
        return v
    # Datetime / Decimal / UUID / etc.
    try:
        return str(v)
    except Exception:
        return repr(v)


@bp.post("/ops/diag/sql")
def ops_diag_sql():
    """Run a single read-only SELECT and return columns + rows.

    See module docstring for full contract.
    """
    authed = _guard_ops()
    if not authed:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    sql = (body.get("sql") or "").strip()
    raw_limit = body.get("limit", DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400

    ok, err, offending = _validate_sql(sql)
    if not ok:
        payload: dict[str, Any] = {"error": err}
        if offending:
            payload["offending_keyword"] = offending
        return jsonify(payload), 400

    requester_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                    or request.remote_addr or "unknown")
    started = time.perf_counter()

    try:
        result = _execute(sql, limit)
    except OperationalError as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        msg = str(getattr(e, "orig", e))
        # Postgres reports statement_timeout via SQLSTATE 57014
        # ("query_canceled"). Prefer to match on the SQLSTATE attribute
        # of the underlying psycopg error when available, otherwise the
        # message text.
        sqlstate = getattr(getattr(e, "orig", None), "sqlstate", None) or ""
        if sqlstate == "57014" or "statement timeout" in msg.lower():
            log.warning(
                "diag_sql timeout ip=%s elapsed_ms=%d sql=%r",
                requester_ip, elapsed_ms, sql,
            )
            return jsonify({"error": "statement_timeout (5s) exceeded"}), 408
        log.exception("diag_sql DB error ip=%s elapsed_ms=%d", requester_ip, elapsed_ms)
        # Authenticated callers get a traceback; the unauthenticated
        # branch never reaches here because we 401 above. Still useful
        # to keep the conditional comment for future maintainers.
        return jsonify({
            "error": "database error",
            "detail": msg,
            "traceback": traceback.format_exc(),
        }), 500
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.exception("diag_sql unexpected failure ip=%s elapsed_ms=%d", requester_ip, elapsed_ms)
        return jsonify({
            "error": f"{e.__class__.__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500

    log.info(
        "diag_sql ok ip=%s elapsed_ms=%d row_count=%d truncated=%s sql=%r",
        requester_ip, result["elapsed_ms"], result["row_count"],
        result["truncated"], sql,
    )
    return jsonify(result), 200
