"""Read-only SQL diagnostic endpoint.

Exposes POST /ops/diag/sql for SELECT-only queries against the live DB.
Designed for autonomous Claude Code sessions to inspect production state
via WebFetch without round-tripping through the user's Render shell.

OPS_KEY-gated, header-only auth. SELECT/CTE-only enforced via sqlparse +
keyword denylist. Each query runs in its own transaction with a 5s
statement_timeout. Result rows hard-capped at min(limit, 1000).

See diag_sql/sql_endpoint.py for the implementation; CLAUDE.md
§Diagnostics & Ops for the contract.
"""

from .sql_endpoint import bp as diag_sql_bp  # noqa: F401
