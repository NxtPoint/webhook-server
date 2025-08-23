# ui_app.py
import os
from flask import Blueprint, render_template_string, request, redirect, url_for
from sqlalchemy import create_engine, text

# Create our own engine (avoids circular imports)
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL required for UI")
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

OPS_KEY = os.environ.get("OPS_KEY", "")

ui_bp = Blueprint("ui", __name__, template_folder=".", static_folder=".")

# ------------ templates ------------
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
    .btn{display:inline-block;border:1px solid #cbd5e0;padding:6px 10px;border-radius:8px;margin-right:8px}
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
  {% block body %}{% endblock %}
</body>
</html>
"""

@ui_bp.route("/")
def home():
    return redirect(url_for("ui.sessions"))

@ui_bp.route("/sessions")
def sessions():
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT s.session_uid,
               (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id=s.session_id) AS players,
               (SELECT COUNT(*) FROM dim_rally dr WHERE dr.session_id=s.session_id) AS rallies,
               (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=s.session_id) AS swings,
               (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=s.session_id) AS ball_bounces,
               (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=s.session_id) AS ball_positions,
               (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=s.session_id) AS player_positions,
               (SELECT COUNT(*) FROM raw_result rr WHERE rr.session_id=s.session_id) AS snapshots
            FROM dim_session s
            ORDER BY s.session_uid
        """)).mappings().all()
    tpl = """
    {% extends _BASE %}{% block body %}
      <h2>Sessions</h2>
      <div class="small">UI prefix: <span class="mono">{{ request.url_root.rstrip('/') }}{{ request.script_root }}/upload</span></div>
      <table>
        <thead>
          <tr>
            <th>Session UID</th>
            <th>Players</th>
            <th>Rallies</th>
            <th>Swings</th>
            <th>Bounces</th>
            <th>Ball Pos</th>
            <th>Player Pos</th>
            <th>Snapshots</th>
            <th>Actions</th>
          </tr>
        </thead>
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
                 href="/ops/init-views?key={{ key }}">Init Views</a>
              <a class="btn danger"
                 href="{{ url_for('ui.delete_confirm', session_uid=r.session_uid) }}">Delete</a>
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    {% endblock %}
    """
    return render_template_string(tpl, _BASE=_BASE, rows=rows, key=OPS_KEY)

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
      <p class="small">This calls the backend /ops/delete-session endpoint.</p>
    {% endblock %}
    """
    return render_template_string(tpl, _BASE=_BASE, uid=session_uid, key=OPS_KEY)

@ui_bp.route("/sql", methods=["GET", "POST"])
def sql():
    default_q = request.values.get("q", "SELECT now() AT TIME ZONE 'utc' AS utc_now")
    result = None
    error = None
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
          <thead><tr>
            {% for k in result[0].keys() %}<th>{{ k }}</th>{% endfor %}
          </tr></thead>
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
      </p>
    {% endblock %}
    """
    return render_template_string(tpl, _BASE=_BASE, default_q=default_q, result=result, error=error, key=OPS_KEY)
