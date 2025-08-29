# ui_app.py
import os, traceback
from flask import Blueprint, render_template_string, request, url_for, jsonify, Response
from sqlalchemy import text

# Try to prepare DB, but don't crash the import if missing
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = None
db_error = None
if DATABASE_URL:
    try:
        from db_init import engine as _engine
        engine = _engine
    except Exception as e:
        db_error = f"db_init import failed: {e}"
else:
    db_error = "DATABASE_URL not set"

OPS_KEY = os.environ.get("OPS_KEY", "")

ui_bp = Blueprint(
    "ui",
    __name__,
    template_folder="templates",
    static_folder="static",    # provides /upload/static/<file>
)

def _require_ops_key() -> bool:
    key = request.args.get("key", "")
    return bool(OPS_KEY) and key == OPS_KEY

_BASE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>NextPoint Admin</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px;line-height:1.35}
    a{color:#2b6cb0;text-decoration:none} a:hover{text-decoration:underline}
    .btn{display:inline-block;border:1px solid #cbd5e0;padding:6px 10px;border-radius:8px;margin:2px 6px 2px 0}
    .btn.danger{border-color:#f56565;color:#c53030}
    table{border-collapse:collapse;width:100%;margin-top:16px}
    th,td{border:1px solid #e2e8f0;padding:8px;text-align:left}
    th{background:#f7fafc}
    .mono{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace}
    .small{color:#4a5568;font-size:12px}
    .row{margin:10px 0}
    .pill{display:inline-block;background:#edf2f7;border:1px solid #e2e8f0;border-radius:12px;padding:2px 8px;margin-left:6px;font-size:12px}
  </style>
</head>
<body>
  <h1>NextPoint Admin</h1>
  <div class="row">
    <a class="btn" href="{{ url_for('ui.sessions') }}">Sessions</a>
    <a class="btn" href="{{ url_for('ui.sql') }}">SQL</a>
  </div>
  {% if db_error %}
    <p class="small" style="color:#c53030">DB not ready: {{ db_error }}</p>
  {% endif %}
  {% block body %}{% endblock %}
</body>
</html>
"""

@ui_bp.route("/")
def home():
    # Correct “Dropbox ready” for either legacy token OR refresh flow
    dropbox_ready = bool(
        os.environ.get("DROPBOX_ACCESS_TOKEN") or
        (os.environ.get("DROPBOX_APP_KEY") and os.environ.get("DROPBOX_APP_SECRET") and os.environ.get("DROPBOX_REFRESH_TOKEN"))
    )
    sportai_ready = bool(os.environ.get("SPORT_AI_TOKEN") or os.environ.get("SPORTAI_TOKEN"))
    target_folder = os.environ.get("DROPBOX_UPLOAD_FOLDER", "/incoming")
    max_upload_mb = int(os.environ.get("MAX_CONTENT_MB", os.environ.get("MAX_UPLOAD_MB", "150")))

    return render_template_string(
        "{% include 'upload.html' %}",
        dropbox_ready=dropbox_ready,
        sportai_ready=sportai_ready,
        target_folder=target_folder,
        max_upload_mb=max_upload_mb,
    )

@ui_bp.route("/sessions")
def sessions():
    if not engine:
        return render_template_string("{% extends _BASE %}{% block body %}<p>DB not available.</p>{% endblock %}",
                                      _BASE=_BASE, db_error=db_error), 503
    try:
        sql = """
            SELECT s.session_uid,
                   (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id=s.session_id)         AS players,
                   (SELECT COUNT(*) FROM dim_rally  dr WHERE dr.session_id=s.session_id)         AS rallies,
                   (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=s.session_id)         AS swings,
                   (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=s.session_id)          AS ball_bounces,
                   (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=s.session_id) AS ball_positions,
                   (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=s.session_id) AS player_positions,
                   (SELECT COUNT(*) FROM raw_result rr WHERE rr.session_id=s.session_id)         AS snapshots
            FROM dim_session s
            ORDER BY s.session_id DESC
            LIMIT 200
        """
        with engine.connect() as conn:
            rows = conn.execute(text(sql)).mappings().all()
        tpl = """
        {% extends _BASE %}{% block body %}
          <h2>Sessions</h2>
          <div class="small">UI prefix:
            <span class="mono">{{ request.url_root.rstrip('/') }}{{ request.script_root }}/upload</span>
          </div>
          <div class="row small">
            Global ops:
            <a class="pill" href="/ops/init-views?key={{ key }}" target="_blank">/ops/init-views</a>
            <a class="pill" href="/ops/perf-indexes?key={{ key }}" target="_blank">/ops/perf-indexes</a>
            <a class="pill" href="/ops/db-ping?key={{ key }}" target="_blank">/ops/db-ping</a>
            <a class="pill" href="/ops/build-gold?key={{ key }}" target="_blank">/ops/build-gold</a>
          </div>
          <table>
            <thead><tr>
              <th>Session UID</th><th>Players</th><th>Rallies</th><th>Swings</th>
              <th>Bounces</th><th>Ball Pos</th><th>Player Pos</th><th>Snapshots</th><th>Actions</th>
            </tr></thead>
            <tbody>
            {% for r in rows %}
              <tr>
                <td class="mono">{{ r.session_uid }}</td>
                <td>{{ r.players }}</td>
                <td>{{ r.rallies }}</td>
                <td>{{ r.swings }}</td>
                <td>{{ r.ball_bounces }}</td>
                <td>{{ r.ball_positions }}</td>
                <td>{{ r.player_positions }}</td>
                <td>{{ r.snapshots }}</td>
                <td>
                  <a class="btn" target="_blank"
                     href="/ops/reconcile?key={{ key }}&session_uid={{ r.session_uid }}">Reconcile</a>
                  <a class="btn" target="_blank"
                     href="/ops/repair-swings?key={{ key }}&session_uid={{ r.session_uid }}">Repair serves</a>
                  <a class="btn" target="_blank"
                     href="{{ url_for('ui.peek', session_uid=r.session_uid) }}">Peek</a>
                  <a class="btn danger"
                     href="{{ url_for('ui.delete_confirm', session_uid=r.session_uid) }}">Delete</a>
                </td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        {% endblock %}
        """
        return render_template_string(tpl, _BASE=_BASE, rows=rows, key=OPS_KEY, db_error=db_error)
    except Exception:
        tb = traceback.format_exc()
        return Response("UI /upload/sessions failed:\n\n" + tb, mimetype="text/plain", status=500)

@ui_bp.route("/peek/<session_uid>")
def peek(session_uid):
    if not engine:
        return Response("DB not available", status=503)
    q1 = text("""
        SELECT point_number, shot_number, swing_id, player_uid, serve, serve_type,
               shot_result, shot_description_depth, ball_hit_s
        FROM vw_point_log
        WHERE session_id = (SELECT session_id FROM dim_session WHERE session_uid=:u)
        ORDER BY point_number, shot_number
        LIMIT 20
    """)
    q2 = text("""
        SELECT swing_id, rally_id, serve, serve_type, ball_hit_s, meta
        FROM vw_swing
        WHERE session_id = (SELECT session_id FROM dim_session WHERE session_uid=:u)
        ORDER BY COALESCE(ball_hit_s,start_s)
        LIMIT 12
    """)
    with engine.connect() as conn:
        pl = [dict(r) for r in conn.execute(q1, {"u": session_uid}).mappings().all()]
        sw = [dict(r) for r in conn.execute(q2, {"u": session_uid}).mappings().all()]
    tpl = """
    {% extends _BASE %}{% block body %}
      <h2>Peek: <span class="mono">{{ uid }}</span></h2>
      <h3>vw_point_log (first 20)</h3>
      <table>
        {% if pl %}<thead><tr>{% for k in pl[0].keys() %}<th>{{k}}</th>{% endfor %}</tr></thead>{% endif %}
        <tbody>{% for r in pl %}<tr>{% for v in r.values() %}<td class="mono">{{ v }}</td>{% endfor %}</tr>{% endfor %}</tbody>
      </table>
      <h3>vw_swing (first 12)</h3>
      <table>
        {% if sw %}<thead><tr>{% for k in sw[0].keys() %}<th>{{k}}</th>{% endfor %}</tr></thead>{% endif %}
        <tbody>{% for r in sw %}<tr>{% for v in r.values() %}<td class="mono">{{ v }}</td>{% endfor %}</tr>{% endfor %}</tbody>
      </table>
    {% endblock %}
    """
    return render_template_string(tpl, _BASE=_BASE, uid=session_uid, pl=pl, sw=sw, db_error=db_error)

@ui_bp.route("/delete/<session_uid>")
def delete_confirm(session_uid):
    tpl = """
    {% extends _BASE %}{% block body %}
      <h2>Delete Session</h2>
      <p>Delete <span class="mono">{{ uid }}</span>?</p>
      <p>
        <a class="btn danger" href="/ops/delete-session?key={{ key }}&session_uid={{ uid }}">Yes, delete</a>
        <a class="btn" href="{{ url_for('ui.sessions') }}">Cancel</a>
      </p>
    {% endblock %}
    """
    return render_template_string(tpl, _BASE=_BASE, uid=session_uid, key=OPS_KEY, db_error=db_error)

@ui_bp.route("/sql", methods=["GET", "POST"])
def sql():
    if not engine:
        return render_template_string("{% extends _BASE %}{% block body %}<p>DB not available.</p>{% endblock %}",
                                      _BASE=_BASE, db_error=db_error), 503
    default_q = request.values.get("q", "SELECT now() AT TIME ZONE 'utc' AS utc_now")
    result, error = None, None
    if request.method == "POST":
        q = request.form.get("q", "")
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = 60000"))
                conn.execute(text("SET LOCAL TRANSACTION READ ONLY"))
                rows = conn.execute(text(q)).mappings().all()
                result = [dict(r) for r in rows]
        except Exception as e:
            error = str(e)
            result = None
        default_q = q
    tpl = """
    {% extends _BASE %}{% block body %}
      <h2>SQL (read-only)</h2>
      <form method="post">
        <textarea name="q" rows="8" style="width:100%;font-family:monospace">{{ default_q }}</textarea>
        <div class="row"><button class="btn" type="submit">Run</button></div>
      </form>
      {% if error %}<pre style="color:#c53030">{{ error }}</pre>{% endif %}
      {% if result is not none %}
        <div class="row small">Rows: {{ result|length }}</div>
        <table>
          {% if result|length > 0 %}
          <thead><tr>{% for k in result[0].keys() %}<th>{{ k }}</th>{% endfor %}</tr></thead>
          {% endif %}
          <tbody>
          {% for r in result %}
            <tr>{% for v in r.values() %}<td class="mono">{{ v }}</td>{% endfor %}</tr>
          {% endfor %}
          </tbody>
        </table>
      {% endif %}
      <p class="small">
        Quick links:
        <a class="pill" href="/ops/db-ping?key={{ key }}" target="_blank">/ops/db-ping</a>
        <a class="pill" href="/ops/init-views?key={{ key }}" target="_blank">/ops/init-views</a>
        <a class="pill" href="/ops/perf-indexes?key={{ key }}" target="_blank">/ops/perf-indexes</a>
        <a class="pill" href="/ops/build-gold?key={{ key }}" target="_blank">/ops/build-gold</a>
      </p>
    {% endblock %}
    """
    return render_template_string(tpl, _BASE=_BASE, default_q=default_q, result=result, error=error, key=OPS_KEY, db_error=db_error)

@ui_bp.route("/sessions_raw")
def sessions_raw():
    if not engine:
        return jsonify({"ok": False, "error": db_error}), 503
    try:
        sql = "SELECT session_uid FROM dim_session ORDER BY session_id DESC LIMIT 10"
        with engine.connect() as conn:
            rows = conn.execute(text(sql)).mappings().all()
        return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500

@ui_bp.route("/health")
def ui_health():
    if not engine:
        return jsonify({"ok": False, "error": db_error}), 503
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@ui_bp.route("/ops/build-gold", methods=["GET"])
def ops_build_gold():
    if not _require_ops_key():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not engine:
        return jsonify({"ok": False, "error": db_error}), 503
    ddl = [
        "DROP TABLE IF EXISTS point_log_tbl;",
        "CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log;",
        "DROP TABLE IF EXISTS point_summary_tbl;",
        "CREATE TABLE point_summary_tbl AS SELECT * FROM vw_point_summary;",
        "CREATE INDEX IF NOT EXISTS ix_pl_session ON point_log_tbl(session_uid, point_number, shot_number);",
        "CREATE INDEX IF NOT EXISTS ix_ps_session ON point_summary_tbl(session_uid, point_number);",
    ]
    try:
        with engine.begin() as conn:
            for stmt in ddl:
                conn.execute(text(stmt))
        return jsonify({"ok": True, "built": ["point_log_tbl", "point_summary_tbl"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
