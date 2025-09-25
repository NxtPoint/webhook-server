#!/usr/bin/env bash
set -e
PORT_TO_BIND="${PORT:-8088}"

echo "==> Running Superset DB upgrade (with retries)..."
for i in 1 2 3 4 5; do
  if superset db upgrade; then
    echo "==> DB upgrade successful"
    break
  fi
  echo "==> DB upgrade failed (attempt $i); sleeping 5s..."
  sleep 5
done

# Upsert two admin users: your preferred (NxtPoint) and a fallback (admin/ChangeMe123!)
echo "==> Ensuring admin users exist..."
python - <<'PY'
import os
from superset.app import create_app
app = create_app()
with app.app_context():
    sm = app.appbuilder.sm
    admin_role = sm.find_role("Admin")

    def upsert(username, email, password):
        user = sm.find_user(username=username)
        if user is None:
            sm.add_user(username=username, first_name="Admin", last_name="User",
                        email=email, role=admin_role, password=password, is_active=True)
            print(f"[OK] created {username}")
        else:
            try:
                user.is_active = True
            except Exception:
                user.active = True
            if admin_role and admin_role not in user.roles:
                user.roles = [admin_role]
            sm.update_user(user, password=password)
            print(f"[OK] updated {username}")

    # Preferred from env
    u = os.environ.get("SUPERSET_ADMIN_USER") or "NxtPoint"
    e = os.environ.get("SUPERSET_ADMIN_EMAIL") or "info@nextpointtennis.com"
    p = os.environ.get("SUPERSET_ADMIN_PASSWORD") or "ChangeMe123!"
    upsert(u, e, p)

    # Fallback
    upsert("admin", "admin@nextpointtennis.com", "ChangeMe123!")
PY

echo "==> superset init..."
superset init || true

echo "==> starting gunicorn on $PORT_TO_BIND ..."
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"$PORT_TO_BIND" "superset.app:create_app()"